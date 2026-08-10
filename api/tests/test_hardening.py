"""Hardening regressions from the adversarial security review.

Every test here failed before the fix it guards, and each one asserts against
a SERVER-side fact — a response header, a stored row, the height the renderer
was handed, a refusal to boot — never against something a client sent.

Mirrors the env/import dance of ``test_security.py``: all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules (queue_app / worker / main)
are purged for a clean re-import. The whole os.environ is snapshotted and
restored around each client, and auth's in-process rate-limit windows (which
outlive the purge) are cleared on the way in AND out.

The render-height test runs the real pipeline in eager mode
(``CC_QUEUE=eager``) with a faked transcriber and a generated test video,
because "the RENDER is clamped, not just the row" is only true if a clip was
actually rendered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

VIDEO_SECONDS = 40
MAX_UPLOAD_BYTES = 20_000_000
FOUNDER_TOKEN = "s3cr3t-founder-token"
PASSWORD = "correct-horse-battery"

# The peer address the proxy tests dial from, and the block that trusts it.
PROXY_PEER = "10.9.0.7"
PROXY_CIDR = "10.9.0.0/24"

SENTENCES = [
    "Here's the secret nobody tells you about growing an audience.",
    "I doubled my watch time in seven days and it shocked me.",
    "Most creators quit right before the algorithm finally rewards them.",
    "The biggest mistake is wasting your first ten seconds.",
    "Why does retention matter so much more than raw views?",
    "Never bury your hook behind a long boring intro.",
    "This one simple change made my clips explode overnight.",
    "You can steal this exact framework and use it today.",
]

WORD_SECONDS = 0.38
SENTENCE_GAP_SECONDS = 0.45
SPEECH_START = 1.0

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
)


def _make_video(path: Path) -> Path:
    """~40 s test video: testsrc2 pattern + 440 Hz sine, x264 + aac."""
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(VIDEO_SECONDS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"test video generation failed:\n{proc.stderr[-2000:]}"
    assert path.stat().st_size > 10_000
    return path


def _make_transcript(path: Path) -> Path:
    """Word-timestamped fake transcript JSON matching the video's duration."""
    words: list[dict] = []
    t = SPEECH_START
    for sentence in SENTENCES:
        for token in sentence.split():
            words.append(
                {"text": f" {token}", "start": round(t, 3), "end": round(t + WORD_SECONDS, 3)}
            )
            t += WORD_SECONDS
        t += SENTENCE_GAP_SECONDS
    assert t < VIDEO_SECONDS - 1, "transcript must fit inside the test video"
    text = "".join(w["text"] for w in words).strip()
    path.write_text(json.dumps({"words": words, "text": text}), encoding="utf-8")
    return path


def _purge() -> None:
    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _set_env(**values: str | None) -> None:
    """Flip CC_* env mid-test; the routes read get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_video(tmp_path_factory.mktemp("hardmedia") / "source.mp4")


def _boot(data_dir: Path, transcript: Path, **extra: str) -> Iterator[SimpleNamespace]:
    """Env → import → TestClient, with every scrap of process state restored."""
    saved_env = dict(os.environ)
    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_TRANSCRIBER": "fake",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_FAKE_TRANSCRIPT_PATH": str(transcript),
            "CC_PUBLIC_BASE_URL": "",
            "CC_MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
            "CC_BILLING": "off",
            "CC_TRUSTED_PROXIES": "",  # trust nobody, the shipped default
            **extra,
        }
    )
    for name in ("CC_API_TOKEN", "CC_STRIPE_PRICE_STARTER", "CC_STRIPE_PRICE_PRO",
                 "CC_STRIPE_PRICE_ENTERPRISE"):
        if name not in extra:
            os.environ.pop(name, None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth
    from clipcatalyst_api.main import app

    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, app=app, data_dir=data_dir)
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; billing off, no founder token."""
    transcript = _make_transcript(tmp_path_factory.mktemp("hardfix") / "transcript.json")
    yield from _boot(tmp_path_factory.mktemp("harddata"), transcript)


