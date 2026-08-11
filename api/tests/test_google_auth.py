"""Sign in with Google (LIBRARY.md Part 1): real RS256 against a stub JWKS.

Nothing here is mocked away. Every token is a real JWT, signed with an RSA
key this module generates, and the verifier checks it against a key set built
from that key's public half — so a bad signature fails because the maths
says so, an unknown `kid` fails because the key genuinely is not in the set,
and the "valid" cases prove the whole path works rather than proving a patch
returned what it was told to. The ONLY thing replaced is
``googleid.fetch_jwks``, the one function that would otherwise reach out to
Google over the network.

Mirrors the env/import dance of ``test_auth.py`` — all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules (queue_app / worker / main,
plus auth for its rate-limit windows and googleid for its key cache) are
purged for a clean re-import. The whole os.environ is snapshotted and restored
around each client.

No pipeline runs here: sign-in touches the users table and nothing else, so
there is no video and no transcriber to stand up.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import time
from types import SimpleNamespace
from typing import Iterator

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# The `aud` every token below is minted for, and the one the server is
# configured with. A token for anybody else's client id is somebody else's
# token, which is the whole point of checking it.
CLIENT_ID = "1234567890-clipcatalyst.apps.googleusercontent.com"
KID = "cc-test-key-1"

GOOGLE_SUB = "104729000000000000042"  # Google's immutable account id
GOOGLE_EMAIL = "creator@gmail.com"
PASSWORD = "correct-horse-battery"

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
    "clipcatalyst_api.auth",
    "clipcatalyst_api.googleid",
)


def _purge() -> None:
    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _set_client_id(value: str | None) -> None:
    """Flip CC_GOOGLE_CLIENT_ID at runtime; the route reads get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    if value is None:
        os.environ.pop("CC_GOOGLE_CLIENT_ID", None)
    else:
        os.environ["CC_GOOGLE_CLIENT_ID"] = value
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Minting ID tokens: the same shape Google Identity Services hands the browser.
# --------------------------------------------------------------------------- #


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_int(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    """One entry of a JWK Set, exactly as Google publishes its own."""
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64u_int(numbers.n),
        "e": _b64u_int(numbers.e),
    }


