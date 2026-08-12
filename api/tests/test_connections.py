"""Connected publishing accounts (PUBLISH.md Part 2), against a stub provider.

Nothing here is mocked away except the network. ``connections.http_request`` —
the one function in the module that reaches out — is replaced by a fake Google
that answers the token, identity and revoke endpoints, records every call it
was sent, and can be told to refuse, to break, or to rotate a refresh token.
Everything else runs for real: real Fernet encryption with a real key, real
HMAC over a real state, a real S256 challenge checked against the verifier the
exchange actually carried, and real SQLite rows read straight off the disk.

The file is organised around the four properties this module exists for, and
each one is tested as something an attacker or an accident would have to get
past rather than as a function that returns what it was told to:

  * §2 the `state` is single-use, signed, platform-bound and session-bound;
  * §3 PKCE is required — no verifier, no exchange;
  * §4 no token is returned by an endpoint or written to the database in
    plaintext (§4 greps the sqlite file itself, WAL included);
  * §7-§8 an unreadable token degrades to "reconnect", and a missing key
    refuses to store anything at all — without deleting what is already there.

Mirrors the env/import dance of ``test_google_auth.py``: all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules are purged for a clean
re-import. The whole os.environ is snapshotted and restored around each client.

No pipeline runs here — a connection touches two tables and nothing else.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from cryptography.fernet import Fernet

# Distinctive on purpose: §4 greps the raw database file for these exact
# strings, so anything that ever wrote one in the clear is caught by name.
ACCESS_TOKEN = "ya29.ACCESS-TOKEN-MUST-NEVER-BE-STORED-IN-PLAINTEXT"
REFRESH_TOKEN = "1//REFRESH-TOKEN-MUST-NEVER-BE-STORED-IN-PLAINTEXT"
ROTATED_REFRESH_TOKEN = "1//ROTATED-REFRESH-TOKEN-ALSO-NEVER-IN-PLAINTEXT"
REFRESHED_ACCESS_TOKEN = "ya29.SECOND-ACCESS-TOKEN-ALSO-NEVER-IN-PLAINTEXT"

CHANNEL_ID = "UC_test_channel_0001"
CHANNEL_NAME = "Creator Studio"

CLIENT_ID = "1234567890-publish.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-client-secret"
API_ORIGIN = "https://api.clipcatalyst.test"
FRONTEND_ORIGIN = "https://app.clipcatalyst.test"
REDIRECT_URI = f"{API_ORIGIN}/v1/connections/youtube/callback"

PASSWORD = "correct-horse-battery"

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
    "clipcatalyst_api.auth",
    "clipcatalyst_api.connections",
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


# --------------------------------------------------------------------------- #
# The stub provider: one callable standing in for Google's three endpoints.
# --------------------------------------------------------------------------- #


class FakeProvider:
    """A provider that answers, records, and can be told to misbehave.

    Every attribute below is something a real provider does on some day:
    refuse a spent code (`token_status`), rotate a refresh token
    (`rotate_refresh`), hand back an account with no channel (`channel = None`),
    fall over (`transport_error`), or say a token is already dead at revoke.
    """

    def __init__(self, db_module) -> None:  # noqa: ANN001 - the db module
        self._db = db_module
        self.calls: list[dict] = []
        self.access_token = ACCESS_TOKEN
        self.refresh_token = REFRESH_TOKEN
        self.expires_in: object = 3600
        self.channel: dict | None = {"id": CHANNEL_ID, "title": CHANNEL_NAME}
        self.token_status = 200
        self.token_body: dict | None = None
        self.identity_status = 200
        self.revoke_status = 200
        #: {"token", "identity", "revoke"} — endpoints that fail at the socket.
        self.transport_error: set[str] = set()
        #: Connections still in the database at the moment revoke was called.
        #: The disconnect ORDER is a promise, so it is observed, not assumed.
        self.connections_at_revoke: list[str] = []

    # -- helpers a test reads back ----------------------------------------- #

    def calls_to(self, kind: str) -> list[dict]:
        return [call for call in self.calls if call["kind"] == kind]

    @property
    def exchanges(self) -> list[dict]:
        return [c for c in self.calls_to("token") if c["form"].get("code")]

    @property
    def refreshes(self) -> list[dict]:
        return [c for c in self.calls_to("token") if c["form"].get("refresh_token")]

    # -- the seam ----------------------------------------------------------- #

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers=None,  # noqa: ANN001 - Mapping[str, str] | None
        form=None,  # noqa: ANN001 - Mapping[str, str] | None
    ) -> tuple[int, str]:
        kind = (
            "token"
            if url.endswith("/token")
            else "revoke"
            if url.endswith("/revoke")
            else "identity"
            if "youtube/v3/channels" in url
            else "unknown"
        )
        self.calls.append(
            {
                "kind": kind,
                "method": method,
                "url": url,
                "form": dict(form or {}),
                "headers": dict(headers or {}),
            }
        )
        if kind in self.transport_error:
            raise OSError(f"stub provider: {kind} endpoint is unreachable")
        if kind == "token":
            return self._token()
        if kind == "identity":
            return self._identity()
        if kind == "revoke":
            self.connections_at_revoke = [
                row["id"] for row in _rows(self._db, "connections")
            ]
            return self.revoke_status, "{}"
        return 404, json.dumps({"error": "not_found"})

    def _token(self) -> tuple[int, str]:
        if self.token_status != 200:
            return self.token_status, json.dumps(
                {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
            )
        if self.token_body is not None:
            return 200, json.dumps(self.token_body)
        body: dict = {"access_token": self.access_token, "token_type": "Bearer"}
        if self.refresh_token:
            body["refresh_token"] = self.refresh_token
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        body["scope"] = (
            "https://www.googleapis.com/auth/youtube.upload"
            " https://www.googleapis.com/auth/youtube.readonly"
        )
        return 200, json.dumps(body)

    def _identity(self) -> tuple[int, str]:
        if self.identity_status != 200:
            return self.identity_status, json.dumps(
                {"error": {"status": "PERMISSION_DENIED", "message": "no."}}
            )
        items = (
            []
            if self.channel is None
            else [{"id": self.channel["id"], "snippet": {"title": self.channel["title"]}}]
        )
        return 200, json.dumps({"items": items})


def _rows(db_module, table: str) -> list[dict]:  # noqa: ANN001 - the db module
    """Every row of a table, straight from SQLite — the server-side fact."""
    with contextlib.closing(db_module._connect()) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


@pytest.fixture()
def sandbox(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """A TestClient with publishing configured and a stub provider answering."""
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("connectdata")
    token_key = Fernet.generate_key().decode("ascii")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": API_ORIGIN,
            "CC_FRONTEND_ORIGIN": FRONTEND_ORIGIN,
            "CC_BILLING": "off",
            "CC_TOKEN_KEY": token_key,
            "CC_YOUTUBE_CLIENT_ID": CLIENT_ID,
            "CC_YOUTUBE_CLIENT_SECRET": CLIENT_SECRET,
        }
    )
    os.environ.pop("CC_API_TOKEN", None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth, connections, db
    from clipcatalyst_api.main import app
    from clipcatalyst_api.settings import get_settings

    provider = FakeProvider(db)
    monkeypatch.setattr(connections, "http_request", provider)
    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                data_dir=data_dir,
                db_path=Path(str(data_dir / "jobs.sqlite3")),
                provider=provider,
                token_key=token_key,
                connections=connections,
                db=db,
                settings=lambda: get_settings(),
            )
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


# --------------------------------------------------------------------------- #
# Driving the flow.
# --------------------------------------------------------------------------- #


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _account(client, email: str = "creator@example.com") -> str:
    resp = client.post("/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _start(client, token: str, platform: str = "youtube"):  # noqa: ANN202 - Response
    return client.post(f"/v1/connections/{platform}/start", headers=_bearer(token))


def _authorize_query(client, token: str) -> dict[str, str]:
    """Start a flow and return the authorize URL's query as a flat dict."""
    resp = _start(client, token)
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorize_url"]
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()
    }