@pytest.fixture()
def billing_sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A billing-enabled box — which now REQUIRES a founder token to boot."""
    transcript = _make_transcript(tmp_path_factory.mktemp("hardbfix") / "transcript.json")
    yield from _boot(
        tmp_path_factory.mktemp("hardbdata"),
        transcript,
        CC_BILLING="fake",
        CC_API_TOKEN=FOUNDER_TOKEN,
        CC_STRIPE_WEBHOOK_SECRET="whsec_hardening",
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str, password: str = PASSWORD) -> dict:
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _job_payload(**overrides) -> dict:
    payload = {
        "filename": "source.mp4",
        "size_bytes": 1234,
        "target_length": 15,
        "count": 1,
        "height": 960,
    }
    payload.update(overrides)
    return payload


def _proxy_client(sandbox: SimpleNamespace, peer: str):  # noqa: ANN202 - TestClient
    """A client whose socket peer is `peer` — i.e. dialing from that address."""
    from fastapi.testclient import TestClient

    return TestClient(sandbox.app, client=(peer, 40404))


def _login_codes(client, count: int, *, forwarded: str | None, same: bool = False) -> list[int]:
    """`count` failed logins, each from its own forwarded client unless `same`."""
    codes = []
    for i in range(count):
        headers = {}
        if forwarded is not None:
            headers["X-Forwarded-For"] = forwarded if same else f"203.0.113.{i + 1}"
        codes.append(
            client.post(
                "/v1/auth/login",
                json={"email": f"nobody{i}@example.com", "password": "whatever12"},
                headers=headers,
            ).status_code
        )
    return codes


# --------------------------------------------------------------------------- #
# 1. Rate limiting behind a reverse proxy: the limiter must key on the real
#    client, and must NEVER take a forwarded header on trust.
# --------------------------------------------------------------------------- #


def test_client_ip_trusts_forwarded_headers_only_from_configured_proxies() -> None:
    from clipcatalyst_api import auth

    trusted = [PROXY_CIDR, "192.0.2.10"]
    # Trusted peer → the leftmost forwarded entry IS the client.
    assert auth.client_ip(PROXY_PEER, "198.51.100.4", trusted) == "198.51.100.4"
    assert auth.client_ip(PROXY_PEER, "198.51.100.4, 10.9.0.7", trusted) == "198.51.100.4"
    assert auth.client_ip("192.0.2.10", " 198.51.100.9 ", trusted) == "198.51.100.9"
    # Untrusted peer → the header is just a string a stranger typed.
    assert auth.client_ip("198.51.100.200", "198.51.100.4", trusted) == "198.51.100.200"
    # No configured proxies (the shipped default) → nobody is believed.
    assert auth.client_ip(PROXY_PEER, "198.51.100.4", []) == PROXY_PEER
    # Junk in the header cannot mint window keys, and a missing one is fine.
    assert auth.client_ip(PROXY_PEER, "not-an-ip", trusted) == PROXY_PEER
    assert auth.client_ip(PROXY_PEER, "", trusted) == PROXY_PEER
    assert auth.client_ip(PROXY_PEER, None, trusted) == PROXY_PEER
    # A non-ip peer (unix socket, test transport) is never a proxy.
    assert auth.client_ip("testclient", "198.51.100.4", trusted) == "testclient"
    # Unparseable CC_TRUSTED_PROXIES entries are ignored, not crashed on.
    assert auth.client_ip(PROXY_PEER, "198.51.100.4", ["nonsense"]) == PROXY_PEER


def test_forwarded_for_is_ignored_when_no_proxy_is_trusted(
    sandbox: SimpleNamespace,
) -> None:
    """The dangerous 'fix' — trusting X-Forwarded-For unconditionally — would
    disable the limiter entirely, since anyone can rotate the header."""
    from clipcatalyst_api import auth

    client = _proxy_client(sandbox, PROXY_PEER)
    auth.reset_rate_limits()
    codes = _login_codes(client, 11, forwarded=None)
    assert codes[-1] == 429, codes  # one peer, one bucket

    auth.reset_rate_limits()
    # Same peer, 11 different forged X-Forwarded-For values: still one bucket.
    codes = _login_codes(client, 11, forwarded="claimed")
    assert codes[-1] == 429, "an untrusted peer must not escape by forging XFF"
    auth.reset_rate_limits()


def test_a_trusted_proxy_gives_every_client_its_own_bucket(
    sandbox: SimpleNamespace,
) -> None:
    """Behind the reverse proxy DEPLOY.md mandates, request.client.host is the
    proxy for EVERYONE: without this, 11 logins from 11 distinct clients shared
    one 10/min window and the 11th — an innocent third party — got a 429."""
    from clipcatalyst_api import auth

    client = _proxy_client(sandbox, PROXY_PEER)
    _set_env(CC_TRUSTED_PROXIES=PROXY_CIDR)
    try:
        auth.reset_rate_limits()
        codes = _login_codes(client, 11, forwarded="distinct")
        assert codes == [401] * 11, f"distinct clients must not share a bucket: {codes}"

        # ...and the limiter still bites the client that is actually guessing.
        auth.reset_rate_limits()
        codes = _login_codes(client, 11, forwarded="198.51.100.77", same=True)
        assert codes[:10] == [401] * 10 and codes[10] == 429, codes
        # An innocent third party behind the same proxy is unaffected.
        victim = client.post(
            "/v1/auth/login",
            json={"email": "victim@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.78"},
        )
        assert victim.status_code == 401, "one attacker must not lock out the service"
    finally:
        auth.reset_rate_limits()
        _set_env(CC_TRUSTED_PROXIES="")


# --------------------------------------------------------------------------- #
# 2. A non-ASCII Authorization header is a bad credential, not a crash.
# --------------------------------------------------------------------------- #


def test_non_ascii_authorization_header_is_rejected_not_a_500(
    sandbox: SimpleNamespace,
) -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str, so `Bearer é`
    used to 500 — unauthenticated, via the credential-free status route."""
    from fastapi.testclient import TestClient

    # raise_server_exceptions=False is what uvicorn does in production: an
    # unhandled exception becomes a 500 response instead of failing the test.
    client = TestClient(sandbox.app, raise_server_exceptions=False)
    # Raw latin-1 on the wire; starlette decodes headers as latin-1, so the
    # route sees a str carrying a non-ASCII codepoint.
    bad = {"Authorization": "Bearer é".encode("latin-1")}

    for token in (FOUNDER_TOKEN, None):  # gated box, then the open dev default
        _set_env(CC_API_TOKEN=token)
        status = client.get("/v1/jobs/" + "a" * 32, headers=bad)
        assert status.status_code == 404, status.text  # optional_actor: unknown job
        created = client.post("/v1/jobs", json=_job_payload(), headers=bad)
        assert created.status_code in (201, 401), created.text
        assert created.status_code != 500
    _set_env(CC_API_TOKEN=None)


