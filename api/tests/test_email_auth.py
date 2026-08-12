"""Email code sign-in (EMAILAUTH.md): the whole door, and every lock on it.

Nothing about the credential is faked. Codes come from ``auth.new_login_code``
— the real CSPRNG generator — and are only OBSERVED on their way past, by a
spy that wraps it; the digest under test is the one the route stored, and
every refusal below is the route's own. The only thing replaced is the
network: ``mailer.post_json``, the single function that would otherwise reach
out to Resend.

Mirrors the env/import dance of ``test_google_auth.py`` — all CC_* vars are
set BEFORE any app module is imported, ``get_settings`` is lru_cached so its
cache is cleared, and the settings-snapshotting modules (queue_app / worker /
main, plus auth for its rate-limit windows) are purged for a clean re-import.
The whole os.environ is snapshotted and restored around each client.

No pipeline runs here: signing in touches the users and login_codes tables and
nothing else, so there is no video and no transcriber to stand up.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import pytest

EMAIL = "creator@example.com"
OTHER_EMAIL = "someone-else@example.com"
PASSWORD = "correct-horse-battery"
GOOGLE_SUB = "104729000000000000042"

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
    "clipcatalyst_api.auth",
)


def _purge() -> None:
    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _set_env(**values: str | None) -> None:
    """Flip CC_* vars at runtime; every route reads get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


@pytest.fixture()
def sandbox(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """A TestClient with the console mailer on, and every code minted recorded.

    `codes` is how the tests read the code a user would have read in their
    inbox: the generator is the real one and runs for real — this only watches
    what came out of it, which is the closest a test can get to being the
    person holding the email.

    `sent` records what the resend backend would have PUT on the wire, and
    `reply` is what the stubbed HTTP layer answers with, so a delivery failure
    is exercised as a failure of the provider rather than of our code.
    """
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("emaildata")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": "",
            "CC_BILLING": "off",
            "CC_MAILER": "console",
        }
    )
    for name in ("CC_API_TOKEN", "CC_EMAIL_CODE_TTL_MINUTES", "CC_EMAIL_CODE_PER_HOUR"):
        os.environ.pop(name, None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth, mailer
    from clipcatalyst_api.main import app

    codes: list[str] = []
    real_new_code = auth.new_login_code

    def _spy_new_code() -> str:
        code = real_new_code()
        codes.append(code)
        return code

    monkeypatch.setattr(auth, "new_login_code", _spy_new_code)

    sent: list[dict] = []
    reply: dict = {"status": 200, "body": '{"id":"re_123"}', "raise": None}

    def _stub_post(url: str, payload: dict, headers: dict) -> tuple[int, str]:
        sent.append({"url": url, "payload": payload, "headers": dict(headers)})
        if reply["raise"] is not None:
            raise reply["raise"]
        return int(reply["status"]), str(reply["body"])

    monkeypatch.setattr(mailer, "post_json", _stub_post)
    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                data_dir=data_dir,
                codes=codes,
                sent=sent,
                reply=reply,
            )
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _start(client, email: str = EMAIL):
    return client.post("/v1/auth/email/start", json={"email": email})


def _verify(client, email: str, code: str):
    return client.post("/v1/auth/email/verify", json={"email": email, "code": code})


def _sign_in(sandbox: SimpleNamespace, email: str = EMAIL):
    """One whole round trip: ask for a code, read it, type it back."""
    started = _start(sandbox.client, email)
    assert started.status_code == 200, started.text
    return _verify(sandbox.client, email, sandbox.codes[-1])


def _register(client, email: str, password: str = PASSWORD) -> dict:
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _users() -> list[dict]:
    """Every user row, straight from SQLite — the server-side fact."""
    from clipcatalyst_api import db

    with contextlib.closing(db._connect()) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def _code_rows() -> list[dict]:
    from clipcatalyst_api import db

    with contextlib.closing(db._connect()) as conn:
        rows = conn.execute("SELECT * FROM login_codes ORDER BY email").fetchall()
    return [dict(row) for row in rows]


def _expire_code(email: str) -> None:
    """Push an address's code an hour into the past (no waiting in tests)."""
    from clipcatalyst_api import db

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="milliseconds"
    )
    with contextlib.closing(db._connect()) as conn:
        conn.execute(
            "UPDATE login_codes SET expires_at = ? WHERE email = ?", (past, email)
        )


class _Clock:
    """A pinned minute that can be stepped without ever leaving its hour.

    The per-address window is an HOUR, but any test that fires more than
    RATE_LIMIT_PER_MINUTE starts trips the per-CLIENT limiter first and would
    measure that instead. Stepping the minute between requests gives every
    request a fresh client window while the hour under test stands still, so
    what a 429 means is never ambiguous. Both counters read `_current_minute`
    (the hour is derived from it), so replacing this one callable pins both.
    """

    def __init__(self, hour: int) -> None:
        self._hour = hour
        self._minute = hour * 60

    def __call__(self) -> int:
        return self._minute

    def tick(self) -> None:
        self._minute += 1
        assert self._minute // 60 == self._hour, "the test walked into a new hour"


# --------------------------------------------------------------------------- #
# 1. The round trip, and what it does to the users table.
# --------------------------------------------------------------------------- #