def _callback(
    client, state: str, *, code: str = "4/auth-code-0001", platform: str = "youtube", **extra
):  # noqa: ANN202 - Response
    params = {"state": state, **extra}
    if code:
        params["code"] = code
    return client.get(
        f"/v1/connections/{platform}/callback", params=params, follow_redirects=False
    )


def _connect(sandbox: SimpleNamespace, token: str, *, code: str = "4/auth-code-0001"):  # noqa: ANN202
    """The whole flow, start to stored connection. Returns the redirect."""
    query = _authorize_query(sandbox.client, token)
    return _callback(sandbox.client, query["state"], code=code)


def _list(client, token: str) -> dict:
    resp = client.get("/v1/connections", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _platform(body: dict, platform: str) -> dict:
    return next(entry for entry in body["platforms"] if entry["platform"] == platform)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- #
# 1. The happy path: authorize, exchange, store, list.
# --------------------------------------------------------------------------- #


def test_start_builds_an_authorize_url_with_pkce_offline_access_and_a_state(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)

    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["response_type"] == "code"
    assert "youtube.upload" in query["scope"]
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"]
    # Both are what makes a refresh token arrive at all (PUBLISH.md).
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"

    staged = _rows(sandbox.db, "oauth_states")
    assert len(staged) == 1
    # The value that travels through the browser is NOT the value on disk.
    nonce = query["state"].split(".")[0]
    assert staged[0]["state_hash"] == hashlib.sha256(nonce.encode()).hexdigest()
    assert nonce not in json.dumps(staged[0])
    assert staged[0]["platform"] == "youtube"
    assert staged[0]["redirect_uri"] == REDIRECT_URI


def test_the_callback_stores_the_connection_and_returns_to_the_account_page(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    resp = _connect(sandbox, token)

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == f"{FRONTEND_ORIGIN}/account?connected=youtube"
    assert resp.headers["cache-control"] == "no-store"

    body = _list(sandbox.client, token)
    assert len(body["connections"]) == 1
    connection = body["connections"][0]
    assert connection["platform"] == "youtube"
    assert connection["account_name"] == CHANNEL_NAME
    assert connection["account_id"] == CHANNEL_ID
    assert "https://www.googleapis.com/auth/youtube.upload" in connection["scopes"]

    exchange = sandbox.provider.exchanges[0]["form"]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == REDIRECT_URI
    assert exchange["client_id"] == CLIENT_ID
    assert exchange["client_secret"] == CLIENT_SECRET
    # The identity call rides the access token we were just given, and nothing
    # else — the channel name is read, never taken from the client.
    identity = sandbox.provider.calls_to("identity")[0]
    assert identity["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_the_spent_state_row_is_gone_after_a_successful_callback(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    assert _rows(sandbox.db, "oauth_states") == []


def test_reconnecting_the_same_channel_updates_the_row_rather_than_duplicating(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    first = _rows(sandbox.db, "connections")[0]

    # A second authorization for the same channel, with a new access token and
    # — as Google does when it does not re-prompt — no refresh token at all.
    sandbox.provider.access_token = REFRESHED_ACCESS_TOKEN
    sandbox.provider.refresh_token = ""
    sandbox.provider.channel = {"id": CHANNEL_ID, "title": "Creator Studio (renamed)"}
    _connect(sandbox, token, code="4/auth-code-0002")

    rows = _rows(sandbox.db, "connections")
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]  # the same connection, updated
    assert rows[0]["created_at"] == first["created_at"]
    assert rows[0]["access_token_enc"] != first["access_token_enc"]
    assert _list(sandbox.client, token)["connections"][0]["account_name"] == (
        "Creator Studio (renamed)"
    )

    # The refresh token we already held SURVIVED a reconnect that brought none.
    # Overwriting it with '' is how a working connection quietly becomes one
    # that dies at the next expiry.
    fernet = Fernet(sandbox.token_key.encode())
    assert fernet.decrypt(rows[0]["refresh_token_enc"].encode()).decode() == REFRESH_TOKEN


def test_a_second_account_on_the_same_platform_is_its_own_connection(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    sandbox.provider.channel = {"id": "UC_test_channel_0002", "title": "Second Channel"}
    _connect(sandbox, token, code="4/auth-code-0003")

    names = {c["account_name"] for c in _list(sandbox.client, token)["connections"]}
    assert names == {CHANNEL_NAME, "Second Channel"}


def test_a_google_account_with_no_channel_stores_nothing(
    sandbox: SimpleNamespace,
) -> None:
    """Consent can be granted by an account that has nowhere to publish."""
    token = _account(sandbox.client)
    sandbox.provider.channel = None
    resp = _connect(sandbox, token)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"{FRONTEND_ORIGIN}/account?connect_error=no_account"
    assert _rows(sandbox.db, "connections") == []


def test_declining_at_the_consent_screen_comes_back_cleanly(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    resp = _callback(sandbox.client, query["state"], code="", error="access_denied")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"{FRONTEND_ORIGIN}/account?connect_error=denied"
    assert _rows(sandbox.db, "connections") == []
    # A refusal is never exchanged for anything.
    assert sandbox.provider.calls == []


def test_a_provider_that_refuses_the_exchange_is_reported_not_stored(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    sandbox.provider.token_status = 400
    resp = _connect(sandbox, token)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"{FRONTEND_ORIGIN}/account?connect_error=refused"
    assert _rows(sandbox.db, "connections") == []


def test_a_provider_we_cannot_reach_is_reported_as_temporary(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    sandbox.provider.transport_error = {"token"}
    resp = _connect(sandbox, token)

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == f"{FRONTEND_ORIGIN}/account?connect_error=unavailable"
    )
    assert _rows(sandbox.db, "connections") == []


# --------------------------------------------------------------------------- #
# 2. The state: single-use, signed, platform-bound, session-bound.
# --------------------------------------------------------------------------- #


def test_a_replayed_state_is_refused(sandbox: SimpleNamespace) -> None:
    """The CSRF property. One state, one callback, whatever arrives second."""
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    first = _callback(sandbox.client, query["state"])
    assert first.status_code == 303
    exchanges = len(sandbox.provider.exchanges)

    replay = _callback(sandbox.client, query["state"], code="4/attacker-code")
    assert replay.status_code == 400
    assert "no longer valid" in replay.json()["detail"]
    # Nothing was exchanged and nothing was stored the second time round.
    assert len(sandbox.provider.exchanges) == exchanges
    assert len(_rows(sandbox.db, "connections")) == 1


def test_a_state_this_server_never_signed_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    _account(sandbox.client)
    resp = _callback(sandbox.client, "not-a-state-we-ever-minted.abcdef")
    assert resp.status_code == 400
    # Refused by arithmetic: a forged value never becomes a database lookup,
    # let alone a token exchange.
    assert sandbox.provider.calls == []
    assert _rows(sandbox.db, "connections") == []


def test_a_tampered_state_signature_is_refused(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    nonce, _, signature = query["state"].partition(".")
    flipped = "a" if signature[0] != "a" else "b"
    resp = _callback(sandbox.client, f"{nonce}.{flipped}{signature[1:]}")

    assert resp.status_code == 400
    assert sandbox.provider.calls == []
    # The state row is untouched: a bad signature must not spend it either, or
    # anybody could cancel somebody else's in-flight connection.
    assert len(_rows(sandbox.db, "oauth_states")) == 1


def test_a_bare_nonce_without_a_signature_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    """Knowing the nonce is not enough — it has to be signed for this flow."""
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    resp = _callback(sandbox.client, query["state"].split(".")[0])

    assert resp.status_code == 400
    assert sandbox.provider.calls == []


def test_a_state_minted_for_one_platform_is_refused_at_another(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    resp = _callback(sandbox.client, query["state"], platform="tiktok")

    assert resp.status_code == 400
    assert sandbox.provider.calls == []
    assert len(_rows(sandbox.db, "oauth_states")) == 1


def test_the_connection_lands_on_the_account_that_started_the_flow(
    sandbox: SimpleNamespace,
) -> None:
    """A callback carries no credential, so the ROW decides whose it is.

    Somebody else's browser completing the redirect — the shape a CSRF attempt
    takes — cannot attach a channel to their own account: the state is bound to
    the session that minted it, and that is the account it lands on.
    """
    alice = _account(sandbox.client, "alice@example.com")
    bob = _account(sandbox.client, "bob@example.com")
    query = _authorize_query(sandbox.client, alice)

    # Bob's browser follows the redirect, session and all. It changes nothing.
    resp = sandbox.client.get(
        "/v1/connections/youtube/callback",
        params={"state": query["state"], "code": "4/auth-code-0001"},
        headers=_bearer(bob),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert len(_list(sandbox.client, alice)["connections"]) == 1
    assert _list(sandbox.client, bob)["connections"] == []


def test_a_state_whose_session_has_ended_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    assert sandbox.client.post("/v1/auth/logout", headers=_bearer(token)).status_code == 200

    resp = _callback(sandbox.client, query["state"])
    assert resp.status_code == 400
    assert sandbox.provider.calls == []
    assert _rows(sandbox.db, "connections") == []


def test_an_expired_state_is_refused_and_swept(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    staged = _rows(sandbox.db, "oauth_states")[0]
    with contextlib.closing(sandbox.db._connect()) as conn:
        conn.execute(
            "UPDATE oauth_states SET expires_at = ? WHERE state_hash = ?",
            (_iso(timedelta(minutes=-1)), staged["state_hash"]),
        )

    resp = _callback(sandbox.client, query["state"])
    assert resp.status_code == 400
    assert sandbox.provider.calls == []
    # Already refused by the read; the sweep is only what clears the table.
    assert len(_rows(sandbox.db, "oauth_states")) == 1
    assert sandbox.db.purge_expired_oauth_states() == 1
    assert _rows(sandbox.db, "oauth_states") == []


def test_two_flows_at_once_stay_independent(sandbox: SimpleNamespace) -> None:
    """Spending one state must not disturb another — one row each, keyed."""
    token = _account(sandbox.client)
    first = _authorize_query(sandbox.client, token)
    second = _authorize_query(sandbox.client, token)
    assert first["state"] != second["state"]
    assert len(_rows(sandbox.db, "oauth_states")) == 2

    assert _callback(sandbox.client, first["state"]).status_code == 303
    assert len(_rows(sandbox.db, "oauth_states")) == 1
    sandbox.provider.channel = {"id": "UC_test_channel_0002", "title": "Second Channel"}
    assert _callback(sandbox.client, second["state"]).status_code == 303
    assert len(_list(sandbox.client, token)["connections"]) == 2


# --------------------------------------------------------------------------- #
# 3. PKCE: the verifier is required, and it is the one for the challenge.
# --------------------------------------------------------------------------- #


def test_the_exchange_carries_the_verifier_for_the_challenge_that_was_sent(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    assert _callback(sandbox.client, query["state"]).status_code == 303

    verifier = sandbox.provider.exchanges[0]["form"]["code_verifier"]
    assert verifier, "the exchange must carry a PKCE verifier"
    assert len(verifier) >= 43  # RFC 7636's floor
    # The whole point: this verifier hashes to the challenge the browser was
    # sent away with, so an intercepted code is worth nothing without it.
    assert _s256(verifier) == query["code_challenge"]


def test_the_verifier_is_never_the_same_twice(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    first = _authorize_query(sandbox.client, token)
    second = _authorize_query(sandbox.client, token)
    assert first["code_challenge"] != second["code_challenge"]


def test_a_verifier_that_cannot_be_read_refuses_the_exchange(
    sandbox: SimpleNamespace,
) -> None:
    """No verifier, no tokens. There is no fallback — that is what required means."""
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    with contextlib.closing(sandbox.db._connect()) as conn:
        conn.execute("UPDATE oauth_states SET verifier_enc = 'not-ciphertext'")

    resp = _callback(sandbox.client, query["state"])
    assert resp.status_code == 400
    assert sandbox.provider.calls == []
    assert _rows(sandbox.db, "connections") == []


# --------------------------------------------------------------------------- #
# 4. Tokens: never returned, never in the database in plaintext.
# --------------------------------------------------------------------------- #


def _database_bytes(sandbox: SimpleNamespace) -> bytes:
    """Every byte SQLite is holding for us — the WAL and shm files included.

    Reading only ``jobs.sqlite3`` would be a test that passes while the tokens
    sit in ``jobs.sqlite3-wal``: in WAL mode a committed row lives there until
    a checkpoint moves it. The point of this test is what is on the disk, so it
    reads what is on the disk.
    """
    return b"".join(
        path.read_bytes()
        for path in sorted(sandbox.data_dir.glob("jobs.sqlite3*"))
        if path.is_file()
    )


def test_the_database_file_never_holds_a_token_in_plaintext(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    verifier = sandbox.provider.exchanges[0]["form"].get("code_verifier", "")
    assert verifier, "the exchange must carry a PKCE verifier"

    raw = _database_bytes(sandbox)
    # The connection really is in there…
    assert CHANNEL_NAME.encode() in raw
    # …and none of its secrets are, in any encoding a grep would find.
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, verifier):
        assert secret.encode("utf-8") not in raw, f"{secret!r} is on disk in plaintext"
        assert secret.encode("utf-16-le") not in raw


def test_the_stored_tokens_are_fernet_ciphertext_under_the_configured_key(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    row = _rows(sandbox.db, "connections")[0]

    fernet = Fernet(sandbox.token_key.encode())
    assert fernet.decrypt(row["access_token_enc"].encode()).decode() == ACCESS_TOKEN
    assert fernet.decrypt(row["refresh_token_enc"].encode()).decode() == REFRESH_TOKEN
    # A different key gets nothing — the ciphertext is not an encoding.
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(row["access_token_enc"].encode())


def test_no_connections_endpoint_ever_returns_a_token(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    redirect = _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]
    row = _rows(sandbox.db, "connections")[0]

    bodies = [
        redirect.text,
        redirect.headers["location"],
        _start(sandbox.client, token).text,
        sandbox.client.get("/v1/connections", headers=_bearer(token)).text,
        sandbox.client.delete(
            f"/v1/connections/{connection_id}", headers=_bearer(token)
        ).text,
    ]
    for body in bodies:
        for secret in (ACCESS_TOKEN, REFRESH_TOKEN):
            assert secret not in body
        # Not even the ciphertext leaves the server: a client has no use for it
        # and its presence would be one decode away from the real thing.
        assert row["access_token_enc"] not in body
        assert row["refresh_token_enc"] not in body


def test_the_listing_carries_no_token_shaped_field_at_all(
    sandbox: SimpleNamespace,
) -> None:
    """The schema is the guarantee: there is nowhere for a token to go."""
    token = _account(sandbox.client)
    _connect(sandbox, token)
    body = _list(sandbox.client, token)
    assert set(body["connections"][0]) == {
        "id",
        "platform",
        "account_name",
        "account_id",
        "scopes",
        "created_at",
        "updated_at",
    }


def test_no_token_ever_reaches_a_log_line(
    sandbox: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """A log file outlives the token in it, and is read by more people.

    The whole lifecycle is driven with logging wide open — connect, refresh,
    a refused refresh, a disconnect — because the tempting place to put a
    token in a log is a failure branch nobody exercises.
    """
    caplog.set_level(0)
    token = _account(sandbox.client)
    _connect(sandbox, token)
    verifier = sandbox.provider.exchanges[0]["form"].get("code_verifier", "")

    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )
    sandbox.provider.access_token = REFRESHED_ACCESS_TOKEN
    sandbox.provider.refresh_token = ROTATED_REFRESH_TOKEN
    sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))

    # A refused refresh: the branch that logs why a connection was dropped.
    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )
    sandbox.provider.token_status = 400
    with pytest.raises(sandbox.connections.NeedsReconnect):
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))

    # And a disconnect, tokens and all.
    sandbox.provider.token_status = 200
    _connect(sandbox, token, code="4/auth-code-0009")
    connection_id = _rows(sandbox.db, "connections")[0]["id"]
    sandbox.client.delete(f"/v1/connections/{connection_id}", headers=_bearer(token))

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        ROTATED_REFRESH_TOKEN,
        REFRESHED_ACCESS_TOKEN,
        verifier,
        sandbox.token_key,
    ):
        assert secret and secret not in logged


def test_connection_routes_need_a_session(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]

    assert sandbox.client.get("/v1/connections").status_code == 401
    assert sandbox.client.post("/v1/connections/youtube/start").status_code == 401
    assert sandbox.client.delete(f"/v1/connections/{connection_id}").status_code == 401
    assert len(_rows(sandbox.db, "connections")) == 1


def test_account_responses_are_never_cached(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    listing = sandbox.client.get("/v1/connections", headers=_bearer(token))
    assert listing.headers["cache-control"] == "no-store"
    assert _start(sandbox.client, token).headers["cache-control"] == "no-store"


# --------------------------------------------------------------------------- #
# 5. Disconnecting: revoke first, then delete.
# --------------------------------------------------------------------------- #


def test_disconnect_revokes_with_the_provider_and_then_deletes(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]

    resp = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text

    revokes = sandbox.provider.calls_to("revoke")
    assert len(revokes) == 1
    # The REFRESH token: revoking it takes the whole grant, while revoking an
    # access token would leave the standing permission alive.
    assert revokes[0]["form"]["token"] == REFRESH_TOKEN
    # The ORDER is the promise — the row was still there when we revoked, so a
    # failure could not have stranded a live grant with no token left to kill it.
    assert sandbox.provider.connections_at_revoke == [connection_id]
    assert _rows(sandbox.db, "connections") == []
    assert _list(sandbox.client, token)["connections"] == []


def test_a_provider_we_cannot_reach_keeps_the_connection_for_a_retry(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]
    sandbox.provider.transport_error = {"revoke"}

    resp = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(token)
    )
    assert resp.status_code == 502
    assert "try again" in resp.json()["detail"]
    # Nothing was thrown away: these tokens are the only thing that can ever
    # revoke that grant.
    assert len(_rows(sandbox.db, "connections")) == 1

    sandbox.provider.transport_error = set()
    retry = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(token)
    )
    assert retry.status_code == 200
    assert _rows(sandbox.db, "connections") == []


def test_a_token_the_provider_says_is_already_dead_still_disconnects(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]
    sandbox.provider.revoke_status = 400  # already revoked from Google's side

    resp = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(token)
    )
    assert resp.status_code == 200
    assert _rows(sandbox.db, "connections") == []


def test_a_connection_whose_tokens_are_unreadable_can_still_be_removed(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]
    with contextlib.closing(sandbox.db._connect()) as conn:
        conn.execute(
            "UPDATE connections SET access_token_enc = 'x', refresh_token_enc = 'x'"
        )

    resp = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(token)
    )
    assert resp.status_code == 200
    # There was nothing revocable to send, so nothing was sent — but the row
    # does not get to sit on somebody's account page forever because of it.
    assert sandbox.provider.calls_to("revoke") == []
    assert _rows(sandbox.db, "connections") == []


def test_another_accounts_connection_is_a_404_and_survives(
    sandbox: SimpleNamespace,
) -> None:
    alice = _account(sandbox.client, "alice@example.com")
    bob = _account(sandbox.client, "bob@example.com")
    _connect(sandbox, alice)
    connection_id = _rows(sandbox.db, "connections")[0]["id"]

    resp = sandbox.client.delete(
        f"/v1/connections/{connection_id}", headers=_bearer(bob)
    )
    assert resp.status_code == 404
    assert sandbox.provider.calls_to("revoke") == []
    assert len(_rows(sandbox.db, "connections")) == 1
    assert len(_list(sandbox.client, alice)["connections"]) == 1


# --------------------------------------------------------------------------- #
# 6. The refresh helper: a live token, and the new one persisted.
# --------------------------------------------------------------------------- #


def _stored(sandbox: SimpleNamespace) -> dict:
    return sandbox.db.get_connection(_rows(sandbox.db, "connections")[0]["id"])


def _decrypted(sandbox: SimpleNamespace, field: str) -> str:
    row = _stored(sandbox)
    return Fernet(sandbox.token_key.encode()).decrypt(row[field].encode()).decode()


def test_a_token_with_time_left_is_returned_without_calling_the_provider(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    before = len(sandbox.provider.calls)

    live = sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert live == ACCESS_TOKEN
    assert len(sandbox.provider.calls) == before  # nothing was asked of anybody


def test_a_token_inside_the_refresh_window_is_refreshed_and_persisted(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    # Four minutes left: inside PUBLISH.md's five-minute window, so an upload
    # starting now would otherwise die halfway through.
    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(minutes=4))
    )
    sandbox.provider.access_token = REFRESHED_ACCESS_TOKEN
    sandbox.provider.refresh_token = ""  # Google sends none on a refresh

    live = sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert live == REFRESHED_ACCESS_TOKEN
    assert sandbox.provider.refreshes[0]["form"]["refresh_token"] == REFRESH_TOKEN
    assert sandbox.provider.refreshes[0]["form"]["client_secret"] == CLIENT_SECRET

    # Persisted, encrypted, and the refresh token we hold is still ours.
    assert _decrypted(sandbox, "access_token_enc") == REFRESHED_ACCESS_TOKEN
    assert _decrypted(sandbox, "refresh_token_enc") == REFRESH_TOKEN
    assert REFRESHED_ACCESS_TOKEN.encode() not in _database_bytes(sandbox)
    # And the next call is served from the row, not from another refresh.
    assert sandbox.connections.access_token(sandbox.settings(), _stored(sandbox)) == (
        REFRESHED_ACCESS_TOKEN
    )
    assert len(sandbox.provider.refreshes) == 1


def test_a_rotated_refresh_token_replaces_the_stored_one(
    sandbox: SimpleNamespace,
) -> None:
    """Providers that rotate must have the NEW value stored (PUBLISH.md)."""
    token = _account(sandbox.client)
    _connect(sandbox, token)
    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )
    sandbox.provider.access_token = REFRESHED_ACCESS_TOKEN
    sandbox.provider.refresh_token = ROTATED_REFRESH_TOKEN

    assert sandbox.connections.access_token(sandbox.settings(), _stored(sandbox)) == (
        REFRESHED_ACCESS_TOKEN
    )
    assert _decrypted(sandbox, "refresh_token_enc") == ROTATED_REFRESH_TOKEN
    assert ROTATED_REFRESH_TOKEN.encode() not in _database_bytes(sandbox)


def test_a_refused_refresh_disconnects_and_asks_for_a_reconnect(
    sandbox: SimpleNamespace,
) -> None:
    """The user revoked us from their Google account — an ordinary thing."""
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection = _stored(sandbox)
    sandbox.db.update_connection(connection["id"], expires_at=_iso(timedelta(seconds=-1)))
    sandbox.provider.token_status = 400

    with pytest.raises(sandbox.connections.NeedsReconnect) as failure:
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert "reconnect" in str(failure.value).lower()
    assert _rows(sandbox.db, "connections") == []
    assert _list(sandbox.client, token)["connections"] == []


def test_a_provider_outage_during_a_refresh_keeps_the_connection(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )
    sandbox.provider.transport_error = {"token"}

    with pytest.raises(sandbox.connections.ProviderUnavailable):
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    # An outage is not a revocation: the connection is still theirs.
    assert len(_rows(sandbox.db, "connections")) == 1


def test_an_expired_token_with_no_refresh_token_asks_for_a_reconnect(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    sandbox.provider.refresh_token = ""  # a consent that never granted offline access
    _connect(sandbox, token)
    assert _stored(sandbox)["refresh_token_enc"] == ""
    sandbox.db.update_connection(
        _stored(sandbox)["id"], expires_at=_iso(timedelta(seconds=-1))
    )

    with pytest.raises(sandbox.connections.NeedsReconnect):
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert _rows(sandbox.db, "connections") == []
    assert sandbox.provider.refreshes == []


def test_a_provider_that_states_no_expiry_is_never_pre_emptively_refreshed(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    sandbox.provider.expires_in = None
    _connect(sandbox, token)
    assert _stored(sandbox)["expires_at"] == ""

    assert sandbox.connections.access_token(sandbox.settings(), _stored(sandbox)) == (
        ACCESS_TOKEN
    )
    assert sandbox.provider.refreshes == []


# --------------------------------------------------------------------------- #
# 7. A decrypt failure degrades to "reconnect" — it never crashes a publish.
# --------------------------------------------------------------------------- #


def _corrupt_tokens(sandbox: SimpleNamespace) -> None:
    """What a rotated key or a restored backup leaves behind."""
    with contextlib.closing(sandbox.db._connect()) as conn:
        conn.execute(
            "UPDATE connections SET access_token_enc = ?, refresh_token_enc = ?",
            (Fernet(Fernet.generate_key()).encrypt(b"other").decode(), "nonsense"),
        )


def test_a_decrypt_failure_disconnects_and_asks_for_a_reconnect(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _connect(sandbox, token)
    _corrupt_tokens(sandbox)

    with pytest.raises(sandbox.connections.NeedsReconnect) as failure:
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert "reconnect" in str(failure.value).lower()
    # Disconnected, not left in place to fail the same way on every publish.
    assert _rows(sandbox.db, "connections") == []
    assert _list(sandbox.client, token)["connections"] == []


def test_a_key_rotation_is_survivable_by_reconnecting(
    sandbox: SimpleNamespace,
) -> None:
    """End to end: rotate the key, lose the connection, connect again."""
    token = _account(sandbox.client)
    _connect(sandbox, token)
    _set_env(CC_TOKEN_KEY=Fernet.generate_key().decode("ascii"))

    with pytest.raises(sandbox.connections.NeedsReconnect):
        sandbox.connections.access_token(sandbox.settings(), _stored(sandbox))
    assert _rows(sandbox.db, "connections") == []

    assert _connect(sandbox, token).status_code == 303
    assert len(_list(sandbox.client, token)["connections"]) == 1


def test_a_missing_key_does_not_delete_anybodys_connection(
    sandbox: SimpleNamespace,
) -> None:
    """The distinction that matters: no key is an ops fault, not a bad row.

    Treating "we cannot read this right now" as "this connection is broken"
    would delete every stored connection on the box the first time somebody
    forgot an environment variable.
    """
    token = _account(sandbox.client)
    _connect(sandbox, token)
    connection = _stored(sandbox)
    _set_env(CC_TOKEN_KEY=None)

    with pytest.raises(sandbox.connections.TokenKeyUnavailable):
        sandbox.connections.access_token(sandbox.settings(), connection)
    assert len(_rows(sandbox.db, "connections")) == 1


# --------------------------------------------------------------------------- #
# 8. No CC_TOKEN_KEY: an honest 503, and nothing stored.
# --------------------------------------------------------------------------- #


def test_with_no_token_key_starting_is_refused_and_nothing_is_staged(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _set_env(CC_TOKEN_KEY=None)

    resp = _start(sandbox.client, token)
    assert resp.status_code == 503, resp.text
    assert "CC_TOKEN_KEY" in resp.json()["detail"]
    # The whole point: no half-staged flow, no verifier written somewhere it
    # could not be encrypted, nothing at all.
    assert _rows(sandbox.db, "oauth_states") == []
    assert _rows(sandbox.db, "connections") == []


def test_with_no_token_key_the_callback_is_refused_too(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    query = _authorize_query(sandbox.client, token)
    _set_env(CC_TOKEN_KEY=None)

    resp = _callback(sandbox.client, query["state"])
    assert resp.status_code == 503
    assert sandbox.provider.calls == []
    assert _rows(sandbox.db, "connections") == []


def test_a_token_key_that_is_not_a_fernet_key_is_refused_like_a_missing_one(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _set_env(CC_TOKEN_KEY="hunter2-not-a-fernet-key")

    resp = _start(sandbox.client, token)
    assert resp.status_code == 503
    assert _rows(sandbox.db, "oauth_states") == []


def test_the_account_page_is_told_that_connecting_is_off(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _set_env(CC_TOKEN_KEY=None)

    youtube = _platform(_list(sandbox.client, token), "youtube")
    assert youtube["connectable"] is False
    assert "CC_TOKEN_KEY" in youtube["reason"]


# --------------------------------------------------------------------------- #
# 9. The platform catalogue: nothing is offered that cannot work.
# --------------------------------------------------------------------------- #


def test_youtube_is_connectable_and_says_what_a_post_will_do(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    youtube = _platform(_list(sandbox.client, token), "youtube")
    assert youtube["connectable"] is True
    assert youtube["reason"] == ""
    # The honest v1 (PUBLISH.md): it really uploads, as a private video.
    assert "private" in youtube["note"]


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_platforms_awaiting_review_are_listed_but_refuse_to_start(
    sandbox: SimpleNamespace, platform: str
) -> None:
    token = _account(sandbox.client)
    entry = _platform(_list(sandbox.client, token), platform)
    assert entry["connectable"] is False
    assert "review" in entry["reason"]

    resp = _start(sandbox.client, token, platform)
    assert resp.status_code == 503
    assert resp.json()["detail"] == entry["reason"]
    assert _rows(sandbox.db, "oauth_states") == []


def test_an_unknown_platform_is_a_404(sandbox: SimpleNamespace) -> None:
    token = _account(sandbox.client)
    assert _start(sandbox.client, token, "myspace").status_code == 404
    assert _callback(sandbox.client, "x.y", platform="myspace").status_code == 404


def test_an_account_with_nothing_connected_still_gets_the_catalogue(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    body = _list(sandbox.client, token)
    assert body["connections"] == []
    assert {entry["platform"] for entry in body["platforms"]} == {
        "youtube",
        "tiktok",
        "instagram",
    }


def test_without_a_public_url_a_flow_cannot_be_started(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _set_env(CC_PUBLIC_BASE_URL="")

    resp = _start(sandbox.client, token)
    assert resp.status_code == 503
    assert "CC_PUBLIC_BASE_URL" in resp.json()["detail"]
    assert _platform(_list(sandbox.client, token), "youtube")["connectable"] is False
    assert _rows(sandbox.db, "oauth_states") == []


def test_without_client_credentials_a_flow_cannot_be_started(
    sandbox: SimpleNamespace,
) -> None:
    token = _account(sandbox.client)
    _set_env(CC_YOUTUBE_CLIENT_SECRET="")

    resp = _start(sandbox.client, token)
    assert resp.status_code == 503
    assert "CC_YOUTUBE_CLIENT_SECRET" in resp.json()["detail"]
    assert _rows(sandbox.db, "oauth_states") == []