# --------------------------------------------------------------------------- #
# 3. Height is an entitlement, so it is re-derived at RENDER time.
# --------------------------------------------------------------------------- #


def test_height_is_reclamped_from_the_owner_plan_at_render_time(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db, worker

    client = sandbox.client
    body = _register(client, "stockpiler@example.com")
    token, user_id = body["token"], body["user"]["id"]
    db.update_user(user_id, plan="pro", plan_status="active")

    created = client.post(
        "/v1/jobs", json=_job_payload(height=3840), headers=_bearer(token)
    )
    job_id = created.json()["job_id"]
    assert created.json()["height"] == 3840
    job = db.get_job(job_id)
    assert job["height"] == 3840  # the row records what Pro was granted
    assert worker._height_for(job) == 3840

    # The subscription lapses before the stockpiled job is ever started.
    db.update_user(user_id, plan="free", plan_status="canceled")
    job = db.get_job(job_id)
    assert job["height"] == 3840, "the stored row is untouched — the RENDER decides"
    assert worker._height_for(job) == 1280, "4K must not survive the downgrade"

    # An upgrade landing between create and start is honoured too, and a
    # smaller ask is never raised by it.
    db.update_user(user_id, plan="starter", plan_status="active")
    assert worker._height_for(db.get_job(job_id)) == 1920
    assert worker._height_for({"user_id": user_id, "height": 960}) == 960
    # No account behind the job → the row's height stands (founder/dev jobs).
    assert worker._height_for({"user_id": "", "height": 3840}) == 3840
    assert worker._height_for({"user_id": "deleted-user", "height": 3840}) == 3840


def test_a_downgraded_account_renders_at_its_new_height(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    """The end-to-end proof: not the job row, the pixels that came out."""
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "downgrader@example.com")
    token, user_id = body["token"], body["user"]["id"]
    db.update_user(user_id, plan="starter", plan_status="active")

    created = client.post(
        "/v1/jobs",
        json=_job_payload(height=1920, size_bytes=source_video.stat().st_size),
        headers=_bearer(token),
    )
    job_id = created.json()["job_id"]
    assert created.json()["height"] == 1920
    assert client.put(
        f"/v1/uploads/{job_id}", content=source_video.read_bytes(), headers=_bearer(token)
    ).status_code == 200

    db.update_user(user_id, plan="free", plan_status="canceled")
    assert client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token)).status_code == 202

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "done", f"pipeline failed: {status.get('error')!r}"
    assert db.get_job(job_id)["height"] == 1920  # the row still says 1920
    assert status["clips"][0]["height"] == 1280, "the render followed the free plan"