def _sign(
    key: rsa.RSAPrivateKey,
    *,
    kid: str = KID,
    tamper: bool = False,
    **overrides: object,
) -> str:
    """A signed RS256 ID token: valid by default, broken exactly as asked.

    `overrides` replace individual claims (`aud`, `iss`, `exp`, `nbf`,
    `email_verified`, …); `tamper` flips a bit of the signature AFTER it is
    computed, which is the one failure that cannot be faked by editing claims.
    """
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": GOOGLE_SUB,
        "email": GOOGLE_EMAIL,
        "email_verified": True,
        "name": "A Creator",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    signing_input = ".".join(
        (
            _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        )
    ).encode("ascii")
    signature = bytearray(key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    if tamper:
        signature[-1] ^= 0x01
    return f"{signing_input.decode('ascii')}.{_b64u(bytes(signature))}"


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    """The 'Google' key for this run — generated here, never leaves the process."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def sandbox(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[SimpleNamespace]:
    """A TestClient with Google sign-in configured and a stub key set served.

    `served` is mutable so a test can rotate what "Google" publishes, and
    `fetches` records every trip the verifier would have made to the network.
    """
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("googledata")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": "",
            "CC_BILLING": "off",
            "CC_GOOGLE_CLIENT_ID": CLIENT_ID,
        }
    )
    os.environ.pop("CC_API_TOKEN", None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth, googleid
    from clipcatalyst_api.main import app

    served: dict = {"keys": [_jwk(signing_key, KID)]}
    fetches: list[str] = []

    def _stub_fetch(url: str = googleid.JWKS_URL) -> dict:
        fetches.append(url)
        # A copy, like a real response body: the verifier must not be able to
        # keep a live handle on what the test mutates next.
        return json.loads(json.dumps(served))

    monkeypatch.setattr(googleid, "fetch_jwks", _stub_fetch)
    googleid.reset_keys_cache()
    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                data_dir=data_dir,
                served=served,
                fetches=fetches,
                fetch=_stub_fetch,
            )
    finally:
        auth.reset_rate_limits()
        googleid.reset_keys_cache()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _google(client, id_token: str):
    return client.post("/v1/auth/google", json={"id_token": id_token})


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


# --------------------------------------------------------------------------- #
# 1. The happy paths: create, sign in again, link.
# --------------------------------------------------------------------------- #


def test_valid_token_creates_a_password_less_account(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    resp = _google(client, _sign(signing_key))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"  # carries a session token

    body = resp.json()
    assert body["token"].startswith("cc_sess_")
    assert body["user"]["email"] == GOOGLE_EMAIL
    assert body["user"]["plan"] == "free"

    # The session is an ordinary session: it works on every gated route.
    me = client.get("/v1/me", headers=_bearer(body["token"]))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == GOOGLE_EMAIL
    assert me.json()["auth_methods"] == ["google"]

    rows = _users()
    assert len(rows) == 1
    assert rows[0]["google_sub"] == GOOGLE_SUB
    # Password-less, and stored as such: NOT a hash of the empty string.
    assert rows[0]["password_hash"] == ""


def test_second_sign_in_reuses_the_same_account(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    first = _google(client, _sign(signing_key))
    assert first.status_code == 200, first.text

    # A second token for the same Google identity — a fresh `iat`/`exp`, and
    # an email Google has since changed the case of.
    second = _google(client, _sign(signing_key, email="Creator@Gmail.com"))
    assert second.status_code == 200, second.text

    assert second.json()["user"]["id"] == first.json()["user"]["id"]
    assert second.json()["token"] != first.json()["token"]  # distinct sessions
    assert len(_users()) == 1  # matched on `sub`, not re-created

    # Both sessions are live at once, exactly like two password logins.
    for token in (first.json()["token"], second.json()["token"]):
        assert client.get("/v1/me", headers=_bearer(token)).status_code == 200


def test_verified_email_links_to_an_existing_password_account(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    registered = _register(client, GOOGLE_EMAIL)
    user_id = registered["user"]["id"]
    assert _users()[0]["google_sub"] == ""

    resp = _google(client, _sign(signing_key))
    assert resp.status_code == 200, resp.text
    # Linked, not forked: same row, now carrying the Google identity too.
    assert resp.json()["user"]["id"] == user_id
    rows = _users()
    assert len(rows) == 1
    assert rows[0]["google_sub"] == GOOGLE_SUB

    me = client.get("/v1/me", headers=_bearer(resp.json()["token"])).json()
    assert me["auth_methods"] == ["password", "google"]

    # Linking adds a way in; it never takes the password away.
    login = client.post(
        "/v1/auth/login", json={"email": GOOGLE_EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == user_id


def test_linking_matches_the_email_however_google_spells_it(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    """The stored address is normalized, so the match is on identity, not case."""
    client = sandbox.client
    user_id = _register(client, " Creator@GMAIL.com ")["user"]["id"]
    resp = _google(client, _sign(signing_key, email="CREATOR@gmail.com"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == user_id
    assert len(_users()) == 1


# --------------------------------------------------------------------------- #
# 2. The security boundary: an unverified email links to nothing, ever.
# --------------------------------------------------------------------------- #


def test_unverified_email_is_refused_and_touches_nothing(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    _register(client, GOOGLE_EMAIL)
    before = _users()

    for claim in (False, "false", "", None, 0, "true-ish"):
        resp = _google(client, _sign(signing_key, email_verified=claim))
        assert resp.status_code == 401, f"email_verified={claim!r}: {resp.text}"
    # Not linked, not created, not touched — a matched email is only ever
    # trusted because Google says it verified it.
    assert _users() == before


# --------------------------------------------------------------------------- #
# 3. Everything a forged or stale token can be: one 401, no rows moved.
# --------------------------------------------------------------------------- #


def test_bad_tokens_are_401_and_leave_the_user_table_alone(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    _register(client, GOOGLE_EMAIL)
    before = _users()
    now = int(time.time())

    # A key that is NOT in the served set: a token signed by anybody else.
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    cases = {
        # Somebody else's client id — a token minted for another site's
        # Google app, replayed at ours.
        "wrong aud": _sign(signing_key, aud="9999-someone-else.apps.googleusercontent.com"),
        "wrong iss": _sign(signing_key, iss="https://accounts.evil.example"),
        # Past the 60 s skew on both sides.
        "expired": _sign(signing_key, iat=now - 7200, exp=now - 120),
        "nbf in the future": _sign(signing_key, nbf=now + 3600),
        "bad signature": _sign(signing_key, tamper=True),
        "unknown kid": _sign(signing_key, kid="never-published"),
        # Signed by a real RSA key that Google never published: the `kid` is
        # one we serve, so the set is found and the signature is what fails.
        "wrong key": _sign(impostor),
        "not a jwt": "this-is-not-a-token",
    }
    details = set()
    for name, token in cases.items():
        resp = _google(client, token)
        assert resp.status_code == 401, f"{name}: {resp.status_code} {resp.text}"
        details.add(resp.json()["detail"])
    # One refusal, byte-identical for all of them: which check failed is a
    # server-log fact and never a hint to whoever sent the token.
    assert len(details) == 1, details

    assert _users() == before


def test_a_rotated_signing_key_is_picked_up_by_the_kid_miss_refresh(
    sandbox: SimpleNamespace,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown `kid` refetches the key set instead of refusing on stale keys."""
    from clipcatalyst_api import googleid

    client = sandbox.client
    assert _google(client, _sign(signing_key)).status_code == 200
    warm = len(sandbox.fetches)
    assert warm == 1  # the set is cached, not fetched per request

    # Google rotates: a new key under a new id, the old one withdrawn.
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sandbox.served["keys"] = [_jwk(rotated, "cc-test-key-2")]

    # The refresh floor is what stops a flood of forged `kid`s from turning
    # into a flood of requests to Google; a real rotation is seen minutes
    # later, which is what pinning it to zero stands in for here (the same
    # trick test_auth.py plays on auth._current_minute).
    monkeypatch.setattr(googleid, "_MIN_REFRESH_INTERVAL_S", 0)

    resp = _google(client, _sign(rotated, kid="cc-test-key-2"))
    assert resp.status_code == 200, resp.text
    assert len(sandbox.fetches) > warm  # it went and looked, rather than refusing
    # And the old key really is gone — tokens signed with it now fail.
    assert _google(client, _sign(signing_key)).status_code == 401


def test_the_key_set_is_not_refetched_for_every_forged_kid(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    """A stream of made-up `kid`s costs Google one fetch, not one per token."""
    client = sandbox.client
    for i in range(5):
        assert _google(client, _sign(signing_key, kid=f"forged-{i}")).status_code == 401
    assert len(sandbox.fetches) == 1


# --------------------------------------------------------------------------- #
# 4. Deployment state: no client id configured is a 503, not a refusal.
# --------------------------------------------------------------------------- #


def test_google_sign_in_is_503_without_a_client_id(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    _set_client_id(None)
    try:
        # A perfectly good token: the server still cannot say what `aud` it
        # would accept, so it says so honestly instead of blaming the caller.
        resp = _google(client, _sign(signing_key))
        assert resp.status_code == 503, resp.text
        assert _users() == []
        assert sandbox.fetches == []  # nothing was verified, nothing fetched
    finally:
        _set_client_id(CLIENT_ID)
    assert _google(client, _sign(signing_key)).status_code == 200


def test_unreachable_google_keys_are_a_503_not_a_refusal(
    sandbox: SimpleNamespace,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key set we cannot reach is a 503 — never somebody's sign-in refused."""
    from clipcatalyst_api import googleid

    client = sandbox.client

    def _unreachable(url: str = googleid.JWKS_URL) -> dict:
        raise OSError("connection refused")

    monkeypatch.setattr(googleid, "fetch_jwks", _unreachable)
    resp = _google(client, _sign(signing_key))
    assert resp.status_code == 503, resp.text
    assert _users() == []  # a good token was not turned into a bad one

    # Once a key set HAS been fetched, Google going away stops nobody: the
    # keys rotate slowly, and the cached ones are still the right answer.
    monkeypatch.setattr(googleid, "fetch_jwks", sandbox.fetch)
    assert _google(client, _sign(signing_key)).status_code == 200
    monkeypatch.setattr(googleid, "fetch_jwks", _unreachable)
    assert _google(client, _sign(signing_key)).status_code == 200


# --------------------------------------------------------------------------- #
# 5. The rate limiter: a forged-token flood is cheap to send, expensive to
#    refuse, so /v1/auth/google is metered like every other credential route.
# --------------------------------------------------------------------------- #


def test_rate_limit_returns_429_on_the_google_route(
    sandbox: SimpleNamespace,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clipcatalyst_api import auth

    # Pin the window so a minute rollover mid-test can't deflake the count.
    monkeypatch.setattr(auth, "_current_minute", lambda: 22345)
    client = sandbox.client
    forged = _sign(signing_key, aud="somebody-else.apps.googleusercontent.com")

    for _ in range(auth.RATE_LIMIT_PER_MINUTE):
        assert _google(client, forged).status_code == 401
    assert _google(client, forged).status_code == 429
    # Even a VALID token is refused once the window is spent — the limiter
    # counts attempts, not failures.
    assert _google(client, _sign(signing_key)).status_code == 429

    # A new minute clears it, and the valid token then works.
    monkeypatch.setattr(auth, "_current_minute", lambda: 22346)
    assert _google(client, _sign(signing_key)).status_code == 200


# --------------------------------------------------------------------------- #
# 6. The other half of a password-less account: it cannot be password-logged
#    into, and the empty hash is refused rather than compared against.
# --------------------------------------------------------------------------- #


def test_a_password_less_account_cannot_use_password_login(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    client = sandbox.client
    created = _google(client, _sign(signing_key))
    assert created.status_code == 200, created.text
    assert _users()[0]["password_hash"] == ""

    # An empty password never even reaches the route — LoginRequest requires
    # a non-empty one (422) — and, either way, no session comes back.
    empty = client.post("/v1/auth/login", json={"email": GOOGLE_EMAIL, "password": ""})
    assert empty.status_code == 422
    assert "token" not in empty.json()

    # And no other password works either, including the strings that a naive
    # comparison against an empty stored hash might let through.
    for password in (" ", "scrypt$16384$8$1$$", "$", "hunter2hunter2", PASSWORD):
        resp = client.post(
            "/v1/auth/login", json={"email": GOOGLE_EMAIL, "password": password}
        )
        assert resp.status_code == 401, f"{password!r}: {resp.text}"
        # The same generic refusal an unknown email gets — a password-less
        # account is not advertised as one by the login endpoint.
        unknown = client.post(
            "/v1/auth/login",
            json={"email": "nobody@example.com", "password": password},
        )
        assert resp.json() == unknown.json()

    # Google still signs the account in — nothing above damaged it.
    assert _google(client, _sign(signing_key)).status_code == 200


# --------------------------------------------------------------------------- #
# 7. The schema: a guarded ALTER for databases that predate this feature, and
#    a UNIQUE index that is PARTIAL because '' means "no Google identity".
# --------------------------------------------------------------------------- #


def test_an_existing_database_gains_google_sub_in_place(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    from clipcatalyst_api import auth, db

    client = sandbox.client
    stored = auth.hash_password(PASSWORD)
    # Rebuild `users` in its pre-Google shape, with an account already in it.
    with contextlib.closing(db._connect()) as conn:
        conn.execute("DROP TABLE users")
        conn.execute(
            "CREATE TABLE users ("
            " id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
            " password_hash TEXT NOT NULL, created_at TEXT NOT NULL,"
            " stripe_customer_id TEXT NOT NULL DEFAULT '',"
            " plan TEXT NOT NULL DEFAULT 'free',"
            " plan_status TEXT NOT NULL DEFAULT '',"
            " current_period_end TEXT NOT NULL DEFAULT '',"
            " brand_logo_path TEXT NOT NULL DEFAULT '',"
            " brand_caption_color TEXT NOT NULL DEFAULT '',"
            " brand_show_logo INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at)"
            " VALUES ('legacy-1', ?, ?, '2026-01-01T00:00:00.000+00:00')",
            (GOOGLE_EMAIL, stored),
        )

    # Reading it applies the guarded ALTER; the pre-existing row reads as "no
    # Google identity", which is exactly what it is.
    upgraded = db.get_user_by_email(GOOGLE_EMAIL)
    assert upgraded is not None
    assert upgraded["google_sub"] == ""

    # And the old account behaves like any other: it links on a verified
    # email, and its password keeps working afterwards.
    resp = _google(client, _sign(signing_key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["id"] == "legacy-1"
    linked = db.get_user_by_email(GOOGLE_EMAIL)
    assert linked is not None and linked["google_sub"] == GOOGLE_SUB
    assert (
        client.post(
            "/v1/auth/login", json={"email": GOOGLE_EMAIL, "password": PASSWORD}
        ).status_code
        == 200
    )


def test_one_google_identity_belongs_to_at_most_one_account(
    sandbox: SimpleNamespace, signing_key: rsa.RSAPrivateKey
) -> None:
    from clipcatalyst_api import auth, db

    client = sandbox.client
    owner_id = _google(client, _sign(signing_key)).json()["user"]["id"]

    # A second, ordinary account. Two rows carrying google_sub '' coexist
    # happily — which is the whole reason the UNIQUE index is partial.
    other_id = _register(client, "someone-else@example.com")["user"]["id"]
    assert db.get_user_by_google_sub("") is None  # '' matches nobody

    # Neither creating nor linking can attach the same identity twice.
    assert (
        db.create_user(
            "would-be",
            email="third@example.com",
            password_hash=auth.hash_password(PASSWORD),
            google_sub=GOOGLE_SUB,
        )
        is None
    )
    assert db.link_google_sub(other_id, GOOGLE_SUB) is False
    stranger = db.get_user_by_email("someone-else@example.com")
    assert stranger is not None and stranger["google_sub"] == ""

    # And an account that already has a Google identity is never re-pointed
    # at another one by a later link.
    assert db.link_google_sub(owner_id, "some-other-google-sub") is False
    owner = db.get_user_by_id(owner_id)
    assert owner is not None and owner["google_sub"] == GOOGLE_SUB
    assert db.get_user_by_google_sub(GOOGLE_SUB)["id"] == owner_id  # type: ignore[index]


def test_verify_password_refuses_an_empty_stored_hash(sandbox: SimpleNamespace) -> None:
    """The rule stated where it lives: no stored password, no password login."""
    from clipcatalyst_api import auth

    assert not auth.verify_password("", "")
    assert not auth.verify_password("anything", "")
    # And a real hash of the empty password still verifies, so the refusal is
    # about the STORED value being absent, not about empty passwords as such.
    assert auth.verify_password("", auth.hash_password(""))