def test_a_round_trip_creates_a_password_less_account_and_signs_in(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    started = _start(client)
    assert started.status_code == 200, started.text
    assert started.json() == {"sent": True}
    assert started.headers["cache-control"] == "no-store"
    # Nothing was created by ASKING: the account happens at verify time, which
    # is what leaves `start` with nothing to enumerate.
    assert _users() == []

    row = _code_rows()[0]
    assert row["email"] == EMAIL
    assert row["attempts"] == 0
    # What is stored is a digest, not the code (the whole DB file is checked
    # in test_the_plaintext_code_appears_nowhere_in_the_database_file).
    assert row["code_hash"] != sandbox.codes[-1]
    assert len(row["code_hash"]) == 64

    resp = _verify(client, EMAIL, sandbox.codes[-1])
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"  # carries a session token

    body = resp.json()
    assert body["token"].startswith("cc_sess_")
    assert body["user"]["email"] == EMAIL
    assert body["user"]["plan"] == "free"

    # An ordinary session: it works on every gated route.
    me = client.get("/v1/me", headers=_bearer(body["token"]))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == EMAIL
    # Password-less and Google-less — the account page still says how its
    # owner gets back in, because this server has a mailer.
    assert me.json()["auth_methods"] == ["email"]

    rows = _users()
    assert len(rows) == 1
    assert rows[0]["password_hash"] == ""  # NOT a hash of the empty string
    assert rows[0]["google_sub"] == ""
    # Single use: the row is gone the moment it was spent.
    assert _code_rows() == []


def test_a_second_round_trip_signs_into_the_same_account(
    sandbox: SimpleNamespace,
) -> None:
    first = _sign_in(sandbox)
    assert first.status_code == 200, first.text

    second = _sign_in(sandbox)
    assert second.status_code == 200, second.text
    assert second.json()["user"]["id"] == first.json()["user"]["id"]
    assert second.json()["token"] != first.json()["token"]  # distinct sessions
    assert len(_users()) == 1  # matched on the address, not re-created

    # Both sessions are live at once, exactly like two password logins.
    for token in (first.json()["token"], second.json()["token"]):
        assert (
            sandbox.client.get("/v1/me", headers=_bearer(token)).status_code == 200
        )


def test_the_address_is_normalized_before_anything_is_keyed_on_it(
    sandbox: SimpleNamespace,
) -> None:
    """Case and whitespace are formatting, not identity."""
    started = _start(sandbox.client, "  Creator@EXAMPLE.com  ")
    assert started.status_code == 200, started.text
    assert [row["email"] for row in _code_rows()] == [EMAIL]

    resp = _verify(sandbox.client, "CREATOR@example.com", sandbox.codes[-1])
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == EMAIL
    assert len(_users()) == 1


def test_an_existing_password_account_is_signed_into_untouched(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    user_id = _register(client, EMAIL)["user"]["id"]
    stored = _users()[0]["password_hash"]
    assert stored  # a real scrypt hash

    resp = _sign_in(sandbox)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == user_id  # the same account, not a second

    rows = _users()
    assert len(rows) == 1
    # The hash is byte-for-byte what it was: a code adds a door, it does not
    # touch the lock that was already there.
    assert rows[0]["password_hash"] == stored
    login = client.post("/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == user_id

    me = client.get("/v1/me", headers=_bearer(resp.json()["token"])).json()
    assert me["auth_methods"] == ["password", "email"]


def test_an_existing_google_account_is_signed_into_and_keeps_its_sub(
    sandbox: SimpleNamespace,
) -> None:
    """A Google account signs in by code without its identity moving.

    The row is built directly rather than through a real ID token — that path
    has its own suite (test_google_auth.py). What matters here is that this
    flow reads an account it did not create and changes nothing about it.
    """
    from clipcatalyst_api import db

    created = db.create_user(
        uuid.uuid4().hex, email=EMAIL, password_hash="", google_sub=GOOGLE_SUB
    )
    assert created is not None

    resp = _sign_in(sandbox)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == created["id"]

    rows = _users()
    assert len(rows) == 1
    assert rows[0]["google_sub"] == GOOGLE_SUB  # untouched
    assert rows[0]["password_hash"] == ""  # still password-less
    me = sandbox.client.get("/v1/me", headers=_bearer(resp.json()["token"])).json()
    assert me["auth_methods"] == ["google", "email"]


# --------------------------------------------------------------------------- #
# 2. The attempt cap: five guesses, then the code is dead.
# --------------------------------------------------------------------------- #


def test_a_wrong_code_is_401_and_spends_one_attempt(sandbox: SimpleNamespace) -> None:
    assert _start(sandbox.client).status_code == 200
    real = sandbox.codes[-1]
    wrong = f"{(int(real) + 1) % 1_000_000:06d}"

    resp = _verify(sandbox.client, EMAIL, wrong)
    assert resp.status_code == 401, resp.text
    assert _code_rows()[0]["attempts"] == 1
    # The code itself survives a wrong guess — only the allowance shrank.
    assert _verify(sandbox.client, EMAIL, real).status_code == 200


def test_the_sixth_attempt_is_refused_and_the_row_is_gone(
    sandbox: SimpleNamespace,
) -> None:
    """Five guesses is the whole budget of a 6-digit secret.

    Without the cap, a million-wide space is a matter of patience at whatever
    rate the limiter allows. With it, one code is worth five guesses and then
    the user has to ask for another — which the per-address hourly cap meters
    in turn.
    """
    from clipcatalyst_api import auth

    assert _start(sandbox.client).status_code == 200
    real = sandbox.codes[-1]
    wrong = f"{(int(real) + 500_000) % 1_000_000:06d}"

    for attempt in range(1, auth.LOGIN_CODE_MAX_ATTEMPTS + 1):
        resp = _verify(sandbox.client, EMAIL, wrong)
        assert resp.status_code == 401, f"attempt {attempt}: {resp.text}"
        if attempt < auth.LOGIN_CODE_MAX_ATTEMPTS:
            assert _code_rows()[0]["attempts"] == attempt

    # The fifth wrong guess took the row with it, in the same transaction.
    assert _code_rows() == []
    # So the sixth attempt is refused — even holding the RIGHT code, which is
    # the point: an exhausted code is dead, not merely disadvantaged.
    sixth = _verify(sandbox.client, EMAIL, real)
    assert sixth.status_code == 401, sixth.text
    assert _users() == []  # nothing was created along the way


def test_every_refusal_is_the_same_401(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong, expired, exhausted and never-requested are indistinguishable.

    Telling them apart would rebuild the oracle the flow exists to avoid: "no
    code was requested for that address" names who is mid-sign-in, and "that
    code expired" confirms one was sent.
    """
    from clipcatalyst_api import auth

    # Nine verifications below, against a ten-a-minute client window: pin the
    # clock so the count is the test's rather than the wall clock's.
    monkeypatch.setattr(auth, "_current_minute", lambda: 22_700)
    client = sandbox.client
    bodies = set()

    # Never requested.
    never = _verify(client, "nobody@example.com", "123456")
    assert never.status_code == 401
    bodies.add(never.content)

    # Wrong.
    assert _start(client).status_code == 200
    real = sandbox.codes[-1]
    wrong = f"{(int(real) + 7) % 1_000_000:06d}"
    refused = _verify(client, EMAIL, wrong)
    assert refused.status_code == 401
    bodies.add(refused.content)

    # Expired.
    _expire_code(EMAIL)
    expired = _verify(client, EMAIL, real)
    assert expired.status_code == 401
    bodies.add(expired.content)

    # Exhausted.
    assert _start(client, OTHER_EMAIL).status_code == 200
    fresh = sandbox.codes[-1]
    other_wrong = f"{(int(fresh) + 7) % 1_000_000:06d}"
    for _ in range(5):
        assert _verify(client, OTHER_EMAIL, other_wrong).status_code == 401
    exhausted = _verify(client, OTHER_EMAIL, fresh)
    assert exhausted.status_code == 401
    bodies.add(exhausted.content)

    # Not merely equal — the same bytes, for all four.
    assert len(bodies) == 1, bodies


def test_a_code_shaped_like_nothing_is_the_same_401(sandbox: SimpleNamespace) -> None:
    """Not a 422 either: the model bounds the length and judges nothing else."""
    assert _start(sandbox.client).status_code == 200
    wrong = f"{(int(sandbox.codes[-1]) + 11) % 1_000_000:06d}"
    expected = _verify(sandbox.client, EMAIL, wrong).content
    for guess in ("not-a-code-at-all", "1", "０１２３４５", " 123456 ", "12345678"):
        resp = _verify(sandbox.client, EMAIL, guess)
        assert resp.status_code == 401, f"{guess!r}: {resp.text}"
        assert resp.content == expected


# --------------------------------------------------------------------------- #
# 3. The TTL, single use, and replacement.
# --------------------------------------------------------------------------- #


def test_an_expired_code_is_refused(sandbox: SimpleNamespace) -> None:
    assert _start(sandbox.client).status_code == 200
    _expire_code(EMAIL)
    resp = _verify(sandbox.client, EMAIL, sandbox.codes[-1])
    assert resp.status_code == 401, resp.text
    assert _users() == []


def test_the_ttl_comes_from_its_setting(sandbox: SimpleNamespace) -> None:
    """CC_EMAIL_CODE_TTL_MINUTES is the deadline, and the mail says the same."""
    from clipcatalyst_api import mailer
    from clipcatalyst_api.settings import get_settings

    _set_env(CC_EMAIL_CODE_TTL_MINUTES="2")
    try:
        before = datetime.now(timezone.utc)
        assert _start(sandbox.client).status_code == 200
        expires = datetime.fromisoformat(_code_rows()[0]["expires_at"])
        # `before` is read a request earlier, so the window is 2 minutes plus
        # however long that took — never the 10 the default would give.
        assert timedelta(minutes=1) < expires - before < timedelta(minutes=3)
        subject, body = mailer.login_code_message("123456", 2)
        assert "2 minutes" in body and "123456" in subject
    finally:
        _set_env(CC_EMAIL_CODE_TTL_MINUTES=None)
    # Back to the default: the mail cannot drift from what is enforced.
    assert get_settings().email_code_ttl_minutes == 10


def test_a_used_code_cannot_be_reused(sandbox: SimpleNamespace) -> None:
    first = _sign_in(sandbox)
    assert first.status_code == 200, first.text
    code = sandbox.codes[-1]

    replay = _verify(sandbox.client, EMAIL, code)
    assert replay.status_code == 401, replay.text
    assert len(_users()) == 1  # no second account, no second session minted


def test_a_second_start_invalidates_the_first_code(sandbox: SimpleNamespace) -> None:
    assert _start(sandbox.client).status_code == 200
    first = sandbox.codes[-1]
    assert _start(sandbox.client).status_code == 200
    second = sandbox.codes[-1]
    assert first != second
    # One row per address: the second code REPLACED the first.
    assert len(_code_rows()) == 1

    stale = _verify(sandbox.client, EMAIL, first)
    assert stale.status_code == 401, stale.text
    assert _verify(sandbox.client, EMAIL, second).status_code == 200


def test_a_new_code_restores_the_attempt_budget(sandbox: SimpleNamespace) -> None:
    """Guesses are spent against a SECRET, not against an address.

    Carrying them over would let anyone disable someone's code sign-in by
    burning five guesses and walking away.
    """
    assert _start(sandbox.client).status_code == 200
    wrong = f"{(int(sandbox.codes[-1]) + 3) % 1_000_000:06d}"
    for _ in range(3):
        assert _verify(sandbox.client, EMAIL, wrong).status_code == 401
    assert _code_rows()[0]["attempts"] == 3

    assert _start(sandbox.client).status_code == 200
    assert _code_rows()[0]["attempts"] == 0
    assert _verify(sandbox.client, EMAIL, sandbox.codes[-1]).status_code == 200


def test_a_code_for_one_address_never_verifies_another(
    sandbox: SimpleNamespace,
) -> None:
    """The digest is bound to the address, so a code is not a bearer token.

    It also means a leaked database is not a pile of live codes: a bare
    sha256 of six digits is a million-entry table anybody can precompute.
    """
    from clipcatalyst_api import auth

    assert _start(sandbox.client, EMAIL).status_code == 200
    mine = sandbox.codes[-1]
    assert _start(sandbox.client, OTHER_EMAIL).status_code == 200

    assert _verify(sandbox.client, OTHER_EMAIL, mine).status_code == 401
    assert auth.hash_login_code(EMAIL, mine) != auth.hash_login_code(OTHER_EMAIL, mine)
    # And the address's own code still works — the wrong guess above was
    # charged to the other row, not to this one.
    assert _verify(sandbox.client, EMAIL, mine).status_code == 200


# --------------------------------------------------------------------------- #
# 4. The two rate limits. They stop different attacks; neither substitutes.
# --------------------------------------------------------------------------- #


def test_the_per_address_hourly_cap_is_429(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody can be mail-bombed by a stranger typing their address."""
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    # Pin the window so a rollover mid-test cannot deflake the count. The hour
    # is derived from the minute, so this pins both.
    monkeypatch.setattr(auth, "_current_minute", lambda: 22_345)
    per_hour = get_settings().email_code_per_hour
    assert per_hour == 5

    for _ in range(per_hour):
        assert _start(sandbox.client).status_code == 200
    refused = _start(sandbox.client)
    assert refused.status_code == 429, refused.text
    # Spelled the other way, it is still the same address and the same bucket.
    assert _start(sandbox.client, " CREATOR@Example.com ").status_code == 429

    # Somebody else's address is untouched by it — the limit is per recipient,
    # so one person's cap can never lock another out.
    assert _start(sandbox.client, OTHER_EMAIL).status_code == 200

    # The refusal did not disturb the code already in flight.
    assert _verify(sandbox.client, EMAIL, sandbox.codes[-2]).status_code == 200


def test_the_per_address_cap_is_configurable(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipcatalyst_api import auth

    monkeypatch.setattr(auth, "_current_minute", lambda: 22_400)
    _set_env(CC_EMAIL_CODE_PER_HOUR="2")
    try:
        assert _start(sandbox.client).status_code == 200
        assert _start(sandbox.client).status_code == 200
        assert _start(sandbox.client).status_code == 429
    finally:
        _set_env(CC_EMAIL_CODE_PER_HOUR=None)


# Eighteen strings, ONE Gmail inbox. Every one of them is a spelling Gmail
# itself throws away on delivery, so a stranger who owns none of them can put
# eighteen pieces of mail in somebody's inbox — which is the mail-bombing the
# per-address cap exists to stop, and which it did not stop while it counted
# the identity form of the address.
_ONE_GMAIL_MAILBOX = (
    # Twelve sub-addresses (RFC 5233): Gmail strips everything from the `+`.
    *[f"alexcreator+{tag}@gmail.com" for tag in range(1, 13)],
    # Four placements of dots in the local part, which Gmail ignores.
    "a.lexcreator@gmail.com",
    "al.excreator@gmail.com",
    "alex.creator@gmail.com",
    "alexcreato.r@gmail.com",
    # Two root-anchored spellings of the same host — the trailing dot is DNS
    # punctuation, not part of the name.
    "alexcreator@gmail.com.",
    "alexcreator+late@gmail.com.",
)


def test_one_gmail_mailbox_cannot_be_bombed_by_respelling_the_address(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap counts an INBOX, so retyping the address does not buy more.

    The whole purpose EMAILAUTH.md gives this limit — "nobody can be mail-
    bombed by someone typing their address repeatedly" — is only true if the
    bucket is the mailbox. Keyed on the address as typed, one Gmail mailbox
    has unbounded distinct keys and the cap never fires once.
    """
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _Clock(380)
    monkeypatch.setattr(auth, "_current_minute", clock)
    per_hour = get_settings().email_code_per_hour
    assert per_hour == 5
    assert len(_ONE_GMAIL_MAILBOX) == 18

    # One inbox by deliverability; eighteen distinct strings by identity. Both
    # halves matter — see mailbox_key on why they are different questions.
    assert len({auth.mailbox_key(a) for a in _ONE_GMAIL_MAILBOX}) == 1
    assert len({auth.normalize_email(a) for a in _ONE_GMAIL_MAILBOX}) == 18

    statuses = []
    for address in _ONE_GMAIL_MAILBOX:
        clock.tick()  # a fresh CLIENT minute, so only the hour window is tested
        statuses.append(_start(sandbox.client, address).status_code)

    assert statuses[:per_hour] == [200] * per_hour
    assert set(statuses[per_hour:]) == {429}, statuses
    assert statuses.count(200) == per_hour
    # Five codes minted and five rows staged — not eighteen. The refusals cost
    # the attacker the send, not merely the response.
    assert len(sandbox.codes) == per_hour
    assert len(_code_rows()) == per_hour


def test_a_tag_a_dotted_local_part_and_a_trailing_dot_each_share_the_budget(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each folding rule on its own, against its own base address.

    Separate base mailboxes so no rule can pass on another's spent budget.
    """
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _Clock(381)
    monkeypatch.setattr(auth, "_current_minute", clock)
    per_hour = get_settings().email_code_per_hour

    for base, variant in (
        ("boxone@gmail.com", "boxone+newsletter@gmail.com"),
        ("boxtwo@gmail.com", "b.o.x.t.w.o@gmail.com"),
        ("boxthree@gmail.com", "boxthree@gmail.com."),
    ):
        assert auth.mailbox_key(variant) == auth.mailbox_key(base)
        for _ in range(per_hour):
            clock.tick()
            assert _start(sandbox.client, base).status_code == 200
        clock.tick()
        refused = _start(sandbox.client, variant)
        assert refused.status_code == 429, f"{variant} bought a fresh budget"


def test_googlemail_is_the_same_mailbox_as_gmail(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google's second name for one inbox, folded to the first."""
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _Clock(382)
    monkeypatch.setattr(auth, "_current_minute", clock)
    per_hour = get_settings().email_code_per_hour

    assert auth.mailbox_key("boxfour@googlemail.com") == "boxfour@gmail.com"
    # And the alias carries the other rules with it, in either spelling.
    assert auth.mailbox_key("b.ox.four+tag@googlemail.com.") == "boxfour@gmail.com"

    for _ in range(per_hour):
        clock.tick()
        assert _start(sandbox.client, "boxfour@googlemail.com").status_code == 200
    clock.tick()
    assert _start(sandbox.client, "boxfour@gmail.com").status_code == 429


def test_dots_are_significant_for_a_domain_that_has_not_said_otherwise(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-merging has a victim too, so the dot rule is an allow-list.

    `alex.smith@fastmail.com` and `alexsmith@fastmail.com` are two people's
    inboxes there. Folding them would let either one spend the other's hourly
    allowance and deny a stranger their sign-in mail — a denial of service
    built out of the anti-denial-of-service.
    """
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _Clock(383)
    monkeypatch.setattr(auth, "_current_minute", clock)
    per_hour = get_settings().email_code_per_hour
    dotted, undotted = "alex.smith@fastmail.com", "alexsmith@fastmail.com"
    assert auth.mailbox_key(dotted) != auth.mailbox_key(undotted)

    for _ in range(per_hour):
        clock.tick()
        assert _start(sandbox.client, dotted).status_code == 200
    clock.tick()
    assert _start(sandbox.client, dotted).status_code == 429

    # The other mailbox is untouched: a whole allowance of its own, and its
    # own refusal at the end of it.
    for _ in range(per_hour):
        clock.tick()
        assert _start(sandbox.client, undotted).status_code == 200
    clock.tick()
    assert _start(sandbox.client, undotted).status_code == 429


def test_mailbox_key_folds_deliverability_and_never_identity(
    sandbox: SimpleNamespace,
) -> None:
    """The two questions, side by side, on the same strings."""
    from clipcatalyst_api import auth

    cases = {
        "alex@gmail.com": "alex@gmail.com",
        "  Alex+Work@GMail.com  ": "alex@gmail.com",
        "a.l.e.x@gmail.com": "alex@gmail.com",
        "alex@gmail.com.": "alex@gmail.com",
        "a.l.e.x+tag@googlemail.com.": "alex@gmail.com",
        # Sub-addressing is stripped everywhere; dots are kept everywhere the
        # provider has not documented ignoring them.
        "alex.smith+news@fastmail.com": "alex.smith@fastmail.com",
        "alex.smith@fastmail.com.": "alex.smith@fastmail.com",
        # A local part that is nothing BUT a tag would fold to an empty key
        # and pool unrelated strings; it is left exactly as typed instead.
        "+promo@gmail.com": "+promo@gmail.com",
        # Two trailing dots is an empty DNS label — not a deliverable host,
        # and not this mailbox. Exactly one is the root-anchored spelling.
        "alex@gmail.com..": "alex@gmail.com.",
        # Never a parser: anything that is not address-shaped is its own key.
        "not-an-address": "not-an-address",
    }
    for typed, expected in cases.items():
        assert auth.mailbox_key(typed) == expected, typed

    # And none of that folding reaches identity, which still answers only to
    # case and whitespace.
    assert auth.normalize_email("  Alex+Work@GMail.com  ") == "alex+work@gmail.com"
    assert auth.normalize_email("a.l.e.x@gmail.com") == "a.l.e.x@gmail.com"
    assert auth.normalize_email("alex@googlemail.com") == "alex@googlemail.com"


def test_two_addresses_in_one_mailbox_are_still_two_separate_accounts(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety direction: folding stops at the counter.

    People use plus-addressing deliberately to keep separate accounts on one
    inbox. If the delivery key ever became the identity key, a code minted for
    one of those accounts would sign into the other — so `verify` must go on
    resolving the address exactly as typed.
    """
    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _Clock(384)
    monkeypatch.setattr(auth, "_current_minute", clock)
    client = sandbox.client
    work, personal = "alex+work@gmail.com", "alex@gmail.com"
    assert auth.mailbox_key(work) == auth.mailbox_key(personal)  # one inbox
    assert auth.normalize_email(work) != auth.normalize_email(personal)  # two rows

    assert _start(client, work).status_code == 200
    work_code = sandbox.codes[-1]
    # The code was minted for ONE address: the other has no row to read and no
    # digest this code could match, so it is the same generic 401 as ever.
    assert _verify(client, personal, work_code).status_code == 401
    assert _users() == []

    signed_in_work = _verify(client, work, work_code)
    assert signed_in_work.status_code == 200, signed_in_work.text
    assert signed_in_work.json()["user"]["email"] == work

    clock.tick()
    assert _start(client, personal).status_code == 200
    signed_in_personal = _verify(client, personal, sandbox.codes[-1])
    assert signed_in_personal.status_code == 200, signed_in_personal.text
    assert signed_in_personal.json()["user"]["email"] == personal
    assert (
        signed_in_personal.json()["user"]["id"] != signed_in_work.json()["user"]["id"]
    )
    assert sorted(row["email"] for row in _users()) == sorted([work, personal])

    # Two accounts, but one inbox and therefore one allowance: those two starts
    # came out of the same five.
    for _ in range(get_settings().email_code_per_hour - 2):
        clock.tick()
        assert _start(client, work).status_code == 200
    clock.tick()
    assert _start(client, personal).status_code == 429


def test_the_per_client_limiter_applies_to_both_routes(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One machine cannot farm codes, nor grind verifications.

    Distinct addresses on every start, so what is being measured here is the
    CLIENT window and not the per-address one — the two limits are independent
    and this proves the first of them exists on its own.
    """
    from clipcatalyst_api import auth

    monkeypatch.setattr(auth, "_current_minute", lambda: 22_500)
    client = sandbox.client

    for i in range(auth.RATE_LIMIT_PER_MINUTE):
        assert _start(client, f"creator-{i}@example.com").status_code == 200
    spent = _start(client, "creator-late@example.com")
    assert spent.status_code == 429, spent.text

    # `verify` has its own window — it is a different route — and is metered
    # exactly the same way. The attempt cap only ever protects ONE code; this
    # is what stops five guesses each against code after code, which is how a
    # million-wide space would otherwise be walked.
    for _ in range(auth.RATE_LIMIT_PER_MINUTE):
        assert _verify(client, "no-code-here@example.com", "000000").status_code == 401
    assert _verify(client, "no-code-here@example.com", "000000").status_code == 429
    # Even a VALID code is refused once the window is spent: the limiter counts
    # attempts, not failures.
    assert _verify(client, "creator-0@example.com", sandbox.codes[0]).status_code == 429

    # A new minute clears it, and the real code then works.
    monkeypatch.setattr(auth, "_current_minute", lambda: 22_501)
    assert _verify(client, "creator-0@example.com", sandbox.codes[0]).status_code == 200


def test_the_two_limiters_count_in_separate_tables(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minute window and an hour window must never be counted as each other.

    They live in separate in-process arrays for exactly this reason: one table
    holding both kinds of window number would have each silently resetting the
    other's count — an error in the direction that ADMITS more.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    monkeypatch.setattr(auth, "_current_minute", lambda: 22_600)
    # Spend the CLIENT window completely, on the very route the address limit
    # also guards.
    for _ in range(auth.RATE_LIMIT_PER_MINUTE):
        auth.enforce_rate_limit("198.51.100.9", "email-start")
    with pytest.raises(HTTPException) as spent:
        auth.enforce_rate_limit("198.51.100.9", "email-start")
    assert spent.value.status_code == 429

    # The per-address counter cannot see any of that: it has its own table,
    # its own window, and its own allowance, all of which are intact.
    for _ in range(get_settings().email_code_per_hour):
        auth.enforce_email_code_limit(EMAIL)
    with pytest.raises(HTTPException) as refused:
        auth.enforce_email_code_limit(EMAIL)
    assert refused.value.status_code == 429
    # And the client window is still exactly as spent as it was — neither
    # limiter reset the other.
    with pytest.raises(HTTPException):
        auth.enforce_rate_limit("198.51.100.9", "email-start")


def test_the_per_address_limit_counts_in_redis_on_a_key_of_its_own(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path: an hour-long key under its own prefix.

    Prefix and window both differ from the per-client counter, so the two can
    never collide in Redis either — the separation is not only an in-process
    detail. And the key expires itself, so a finished hour is Redis's to
    reclaim rather than something a sweep has to find.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth

    monkeypatch.setattr(auth, "_current_minute", lambda: 22_800)
    hour = 22_800 // 60
    counts: dict[str, int] = {}
    ttls: dict[str, int] = {}

    class _FakeRedis:
        def eval(self, script: str, numkeys: int, key: str, ttl: int) -> int:
            assert numkeys == 1, numkeys
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1:
                ttls[key] = int(ttl)
            return counts[key]

    fake = _FakeRedis()

    def _stub_client(url: str) -> _FakeRedis:
        assert url, "the limiter must build its client from CC_REDIS_URL"
        return fake

    _stub_client.cache_clear = lambda: None  # type: ignore[attr-defined]
    real_client, auth._redis_client = auth._redis_client, _stub_client
    _set_env(CC_QUEUE="redis")
    try:
        for _ in range(5):
            auth.enforce_email_code_limit(EMAIL)
        with pytest.raises(HTTPException) as refused:
            auth.enforce_email_code_limit(EMAIL)
        assert refused.value.status_code == 429

        key = f"cc:rlh:email-code:{hour}:{EMAIL}"
        assert list(counts) == [key], counts
        assert counts[key] == 6
        assert ttls == {key: auth._HOUR_TTL_S}
        # Nothing landed in the per-client table or its prefix.
        assert set(auth._memory_hours) == {(-1, 0)}
        assert not any(k.startswith("cc:rl:") for k in counts)
    finally:
        auth._redis_client = real_client
        _set_env(CC_QUEUE="eager")
        auth.reset_rate_limits()


def test_an_unreachable_redis_refuses_the_code_by_default(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that cannot be counted is a start that is not limited.

    Same trade as the per-client limiter, and the operator can take the other
    side of it in writing (CC_RATE_LIMIT_FAIL_OPEN).
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth

    monkeypatch.setattr(auth, "_current_minute", lambda: 22_900)

    class _DeadRedis:
        def eval(self, *args: object) -> int:
            raise ConnectionError("connection refused")

    def _stub_client(url: str) -> _DeadRedis:
        return _DeadRedis()

    _stub_client.cache_clear = lambda: None  # type: ignore[attr-defined]
    real_client, auth._redis_client = auth._redis_client, _stub_client
    _set_env(CC_QUEUE="redis")
    try:
        with pytest.raises(HTTPException) as refused:
            auth.enforce_email_code_limit(EMAIL)
        assert refused.value.status_code == 429

        _set_env(CC_RATE_LIMIT_FAIL_OPEN="on")
        for _ in range(20):
            auth.enforce_email_code_limit(EMAIL)  # taken knowingly
    finally:
        auth._redis_client = real_client
        _set_env(CC_QUEUE="eager", CC_RATE_LIMIT_FAIL_OPEN=None)
        auth.reset_rate_limits()


# --------------------------------------------------------------------------- #
# 5. Enumeration: `start` says the same thing about everybody.
# --------------------------------------------------------------------------- #


def test_start_is_byte_identical_for_known_and_unknown_addresses(
    sandbox: SimpleNamespace,
) -> None:
    """The anti-enumeration property, asserted on the bytes themselves.

    It holds because the route never LOOKS the address up — the account is
    resolved at verify time — so there is no branch to take and nothing to
    time. This asserts the observable half of that.
    """
    client = sandbox.client
    _register(client, EMAIL)

    known = _start(client, EMAIL)
    unknown = _start(client, "no-such-person@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.content == unknown.content == b'{"sent":true}'
    assert known.headers["content-type"] == unknown.headers["content-type"]
    assert known.headers["cache-control"] == unknown.headers["cache-control"]

    # A Google account, a password-less account and a brand-new address all
    # answer the same way too.
    from clipcatalyst_api import db

    db.create_user(
        uuid.uuid4().hex, email=OTHER_EMAIL, password_hash="", google_sub=GOOGLE_SUB
    )
    assert _start(client, OTHER_EMAIL).content == known.content


def test_an_unsendable_address_is_refused_without_touching_anything(
    sandbox: SimpleNamespace,
) -> None:
    """A 400 about the STRING is not a 400 about an account."""
    for bad in ("not-an-email", "@example.com", "creator@", "a b@example.com"):
        resp = _start(sandbox.client, bad)
        assert resp.status_code == 400, f"{bad!r}: {resp.text}"
    assert _code_rows() == []
    assert _users() == []


# --------------------------------------------------------------------------- #
# 6. The mailer: off, console, resend — and a failed send is never a success.
# --------------------------------------------------------------------------- #


def test_mailer_none_is_503_and_writes_no_row(sandbox: SimpleNamespace) -> None:
    _set_env(CC_MAILER="none")
    try:
        resp = _start(sandbox.client)
        assert resp.status_code == 503, resp.text
        assert "CC_MAILER" in resp.json()["detail"]  # honest about why
        # Nothing was staged, nothing was minted, nobody was told to wait for
        # an email that was never going to arrive.
        assert _code_rows() == []
        assert sandbox.codes == []
        assert _users() == []
    finally:
        _set_env(CC_MAILER="console")
    assert _start(sandbox.client).status_code == 200


def test_the_console_mailer_is_the_only_path_that_renders_a_code(
    sandbox: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """One place in the system prints a code — and it announces itself.

    The console backend IS the log, and it exists so a developer with no mail
    provider can finish the flow. Every other path treats the code as
    something that must not be written down.
    """
    with caplog.at_level(logging.INFO):
        assert _start(sandbox.client).status_code == 200
    console_code = sandbox.codes[-1]
    printed = "\n".join(record.getMessage() for record in caplog.records)
    assert console_code in printed
    assert "CC_MAILER=console" in printed

    caplog.clear()
    _set_env(CC_MAILER="resend", CC_RESEND_API_KEY="re_test_key")
    try:
        with caplog.at_level(logging.INFO):
            assert _start(sandbox.client, OTHER_EMAIL).status_code == 200
        resend_code = sandbox.codes[-1]
        # It went to the provider…
        assert resend_code in sandbox.sent[-1]["payload"]["text"]
        # …and nowhere near the log.
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert resend_code not in logged
    finally:
        _set_env(CC_MAILER="console", CC_RESEND_API_KEY=None)


def test_the_resend_mailer_sends_the_message_it_should(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import mailer

    _set_env(CC_MAILER="resend", CC_RESEND_API_KEY="re_test_key")
    try:
        assert _start(sandbox.client).status_code == 200
        code = sandbox.codes[-1]
        assert len(sandbox.sent) == 1
        call = sandbox.sent[0]
        assert call["url"] == mailer.RESEND_URL
        assert call["headers"]["Authorization"] == "Bearer re_test_key"
        payload = call["payload"]
        assert payload["from"] == "ClipCatalyst <onboarding@resend.dev>"
        assert payload["to"] == [EMAIL]
        # The code rides in the SUBJECT as well, so a phone notification is
        # enough to read it without opening the mail.
        assert code in payload["subject"]
        assert code in payload["text"]
        assert "10 minutes" in payload["text"]
        assert "will ever ask you for this code" in payload["text"]
        # And the code that was mailed is the code that works.
        assert _verify(sandbox.client, EMAIL, code).status_code == 200
    finally:
        _set_env(CC_MAILER="console", CC_RESEND_API_KEY=None)


def test_a_refused_send_is_a_503_and_leaves_no_live_code(
    sandbox: SimpleNamespace,
) -> None:
    """A user staring at an inbox that will never fill is worse than an error."""
    _set_env(CC_MAILER="resend", CC_RESEND_API_KEY="re_test_key")
    try:
        sandbox.reply["status"] = 422
        sandbox.reply["body"] = '{"message":"domain is not verified"}'
        resp = _start(sandbox.client)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"] != ""
        # The staged row was rolled back: no credential exists that nobody can
        # receive, and the address is not left holding a dead code.
        assert _code_rows() == []
        assert _users() == []
        # The code that was minted is worth nothing.
        assert _verify(sandbox.client, EMAIL, sandbox.codes[-1]).status_code == 401

        # A transport failure — DNS, TLS, a timeout — is the same answer.
        sandbox.reply["raise"] = OSError("connection refused")
        assert _start(sandbox.client).status_code == 503
        assert _code_rows() == []

        # And once the provider answers again, the flow works unchanged.
        sandbox.reply["raise"] = None
        sandbox.reply["status"] = 200
        assert _start(sandbox.client).status_code == 200
        assert _verify(sandbox.client, EMAIL, sandbox.codes[-1]).status_code == 200
    finally:
        _set_env(CC_MAILER="console", CC_RESEND_API_KEY=None)


def test_resend_without_a_key_is_a_503_not_a_silent_success(
    sandbox: SimpleNamespace,
) -> None:
    _set_env(CC_MAILER="resend", CC_RESEND_API_KEY=None)
    try:
        resp = _start(sandbox.client)
        assert resp.status_code == 503, resp.text
        assert sandbox.sent == []  # nothing was even attempted
        assert _code_rows() == []
    finally:
        _set_env(CC_MAILER="console")


def test_an_unknown_mailer_fails_loudly_rather_than_silently_off(
    sandbox: SimpleNamespace,
) -> None:
    """`CC_MAILER=resnd` is a typo, and a typo must not read as "email is off".

    Reading it as off would look exactly like a box that never wanted mail —
    the frontend would drop the option and nobody would ever find out.
    """
    from clipcatalyst_api import mailer
    from clipcatalyst_api.settings import get_settings

    _set_env(CC_MAILER="resnd")
    try:
        assert mailer.is_configured(get_settings()) is True
        resp = _start(sandbox.client)
        assert resp.status_code == 503, resp.text
        assert _code_rows() == []
    finally:
        _set_env(CC_MAILER="console")


def test_a_provider_reply_that_echoes_the_code_is_redacted(
    sandbox: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing a provider says can put a code in our log.

    No provider we know of echoes the request back in an error — but "nobody
    does that today" is not something this code can enforce, and a code in a
    log file outlives the ten minutes it was meant to exist for. So the reply
    is redacted before it is quoted, and this is a provider that does the
    worst thing.
    """
    from clipcatalyst_api import mailer

    def _echo(url: str, payload: dict, headers: dict) -> tuple[int, str]:
        return 400, f'{{"error":"rejected","subject":"{payload["subject"]}"}}'

    monkeypatch.setattr(mailer, "post_json", _echo)
    _set_env(CC_MAILER="resend", CC_RESEND_API_KEY="re_test_key")
    try:
        with caplog.at_level(logging.INFO):
            assert _start(sandbox.client).status_code == 503
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert sandbox.codes[-1] not in logged
        assert "[code redacted]" in logged
    finally:
        _set_env(CC_MAILER="console", CC_RESEND_API_KEY=None)


# --------------------------------------------------------------------------- #
# 7. Storage: what is on disk, and what is swept.
# --------------------------------------------------------------------------- #


def test_the_plaintext_code_appears_nowhere_in_the_database_file(
    sandbox: SimpleNamespace,
) -> None:
    """The strongest form of "never stored in plaintext": grep the file.

    Every page of the database, the write-ahead log included, so a code cannot
    be hiding in a page that has not been checkpointed yet.
    """
    from clipcatalyst_api import auth, db

    assert _start(sandbox.client).status_code == 200
    assert _start(sandbox.client, OTHER_EMAIL).status_code == 200
    codes = list(sandbox.codes)
    assert len(codes) == 2

    with contextlib.closing(db._connect()) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    db_path = sandbox.data_dir / "jobs.sqlite3"
    raw = b"".join(
        path.read_bytes()
        for path in sorted(sandbox.data_dir.glob("jobs.sqlite3*"))
        if path.is_file()
    )
    assert raw, "the database file should not be empty"
    for code in codes:
        assert code.encode("ascii") not in raw, "a code is on disk in plaintext"
    # What IS on disk is the digest, and it is the one the route compares.
    stored = {row["email"]: row["code_hash"] for row in _code_rows()}
    assert stored[EMAIL] == auth.hash_login_code(EMAIL, codes[0])
    assert stored[EMAIL].encode("ascii") in raw
    assert db_path.is_file()


def test_the_hourly_sweep_deletes_expired_codes(sandbox: SimpleNamespace) -> None:
    """Hygiene, not enforcement: they had already stopped working."""
    from clipcatalyst_api import worker

    assert _start(sandbox.client).status_code == 200
    assert _start(sandbox.client, OTHER_EMAIL).status_code == 200
    _expire_code(EMAIL)

    # The expired one is already refused, sweep or no sweep.
    assert _verify(sandbox.client, EMAIL, sandbox.codes[0]).status_code == 401

    assert worker.purge_expired_login_codes() == 1
    assert [row["email"] for row in _code_rows()] == [OTHER_EMAIL]
    # The live one is untouched and still works.
    assert _verify(sandbox.client, OTHER_EMAIL, sandbox.codes[1]).status_code == 200
    assert worker.purge_expired_login_codes() == 0


def test_the_table_survives_a_database_that_predates_it(
    sandbox: SimpleNamespace,
) -> None:
    """A guarded CREATE, so an existing deployment gains the table in place."""
    from clipcatalyst_api import db

    _register(sandbox.client, EMAIL)
    with contextlib.closing(db._connect()) as conn:
        conn.execute("DROP TABLE login_codes")
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'login_codes'"
            ).fetchone()
            is None
        )

    # The next call through the DAO recreates it, and the account that was
    # already there is signed into rather than duplicated.
    resp = _sign_in(sandbox)
    assert resp.status_code == 200, resp.text
    assert len(_users()) == 1


# --------------------------------------------------------------------------- #
# 8. The generator: a credential, not a number.
# --------------------------------------------------------------------------- #


def test_codes_are_six_digits_from_the_whole_space(sandbox: SimpleNamespace) -> None:
    """Zero-padded, six wide, and not obviously a counter.

    The padding is security, not formatting: printing the integer plainly
    would leak its magnitude in the length and shrink the space to the 900_000
    numbers that happen to have six digits.
    """
    from clipcatalyst_api import auth

    codes = [auth.new_login_code() for _ in range(2000)]
    assert all(len(code) == auth.LOGIN_CODE_DIGITS for code in codes)
    assert all(code.isdigit() for code in codes)
    # Distinct enough to rule out a constant or a counter; the CSPRNG itself
    # is the stdlib's problem, not this suite's.
    assert len(set(codes)) > 1900
    assert any(code.startswith("0") for code in codes), "the space includes 0…"