# --------------------------------------------------------------------------- #
# 4. A box that cannot enforce what it sells must refuse to boot.
# --------------------------------------------------------------------------- #


def test_billing_without_a_founder_token_refuses_to_boot(
    sandbox: SimpleNamespace,
) -> None:
    """With CC_API_TOKEN unset the job routes accept anonymous callers, who
    have no account, no plan clamp and no quota — free 4K, unmetered. That is
    not a state a revenue-taking box may start in."""
    from clipcatalyst_api.main import create_app
    from clipcatalyst_api.settings import get_settings

    for billing in ("fake", "stripe"):
        _set_env(CC_BILLING=billing, CC_API_TOKEN=None)
        with pytest.raises(RuntimeError, match="CC_API_TOKEN"):
            create_app()
        with pytest.raises(RuntimeError, match="CC_API_TOKEN"):
            get_settings().validate()
        # The same box boots the moment the token is there.
        _set_env(CC_API_TOKEN=FOUNDER_TOKEN)
        assert create_app() is not None
    # Billing off is the dev default and stays token-free.
    _set_env(CC_BILLING="off", CC_API_TOKEN=None)
    get_settings().validate()
    assert create_app() is not None


def test_one_stripe_price_cannot_map_to_two_plans(sandbox: SimpleNamespace) -> None:
    """A shared price id makes price → plan ambiguous: the map is a dict build,
    so a webhook would grant whichever plan happened to be last."""
    from clipcatalyst_api import billing
    from clipcatalyst_api.settings import get_settings

    _set_env(
        CC_BILLING="fake",
        CC_API_TOKEN=FOUNDER_TOKEN,
        CC_STRIPE_PRICE_STARTER="price_shared",
        CC_STRIPE_PRICE_PRO="price_shared",
        CC_STRIPE_PRICE_ENTERPRISE="price_enterprise",
    )
    settings = get_settings()
    # Caught at gateway build time, so a mid-flight env change can't slip past
    # the startup check either.
    with pytest.raises(ValueError, match="price_shared"):
        billing.get_gateway(settings)
    with pytest.raises(RuntimeError, match="price_shared"):
        settings.validate()
    assert settings.duplicate_price_ids() == {
        "price_shared": ["CC_STRIPE_PRICE_STARTER", "CC_STRIPE_PRICE_PRO"]
    }

    # Distinct ids are fine, and an unset price is not a duplicate of another.
    _set_env(CC_STRIPE_PRICE_PRO="price_pro", CC_STRIPE_PRICE_ENTERPRISE="")
    settings = get_settings()
    assert settings.duplicate_price_ids() == {}
    settings.validate()
    assert billing.get_gateway(settings) is not None


def test_docs_are_off_when_a_founder_token_is_configured(
    sandbox: SimpleNamespace,
) -> None:
    """/docs, /redoc and /openapi.json enumerate every route and schema — a
    dev convenience, not something a public box should hand out."""
    from fastapi.testclient import TestClient

    from clipcatalyst_api.main import create_app

    # No lifespan needed for a routing check, so no `with`: these clients
    # never touch the database.
    assert TestClient(sandbox.app).get("/docs").status_code == 200

    _set_env(CC_API_TOKEN=FOUNDER_TOKEN)
    try:
        gated = TestClient(create_app())
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert gated.get(path).status_code == 404, path
        # The API itself is untouched — only the docs went away.
        assert gated.get("/v1/healthz").status_code == 200
    finally:
        _set_env(CC_API_TOKEN=None)


# --------------------------------------------------------------------------- #
# 5. Caching: nothing account-shaped may sit in a shared cache.
# --------------------------------------------------------------------------- #


def test_authenticated_responses_are_not_cacheable(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    registered = client.post(
        "/v1/auth/register", json={"email": "cache@example.com", "password": PASSWORD}
    )
    assert registered.status_code == 201
    token = registered.json()["token"]
    assert registered.headers["cache-control"] == "no-store"  # carries a token

    login = client.post(
        "/v1/auth/login", json={"email": "cache@example.com", "password": PASSWORD}
    )
    assert login.headers["cache-control"] == "no-store"
    me = client.get("/v1/me", headers=_bearer(token))
    assert me.headers["cache-control"] == "no-store"  # plan + usage
    logout = client.post("/v1/auth/logout", headers=_bearer(login.json()["token"]))
    assert logout.headers["cache-control"] == "no-store"


def test_clip_downloads_are_private_and_revalidated(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token = _register(client, "files@example.com")["token"]
    job_id = client.post(
        "/v1/jobs", json=_job_payload(), headers=_bearer(token)
    ).json()["job_id"]
    # A rendered clip on disk; the render itself is proven elsewhere.
    clip_dir = sandbox.data_dir / "clips" / job_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip-01.mp4").write_bytes(b"stand-in mp4 bytes")

    resp = client.get(f"/v1/files/{job_id}/clip-01.mp4", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "private, max-age=0"


# --------------------------------------------------------------------------- #
# 6. past_due is a dunning grace state, not an indefinite one.
# --------------------------------------------------------------------------- #


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="milliseconds")


def test_past_due_entitlements_expire_after_the_grace_window() -> None:
    from clipcatalyst_api.plans import PAST_DUE_GRACE_DAYS, effective_plan

    grace = timedelta(days=PAST_DUE_GRACE_DAYS)
    base = {"plan": "pro", "plan_status": "past_due"}
    # Inside the window (period ended yesterday; retries are still running).
    assert effective_plan({**base, "current_period_end": _iso(-timedelta(days=1))}) == "pro"
    # Just inside, and just outside.
    assert effective_plan({**base, "current_period_end": _iso(-grace + timedelta(hours=1))}) == "pro"
    assert effective_plan({**base, "current_period_end": _iso(-grace - timedelta(hours=1))}) == "free"
    # A period that has not even ended yet is obviously still entitled.
    assert effective_plan({**base, "current_period_end": _iso(timedelta(days=30))}) == "pro"
    # No period end at all (hand-edited row): documented as still entitled.
    assert effective_plan({**base, "current_period_end": ""}) == "pro"
    assert effective_plan({**base, "current_period_end": "not-a-date"}) == "pro"
    # The other statuses are untouched by the bound.
    stale = _iso(-grace - timedelta(days=365))
    assert effective_plan({"plan": "pro", "plan_status": "active", "current_period_end": stale}) == "pro"
    assert effective_plan({"plan": "pro", "plan_status": "canceled", "current_period_end": ""}) == "free"


def test_me_reports_the_grace_bound_plan(billing_sandbox: SimpleNamespace) -> None:
    """/v1/me reads the same effective plan enforcement does, so a lapsed
    past_due account is shown free-tier limits rather than the plan it stopped
    paying for."""
    from clipcatalyst_api import db

    client = billing_sandbox.client
    body = _register(client, "dunning@example.com")
    token, user_id = body["token"], body["user"]["id"]
    # A period that ended over a year ago — past any defensible grace window,
    # so this asserts the behaviour rather than the constant's exact value.
    db.update_user(
        user_id,
        plan="pro",
        plan_status="past_due",
        current_period_end=_iso(-timedelta(days=400)),
    )
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == "pro" and me["plan_status"] == "past_due"  # stored facts
    assert me["quota"]["limit"] == 3  # ...but the entitlements are free's
    assert me["entitlements"]["max_height"] == 1280
    assert me["entitlements"]["watermark_required"] is True


# --------------------------------------------------------------------------- #
# 7. Checkout mints Stripe sessions, so it gets a window like the credential
#    routes.
# --------------------------------------------------------------------------- #


def test_checkout_is_rate_limited(billing_sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import auth

    client = billing_sandbox.client
    token = _register(client, "spender@example.com")["token"]
    auth.reset_rate_limits()
    try:
        codes = [
            client.post(
                "/v1/billing/checkout", json={"plan": "pro"}, headers=_bearer(token)
            ).status_code
            for _ in range(auth.RATE_LIMIT_PER_MINUTE + 1)
        ]
        assert codes[:-1] == [200] * auth.RATE_LIMIT_PER_MINUTE, codes
        assert codes[-1] == 429, codes
    finally:
        auth.reset_rate_limits()


# --------------------------------------------------------------------------- #
# 8. Passwords are compared in one Unicode normal form.
# --------------------------------------------------------------------------- #


def test_the_same_typed_password_signs_in_from_either_unicode_form(
    sandbox: SimpleNamespace,
) -> None:
    client = sandbox.client
    composed = unicodedata.normalize("NFC", "passwörd-café-123")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed, "the two forms must differ for this to prove anything"

    assert client.post(
        "/v1/auth/register", json={"email": "uni@example.com", "password": composed}
    ).status_code == 201
    signed_in = client.post(
        "/v1/auth/login", json={"email": "uni@example.com", "password": decomposed}
    )
    assert signed_in.status_code == 200, "the same characters must be the same password"
    # A genuinely different password is still wrong, normalization or not.
    assert client.post(
        "/v1/auth/login", json={"email": "uni@example.com", "password": composed + "x"}
    ).status_code == 401


def test_hashes_written_before_normalization_still_verify() -> None:
    """Migration reality: pre-normalization rows hold the raw bytes and cannot
    be rewritten without the plaintext, so verify must still accept the exact
    form that was registered — breaking those logins to force a migration
    would lock existing users out of their own accounts."""
    import base64
    import hashlib

    from clipcatalyst_api import auth

    decomposed = unicodedata.normalize("NFD", "passwörd-café-123")
    # Exactly what hash_password did before normalization landed.
    salt = b"0123456789abcdef0123456789abcdef"
    digest = hashlib.scrypt(
        decomposed.encode("utf-8"), salt=salt, n=16384, r=8, p=1,
        maxmem=64 * 1024 * 1024, dklen=32,
    )
    legacy = (
        "scrypt$16384$8$1$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )
    assert auth.verify_password(decomposed, legacy), "an existing login must survive"
    assert not auth.verify_password(decomposed + "x", legacy)
    # New hashes are stored normalized, so either form verifies against them.
    fresh = auth.hash_password(decomposed)
    assert auth.verify_password(decomposed, fresh)
    assert auth.verify_password(unicodedata.normalize("NFC", decomposed), fresh)
