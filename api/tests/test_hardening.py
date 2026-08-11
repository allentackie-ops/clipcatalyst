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

Sections 9-12 are the second round, from a skeptic re-verifying the first: a
reaped job that walked off with its owner's quota reservation, a rate-limit
table nobody ever emptied, a trusted-proxy entry that deleted the limiter
without saying so, and a per-clip handler that mistook Celery's soft kill for
a broken codec.

Section 13 is the round-two fix's own fallout: moving stall reconciliation onto
the hourly beat made the refund reliable, and made the worker's unguarded
`update_job(status=...)` systematic — a backlogged job was refunded, then
rendered and delivered against a reservation that no longer existed. Its tests
race the two writers both deterministically and on real threads.

Section 10 was rewritten in round four. Its first version guarded a mechanism
— an in-process window table with a sweep and a cap — that produced a new
defect in every round of review, so the mechanism was replaced rather than
patched again, and the tests now assert the properties of what replaced it
(counters in Redis, keyed by their own window). Two of them assert an absence
that the previous tests asserted the presence of; the reasoning is in that
section's banner, and every one of them still fails against the code it
replaced.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
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

# A timestamp far enough back to be past any TTL or stall window.
ANCIENT = "2000-01-01T00:00:00.000+00:00"

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


@pytest.fixture()
def quiet_sandbox(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """`sandbox` with the pipeline stubbed out: /start still claims the job and
    reserves its quota, then dispatches nothing.

    That is not a shortcut, it is the shape of the production (redis) queue —
    /start returns long before anything renders — and it is precisely the state
    the reaper and the stall reconciler find a job in."""
    from clipcatalyst_api import main

    monkeypatch.setattr(main, "process_job", SimpleNamespace(delay=lambda job_id: None))
    return sandbox


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


def _month() -> str:
    """The current 'YYYY-MM' UTC month key — how usage rows are keyed."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ready_job(
    client, token: str | None, *, content: bytes = b"stand-in source bytes", **overrides
) -> str:
    """A job whose source is on disk — the state /start reserves quota in."""
    headers = _bearer(token) if token else {}
    created = client.post("/v1/jobs", json=_job_payload(**overrides), headers=headers)
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    ack = client.put(f"/v1/uploads/{job_id}", content=content, headers=headers)
    assert ack.status_code == 200, ack.text
    return job_id


def _start(client, job_id: str, token: str | None = None):  # noqa: ANN202 - Response
    return client.post(
        f"/v1/jobs/{job_id}/start", headers=_bearer(token) if token else {}
    )


def _age(sandbox: SimpleNamespace, job_id: str, **columns: str) -> None:
    """Backdate a job row's timestamps in place.

    Neither created_at nor updated_at is writable through db.update_job (both
    are bookkeeping the DAO owns), and the reaper keys off wall-clock cutoffs —
    so aging the row is how the 48 h TTL and the stall window are reached
    without sleeping through them.
    """
    conn = sqlite3.connect(sandbox.data_dir / "jobs.sqlite3")
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE jobs SET {assignments} WHERE id = ?", (*columns.values(), job_id)
    )
    conn.commit()
    conn.close()


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


# --------------------------------------------------------------------------- #
# 9. A reaped job must not walk off with its owner's month.
# --------------------------------------------------------------------------- #


def test_a_reaped_job_hands_its_quota_reservation_back(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """The reaper deletes rows by created_at whatever their status, and the
    settlement token lives ON the row (jobs.usage_reserved). So a job still
    queued when the TTL caught it took its owner's whole month with it — and
    unrecoverably, because reconcile_stalled has nothing left to settle once
    the row is gone. A free account was locked out permanently, having rendered
    exactly nothing."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired

    client = quiet_sandbox.client
    body = _register(client, "reaped@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    job_id = _ready_job(client, token, count=3)  # the whole free month
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 3
    assert db.get_job(job_id)["status"] == "queued"  # nothing rendered, ever

    # CC_JOB_TTL_HOURS passes with the API process still up, so the boot-time
    # reconciler never runs; the hourly reaper is what arrives.
    _age(quiet_sandbox, job_id, created_at=ANCIENT, updated_at=ANCIENT)
    assert reap_expired() == 1
    assert db.get_job(job_id) is None

    assert db.get_usage(user_id, month) == 0, "a month was spent on nothing"
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 0
    # ...and that is real quota, not just a number on the account page.
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202


def test_reaping_a_settled_job_neither_re_bills_nor_refunds_twice(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """The release is a settlement like any other: once-only, and clamped at
    zero. Clips the owner actually got stay billed when the reaper eventually
    removes their row, and no sequence of sweeps can hand back more than was
    ever reserved."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired

    client = quiet_sandbox.client
    body = _register(client, "settled-then-reaped@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202
    assert db.settle_usage(job_id, 1) == -2  # one clip rendered, two returned
    assert db.get_usage(user_id, month) == 1

    _age(quiet_sandbox, job_id, created_at=ANCIENT, updated_at=ANCIENT)
    assert reap_expired() == 1
    assert db.get_usage(user_id, month) == 1, "a delivered clip stays billed"
    # A second sweep finds nothing, and the counter never goes negative.
    assert reap_expired() == 0
    assert db.get_usage(user_id, month) == 1


def test_the_hourly_sweep_reconciles_stalled_jobs_without_an_api_restart(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """reconcile_stalled ran ONLY from the API's startup hook, so on a box
    whose API stays up past CC_JOB_TTL_HOURS a job abandoned by a killed worker
    held its owner's quota right up until the reaper deleted the row. The beat
    task runs the same reconciliation hourly, so the refund lands long before
    the TTL — with no restart involved."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired_task

    client = quiet_sandbox.client
    body = _register(client, "stalled@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202
    # A worker claimed it and was killed: still `processing`, last touched far
    # longer ago than a whole render window. created_at is left alone — this
    # row is nowhere near the TTL, so the reaper itself will not touch it.
    assert db.transition_status(job_id, expect="queued", to="processing")
    _age(quiet_sandbox, job_id, updated_at=ANCIENT)

    assert reap_expired_task() == 0, "nothing here is old enough to reap"
    job = db.get_job(job_id)
    assert job is not None, "the row is young; only the reservation was stale"
    assert job["status"] == "failed"
    assert job["usage_reserved"] == 0
    assert db.get_usage(user_id, month) == 0
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202
    # Repeating the sweep re-fails nothing and re-refunds nothing.
    assert reap_expired_task() == 0
    assert db.get_usage(user_id, month) == 3  # the job started just above


# --------------------------------------------------------------------------- #
# 10. The rate limiter counts in Redis.
#
#     Round four. The in-process window table produced a fresh defect in every
#     round, because each round patched it instead of replacing it: a cap whose
#     fallback only diverted keys it had NEVER SEEN, so a peer filled the table
#     once and then recycled the same forged addresses, each still worth a full
#     10/min (S-2); the same full table answering every never-seen client out
#     of the shared socket peer's bucket, which the attacker kept over the
#     ceiling, so five innocent clients behind the proxy were locked out (S-3);
#     and a sweep that walked that dict, unlocked, while other threadpool
#     workers wrote to it (S-4).
#
#     None of the three is patched here. The mechanism is gone. Counters are an
#     atomic INCR in Redis under a key that names its own window and carries a
#     TTL, and the no-Redis fallback is a fixed-size array behind one lock.
#     What the tests below assert is that the limiter's answer depends on the
#     asking client's own window and on nothing else — not on how full a table
#     is, not on what anybody else sent, not on which thread ran first.
# --------------------------------------------------------------------------- #


def _frozen_minute(monkeypatch: pytest.MonkeyPatch, minute: int) -> dict:
    """Pin auth's window clock so a test never straddles a real minute edge."""
    from clipcatalyst_api import auth

    clock = {"minute": minute}
    monkeypatch.setattr(auth, "_current_minute", lambda: clock["minute"])
    return clock


class _FakeRedis:
    """Exactly the slice of redis.Redis the limiter uses, kept in a dict.

    `eval` mirrors the shipped script's contract — INCR, and EXPIRE only on the
    call that created the key — so these tests exercise the real key layout,
    the real TTL and the real reply handling with no live server anywhere near
    the suite. Setting `fail` is what an outage looks like from in here.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.scripts: list[str] = []
        self.fail: Exception | None = None

    def eval(self, script: str, numkeys: int, key: str, ttl: int) -> int:
        if self.fail is not None:
            raise self.fail
        assert numkeys == 1, numkeys
        self.scripts.append(script)
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        if count == 1:
            assert key not in self.ttls, "EXPIRE must only ever touch a fresh key"
            self.ttls[key] = int(ttl)
        return count


@contextmanager
def _redis_limiter(fake: _FakeRedis) -> Iterator[_FakeRedis]:
    """Point the limiter at `fake`, as if this box were running CC_QUEUE=redis."""
    from clipcatalyst_api import auth

    def stub(url: str) -> _FakeRedis:
        assert url, "the limiter must build its client from CC_REDIS_URL"
        return fake

    stub.cache_clear = lambda: None  # reset_rate_limits() drops the handle
    real, auth._redis_client = auth._redis_client, stub
    _set_env(CC_QUEUE="redis")
    auth.reset_rate_limits()
    try:
        yield fake
    finally:
        auth.reset_rate_limits()
        auth._redis_client = real
        _set_env(CC_QUEUE="eager", CC_RATE_LIMIT_FAIL_OPEN=None)


@pytest.fixture()
def cheap_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take scrypt off the login path for the flood tests.

    They drive hundreds of attempts to reach a LIMITER state and assert which
    of them the limiter accepted; burning ~50 ms of key derivation per attempt
    reaches the same answer a minute later. The credential check itself is
    covered for real in test_auth.py.
    """
    from clipcatalyst_api import auth

    monkeypatch.setattr(auth, "verify_password", lambda password, stored: False)


def _login_from(client, forwarded: str) -> int:
    """One failed login claiming to come from `forwarded`; the status code."""
    return client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever12"},
        headers={"X-Forwarded-For": forwarded},
    ).status_code


def test_the_limiter_counts_in_redis_under_a_key_that_expires_itself(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path, exercised with a stub client.

    The counter is one atomic INCR against a key that carries its own window,
    given a TTL by the call that created it and by no other call. That is the
    whole design: a new minute is a new key rather than a counter somebody has
    to reset, and a finished window is Redis's to reclaim rather than something
    this process has to walk a table to find.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth

    clock = _frozen_minute(monkeypatch, 4_000_000)
    fake = _FakeRedis()
    with _redis_limiter(fake):
        for _ in range(auth.RATE_LIMIT_PER_MINUTE):
            auth.enforce_rate_limit("198.51.100.5", "login")
        with pytest.raises(HTTPException) as refused:
            auth.enforce_rate_limit("198.51.100.5", "login")
        assert refused.value.status_code == 429

        key = f"cc:rl:login:{clock['minute']}:198.51.100.5"
        assert list(fake.counts) == [key], fake.counts
        assert fake.counts[key] == auth.RATE_LIMIT_PER_MINUTE + 1
        # Expiry is what bounds the key space, so the TTL is not optional.
        assert fake.ttls == {key: auth._WINDOW_TTL_S}
        assert "INCR" in fake.scripts[0] and "EXPIRE" in fake.scripts[0]

        # The next minute is a different key: the allowance comes back without
        # anything being reset, swept or evicted.
        clock["minute"] += 1
        auth.enforce_rate_limit("198.51.100.5", "login")
        rolled = f"cc:rl:login:{clock['minute']}:198.51.100.5"
        assert fake.counts[rolled] == 1
        assert fake.ttls[rolled] == auth._WINDOW_TTL_S

        # Route and client each get their own counter, as they always did.
        auth.enforce_rate_limit("198.51.100.5", "register")
        auth.enforce_rate_limit("198.51.100.6", "login")
        assert len(fake.counts) == 4

        # And nothing was written in process on this path.
        assert set(auth._memory_windows) == {(-1, 0)}


def test_an_unreachable_redis_fails_closed_unless_the_operator_says_otherwise(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt that cannot be COUNTED is not allowed through by default.

    Failing open would hand an attacker unmetered password guessing at the one
    moment nobody is watching anything but the outage — and it is reachable:
    whoever can knock Redis over can also un-limit login. Closed is affordable
    here because Redis is the Celery broker, so a box that cannot reach it
    cannot render a clip either. The opposite trade stays available, but an
    operator has to write it down.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth

    _frozen_minute(monkeypatch, 4_100_000)
    fake = _FakeRedis()
    fake.fail = ConnectionError("connection refused")
    with _redis_limiter(fake):
        for _ in range(3):
            with pytest.raises(HTTPException) as refused:
                auth.enforce_rate_limit("198.51.100.7", "login")
            assert refused.value.status_code == 429
            # The client is told to wait, not told what broke.
            assert refused.value.detail == auth._RATE_LIMITED

        _set_env(CC_RATE_LIMIT_FAIL_OPEN="on")
        for _ in range(auth.RATE_LIMIT_PER_MINUTE + 5):
            auth.enforce_rate_limit("198.51.100.7", "login")  # taken knowingly
        _set_env(CC_RATE_LIMIT_FAIL_OPEN=None)

        # A Redis that answers is unaffected by the knob in either position,
        # and the outage left no counter behind to poison the recovery.
        fake.fail = None
        auth.enforce_rate_limit("198.51.100.7", "login")
        assert list(fake.counts.values()) == [1]


def test_a_recycled_forwarded_flood_never_buys_a_free_attempt(
    sandbox: SimpleNamespace, cheap_login: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-2. The cap's fallback only diverted keys the table had never seen, so
    a peer filled the table ONCE and then recycled the same forged addresses
    for the rest of the minute — each of them still worth its own full 10/min,
    while addresses unlucky enough to arrive after the table filled were
    squeezed to nothing. At the shipped cap that was ~100,000 accepted attempts
    a minute from one peer, and it was reached by the attacker CHOOSING to stay
    under a ceiling this process enforced on strings the attacker supplied.

    There is no cap now, and so no mode for anyone to flip the limiter into:
    every identity is held to its own window and to nothing else. Recycling
    forty addresses ten times over buys exactly what asking once buys — ten a
    minute each, no more and, decisively, no less, because an identity's
    ceiling must never depend on how many OTHER identities have been seen.
    """
    from clipcatalyst_api import auth

    _frozen_minute(monkeypatch, 4_200_000)
    # The old ceiling, small enough to reach in a test. raising=False so this
    # reads identically against the code that had a cap and the code that has
    # none — the point is that the cap is no longer part of the answer.
    monkeypatch.setattr(auth, "MAX_RATE_WINDOWS", 20, raising=False)
    client = _proxy_client(sandbox, PROXY_PEER)
    _set_env(CC_TRUSTED_PROXIES=PROXY_CIDR)
    auth.reset_rate_limits()
    try:
        forged = [f"203.0.113.{i + 1}" for i in range(40)]
        accepted = {address: 0 for address in forged}
        for _ in range(10):  # ten passes over the same set, from one peer
            for address in forged:
                code = _login_from(client, address)
                assert code in (401, 429), code
                accepted[address] += code == 401

        assert max(accepted.values()) == auth.RATE_LIMIT_PER_MINUTE
        assert min(accepted.values()) == auth.RATE_LIMIT_PER_MINUTE, accepted
        # The flood bought no memory either: the fallback table is a fixed
        # array, the same size after 400 forged identities as before them.
        assert len(auth._memory_windows) == auth._MEMORY_SLOTS
    finally:
        auth.reset_rate_limits()
        _set_env(CC_TRUSTED_PROXIES="")


def test_a_flood_does_not_lock_out_clients_the_limiter_has_never_seen(
    sandbox: SimpleNamespace, cheap_login: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-3. Once the table was full, every never-seen client was counted in the
    socket PEER's bucket — and behind the reverse proxy DEPLOY.md mandates that
    peer is the proxy, i.e. everybody. An attacker held that bucket over the
    ceiling with forged addresses, and five innocent clients were refused on
    their FIRST attempt of the day: precisely the collateral the trusted-proxy
    rule was added to prevent, reintroduced by the fix for the flood.

    Nothing is shared now. A client that has not been seen has an empty window,
    whatever anyone else has been doing.
    """
    from clipcatalyst_api import auth

    _frozen_minute(monkeypatch, 4_300_000)
    monkeypatch.setattr(auth, "MAX_RATE_WINDOWS", 20, raising=False)
    client = _proxy_client(sandbox, PROXY_PEER)
    _set_env(CC_TRUSTED_PROXIES=PROXY_CIDR)
    auth.reset_rate_limits()
    try:
        # One peer, forty forged addresses: enough to fill the old table and
        # then drive the shared peer bucket well past 10.
        for i in range(40):
            _login_from(client, f"203.0.113.{i + 1}")

        # Five bystanders, one attempt each, none of them seen before.
        victims = [f"198.51.100.{n}" for n in (11, 12, 13, 14, 15)]
        codes = [_login_from(client, address) for address in victims]
        assert codes == [401] * 5, f"innocent clients were locked out: {codes}"

        # And they still have their own full window afterwards.
        for _ in range(auth.RATE_LIMIT_PER_MINUTE - 1):
            assert _login_from(client, victims[0]) == 401
        assert _login_from(client, victims[0]) == 429
        assert _login_from(client, victims[1]) == 401, "one client, one window"
    finally:
        auth.reset_rate_limits()
        _set_env(CC_TRUSTED_PROXIES="")


def test_concurrent_threads_neither_raise_nor_lose_a_count(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-4. The credential routes are `def`, so FastAPI runs them in the anyio
    threadpool: several workers really are inside the limiter at once. The old
    sweep iterated the shared window dict with no lock while they wrote to it,
    and claimed the swept minute BEFORE it walked, so a second thread sailed
    past the guard mid-walk — 'dictionary changed size during iteration', 148
    runs in 150 with a shortened switch interval, and it survived in production
    only because the cap kept the walk short.

    There is nothing to walk now, and one lock spans the only read-modify-write
    there is. Two things are asserted with real threads and the switch interval
    turned down. The first is the finding: the limiter raises nothing but its
    own 429, where the sweep raised RuntimeError. The second is the ceiling
    holding while twelve threads race one window — which, measured honestly,
    the old code also survived: 30/30 trials, because CPython happened to run
    that increment without switching. "Happened to" is the whole complaint. It
    is a guarantee here and an accident there, and an accident is not something
    a limiter can be built on.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth

    clock = _frozen_minute(monkeypatch, 4_400_000)
    auth.reset_rate_limits()
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        errors: list[BaseException] = []

        def hammer(worker: int) -> None:
            for i in range(500):
                try:
                    auth.enforce_rate_limit(f"198.51.{worker}.{i % 256}", "login")
                except HTTPException:
                    pass  # a 429 is an answer, not a failure
                except BaseException as error:  # noqa: BLE001 - that IS the finding
                    errors.append(error)

        def roll() -> None:
            for _ in range(300):
                clock["minute"] += 1  # end windows under the workers' feet
                time.sleep(0.001)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(16)]
        threads.append(threading.Thread(target=roll))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [], errors[:3]

        # Twelve threads, five attempts each, one client, one window: the
        # ceiling is a ceiling no matter how the increments interleave.
        auth.reset_rate_limits()
        counted = threading.Semaphore(0)
        accepted: list[int] = []
        lock = threading.Lock()

        def guess() -> None:
            counted.acquire()
            for _ in range(5):
                try:
                    auth.enforce_rate_limit("198.51.100.9", "login")
                except HTTPException:
                    continue
                with lock:
                    accepted.append(1)

        racers = [threading.Thread(target=guess) for _ in range(12)]
        for thread in racers:
            thread.start()
        for _ in racers:
            counted.release()  # start them as close together as possible
        for thread in racers:
            thread.join()
        assert sum(accepted) == auth.RATE_LIMIT_PER_MINUTE, accepted
    finally:
        sys.setswitchinterval(switch_interval)
        auth.reset_rate_limits()


def test_without_redis_the_fallback_is_fixed_size_and_still_a_limiter(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dev/test path (CC_QUEUE=eager): no Redis to reach, counters live in
    process. The old table held one entry per client and needed the sweep to
    shed them — the sweep being the thing S-4 raced on, and the growth being
    what the cap was invented to bound. This one is a fixed array: three
    thousand distinct clients cost exactly what one costs, a window ends when
    its own slot is overwritten, and nothing iterates or evicts.
    """
    from fastapi import HTTPException

    from clipcatalyst_api import auth
    from clipcatalyst_api.settings import get_settings

    clock = _frozen_minute(monkeypatch, 4_500_000)
    auth.reset_rate_limits()
    try:
        assert get_settings().queue == "eager"
        for i in range(3000):
            auth.enforce_rate_limit(f"198.51.{i // 256}.{i % 256}", "login")
        assert len(auth._memory_windows) == auth._MEMORY_SLOTS
        # No Redis handle was ever built on this path.
        assert auth._redis_client.cache_info().currsize == 0

        auth.reset_rate_limits()
        for _ in range(auth.RATE_LIMIT_PER_MINUTE):
            auth.enforce_rate_limit("198.51.100.1", "login")
        with pytest.raises(HTTPException) as refused:
            auth.enforce_rate_limit("198.51.100.1", "login")
        assert refused.value.status_code == 429
        # A different route is a different window...
        auth.enforce_rate_limit("198.51.100.1", "register")
        # ...a different client is untouched by that refusal...
        auth.enforce_rate_limit("198.51.100.2", "login")
        # ...and the next minute is a clean window, with nothing swept to get it.
        clock["minute"] += 1
        auth.enforce_rate_limit("198.51.100.1", "login")
    finally:
        auth.reset_rate_limits()


# --------------------------------------------------------------------------- #
# 11. Trusting every address is not a configuration, it is a deleted limiter.
# --------------------------------------------------------------------------- #


def test_trusting_every_address_as_a_proxy_refuses_to_boot(
    sandbox: SimpleNamespace,
) -> None:
    """CC_TRUSTED_PROXIES=0.0.0.0/0 makes every direct client its own trusted
    proxy: it picks its own rate-limit bucket by rotating X-Forwarded-For and
    is never counted twice — 429s drop to zero and nothing says a word.
    Settings.validate already refuses to boot a box that cannot enforce what it
    sells; an all-addresses trust entry is the same class of mistake."""
    from clipcatalyst_api.main import create_app
    from clipcatalyst_api.settings import get_settings

    # Both families, and a wide entry hiding behind a legitimate narrow one.
    for entry in ("0.0.0.0/0", "::/0", f"{PROXY_CIDR},0.0.0.0/0", "0.0.0.0/0,::/0"):
        _set_env(CC_TRUSTED_PROXIES=entry)
        with pytest.raises(RuntimeError, match="trusts EVERY address"):
            get_settings().validate()
        with pytest.raises(RuntimeError, match="trusts EVERY address"):
            create_app()

    _set_env(CC_TRUSTED_PROXIES="0.0.0.0/0")
    assert get_settings().wide_open_proxies() == ["0.0.0.0/0"]

    # Narrow blocks — the whole point of the setting — still boot, and so does
    # the shipped default of trusting nobody. Nothing here depends on billing:
    # this sandbox runs CC_BILLING=off, where validate() used to return early.
    for entry in ("", PROXY_CIDR, "172.18.0.2", f"{PROXY_CIDR},2001:db8::/32", "0.0.0.0/1"):
        _set_env(CC_TRUSTED_PROXIES=entry)
        settings = get_settings()
        assert settings.wide_open_proxies() == []
        settings.validate()
        assert create_app() is not None
    # An unparseable entry trusts nobody, so it is nobody's problem (auth logs
    # and drops it) — it must not become a boot failure of its own.
    _set_env(CC_TRUSTED_PROXIES="nonsense")
    get_settings().validate()
    _set_env(CC_TRUSTED_PROXIES="")


# --------------------------------------------------------------------------- #
# 12. Celery's soft kill is a torn-down task, not a stage that failed.
# --------------------------------------------------------------------------- #


def _plan_n(worker, count: int):  # noqa: ANN202 - patched plan_clips
    """A plan_clips stand-in returning `count` copies of the real first plan."""
    real = worker.plan_clips

    def planner(transcript, features, options):
        plans = real(transcript, features, options)
        assert plans, "the fixture video must plan at least one clip"
        return [dataclasses.replace(plans[0], id=f"clip-{n + 1}") for n in range(count)]

    return planner


def test_a_soft_time_limit_mid_render_is_a_timeout_not_a_codec_error(
    quiet_sandbox: SimpleNamespace,
    source_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SoftTimeLimitExceeded subclasses Exception, so the per-clip
    `except Exception: … continue` swallowed it: the job blamed the user's
    codec, and — worse — the loop spent the ~60 s of grace before the HARD kill
    rendering the NEXT clip, so the process was SIGKILLed with the row still in
    `processing` and still holding its owner's quota. Which is exactly the
    stranded reservation section 9 is about."""
    from celery.exceptions import SoftTimeLimitExceeded

    from clipcatalyst_api import db, worker
    from clipcatalyst_api.pipeline.types import PipelineError

    client = quiet_sandbox.client
    body = _register(client, "softkill@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    video = source_video.read_bytes()
    job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=3)
    assert _start(client, job_id, token).status_code == 202  # reserved, queued
    assert db.get_usage(user_id, month) == 3

    monkeypatch.setattr(worker, "plan_clips", _plan_n(worker, 3))
    rendered: list[int] = []

    def exploding_render(src, plan, out_path, opts, settings, on_progress, track):
        rendered.append(len(rendered) + 1)
        if len(rendered) == 1:
            raise PipelineError("that clip's codec is unusual")  # an ordinary miss
        raise SoftTimeLimitExceeded()  # the worker is being torn down

    monkeypatch.setattr(worker, "render_clip", exploding_render)

    # process_job re-raises so Celery marks the task FAILURE too.
    with pytest.raises(SoftTimeLimitExceeded):
        worker.process_job(job_id)

    assert rendered == [1, 2], "the loop must stop at the soft kill, not roll on"
    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "failed"
    assert status["error"] == worker._TIMEOUT_ERROR, "an honest timeout, not a codec"
    assert status["error"] != worker._ALL_RENDERS_FAILED_ERROR
    # And the reservation went back, so the month is not lost to a timeout.
    assert db.get_job(job_id)["usage_reserved"] == 0
    assert db.get_usage(user_id, month) == 0


@pytest.mark.parametrize("stage", ["segment_embeddings", "detect_faces"])
def test_the_best_effort_stages_re_raise_the_soft_time_limit(
    quiet_sandbox: SimpleNamespace,
    source_video: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """The same defect, audited across worker.py: diarization and reframing are
    best-effort and swallow everything, so a soft kill arriving in either was
    logged as a degraded stage and the job carried on into the hard kill. Both
    used to finish `done`; both must now report the timeout."""
    from celery.exceptions import SoftTimeLimitExceeded

    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    body = _register(client, f"softkill-{stage}@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    video = source_video.read_bytes()
    job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=1)
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 1

    def soft_kill(*args, **kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(worker, stage, soft_kill)
    with pytest.raises(SoftTimeLimitExceeded):
        worker.process_job(job_id)

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "failed", f"{stage} swallowed the soft kill"
    assert status["error"] == worker._TIMEOUT_ERROR
    assert db.get_usage(user_id, month) == 0


# --------------------------------------------------------------------------- #
# 13. Reconciliation and the worker are two writers on one row, so the ROW
#     decides. A reconciled job may not be resurrected, rendered and given away.
# --------------------------------------------------------------------------- #


def _clip_file(sandbox: SimpleNamespace, job_id: str, name: str = "clip-01.mp4") -> Path:
    """Where LocalStorage puts a rendered clip — what /v1/files serves."""
    return sandbox.data_dir / "clips" / job_id / name


def _download(client, job_id: str, token: str, name: str = "clip-01.mp4") -> int:
    """The status code of a direct clip fetch.

    Deliberately NOT read from clips_json: /v1/files authorizes on the job ROW
    and then serves whatever file is there, so a job that reports no clips can
    still be handing them out under a guessable name.
    """
    return client.get(f"/v1/files/{job_id}/{name}", headers=_bearer(token)).status_code


def _stall(sandbox: SimpleNamespace, job_id: str) -> None:
    """Make a job look abandoned: last touched longer ago than a render window.

    This is an ordinary queue backlog, not an attack — the default window is
    render_timeout_s + 300 = 20 minutes.
    """
    _age(sandbox, job_id, updated_at=ANCIENT)


def test_a_reconciled_job_is_not_resurrected_rendered_and_given_away(
    quiet_sandbox: SimpleNamespace, source_video: Path
) -> None:
    """[A4] The hourly sweep fails and REFUNDS anything still queued past
    render_timeout_s + slack — 20 minutes, which a busy box reaches on backlog
    alone. Nothing told the worker and nothing stopped it: it popped the job,
    blindly wrote status=processing over the terminal row, rendered, flipped it
    to `done` with update_job, and settle_usage then landed on a token
    reconciliation had already cleared. status=done, clips=1, download 200,
    usage_after=0 — a clip delivered and billed to nobody.

    The claim is now a transition the worker can lose, so a run that does not
    own the row does no work and writes nothing."""
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    body = _register(client, "resurrected@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    video = source_video.read_bytes()
    job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=1)
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 1, "the reservation /start took"

    # 20 minutes of backlog, then the beat tick. The task is still in the broker.
    _stall(quiet_sandbox, job_id)
    assert worker.reconcile_stalled() == 1
    assert db.get_job(job_id)["status"] == "failed"
    assert db.get_usage(user_id, month) == 0, "the sweep handed the month back"

    # ...and only NOW does the worker pop it.
    worker.process_job(job_id)

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "failed", "a refunded job was rendered and marked done"
    assert status["clips"] == []
    assert db.get_job(job_id)["usage_reserved"] == 0
    assert db.get_usage(user_id, month) == 0
    assert _download(client, job_id, token) == 404, "a free clip is still downloadable"
    assert not _clip_file(quiet_sandbox, job_id).exists()


def test_a_stalling_free_plan_cannot_be_drained_round_after_round(
    quiet_sandbox: SimpleNamespace, source_video: Path
) -> None:
    """[A4b] The same defect compounded into unmetered service. Free is 3 clips
    a month; each round reserved one, the sweep refunded it, and the worker
    delivered the clip anyway — so the counter kept returning to zero and every
    /start was accepted. Three rounds: delivered=3, billed=0.

    The invariant is the one that pays the bills: clips a customer can actually
    download may never exceed clips billed."""
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    body = _register(client, "drainer@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()
    video = source_video.read_bytes()

    delivered = 0
    for _round in range(3):
        job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=1)
        # Every /start is accepted, exactly as in the report — that is the
        # refund working, not the hole. What must not follow is a free clip.
        assert _start(client, job_id, token).status_code == 202
        _stall(quiet_sandbox, job_id)
        assert worker.reconcile_stalled() == 1
        worker.process_job(job_id)

        status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
        assert status["status"] == "failed", f"round {_round}: refunded, then rendered"
        if _download(client, job_id, token) == 200:
            delivered += 1

    billed = db.get_usage(user_id, month)
    assert delivered <= billed, f"delivered={delivered} billed={billed}"
    assert delivered == 0, "nothing was billed, so nothing may have been handed over"
    # The account is exactly where it started: nothing taken, nothing given.
    assert billed == 0
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202


def test_a_sweep_landing_mid_run_wins_and_the_worker_publishes_nothing(
    quiet_sandbox: SimpleNamespace, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse order, which the round-two fix also left open: the sweep
    arrives while the worker is mid-render. It refunded the reservation, and the
    worker — already past every check — then wrote `done` and settled onto the
    cleared token. Same free clip, opposite interleaving.

    Now the terminal write is a guarded transition carrying clips_json and the
    settlement in ONE statement, so losing it publishes nothing at all."""
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    body = _register(client, "midrun@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    video = source_video.read_bytes()
    job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=1)
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 1

    real_render = worker.render_clip

    def render_then_sweep(*args, **kwargs):
        """Render for real, then let the beat tick land before we go terminal."""
        rendered = real_render(*args, **kwargs)
        _stall(quiet_sandbox, job_id)
        assert worker.reconcile_stalled() == 1, "the sweep must take the row here"
        return rendered

    monkeypatch.setattr(worker, "render_clip", render_then_sweep)
    worker.process_job(job_id)

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "failed", "the worker overwrote a refunded row"
    assert status["clips"] == [], "clips_json must move with the status, not before it"
    assert db.get_usage(user_id, month) == 0, "refunded once, and not billed after"
    assert db.get_job(job_id)["usage_reserved"] == 0
    # The clip really was rendered and stored — it must not survive the loss.
    assert _download(client, job_id, token) == 404
    assert not _clip_file(quiet_sandbox, job_id).exists()


def test_a_normal_completion_settles_once_and_the_sweep_leaves_it_alone(
    quiet_sandbox: SimpleNamespace, source_video: Path
) -> None:
    """The other side of the guard: a job that finishes on time must still bill,
    exactly once, and no number of later sweeps may refund a delivered clip."""
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    body = _register(client, "normal@example.com")
    token, user_id = body["token"], body["user"]["id"]
    month = _month()

    video = source_video.read_bytes()
    job_id = _ready_job(client, token, content=video, size_bytes=len(video), count=1)
    assert _start(client, job_id, token).status_code == 202
    worker.process_job(job_id)

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "done", f"pipeline failed: {status.get('error')!r}"
    assert len(status["clips"]) == 1
    assert _download(client, job_id, token) == 200, "a billed clip must be downloadable"
    assert db.get_usage(user_id, month) == 1
    assert db.get_job(job_id)["usage_reserved"] == 0

    # A sweep afterwards — even with the row backdated past every cutoff — has
    # no live row to fail, so the delivered clip stays billed and stays served.
    _stall(quiet_sandbox, job_id)
    assert worker.reconcile_stalled() == 0
    assert db.get_job(job_id)["status"] == "done"
    assert db.get_usage(user_id, month) == 1, "a delivered clip was refunded"
    assert _download(client, job_id, token) == 200

    # And a second delivery of the same task cannot re-run or re-bill it —
    # nor bin the clips of the run that legitimately finished.
    worker.process_job(job_id)
    assert db.get_job(job_id)["status"] == "done"
    assert db.get_usage(user_id, month) == 1
    assert _download(client, job_id, token) == 200


def _fast_pipeline(monkeypatch: pytest.MonkeyPatch, worker) -> None:  # noqa: ANN001
    """Stub the ffmpeg/ASR stages so `process_job` runs in milliseconds.

    Everything worker.py itself owns still runs for real — the claim, the stage
    writes, the render loop, the terminal transition, the settlement — so the
    race below contends on exactly the statements production contends on. Only
    the media work in front of them is replaced, which is what makes it
    affordable to repeat the race enough times for the result to mean anything.

    The small sleep inside the render is the point of the exercise: it holds the
    worker inside its claim long enough for the sweep to reach the same row.
    """
    from clipcatalyst_api.pipeline.types import (
        AudioFeatures,
        ClipPlan,
        MediaInfo,
        RenderedClip,
        Transcript,
        Word,
    )

    words = [Word(text=" word", start=float(i), end=float(i) + 0.4) for i in range(10)]
    plans = [
        ClipPlan(
            id="clip-1", start=0.0, end=5.0, score=80, title="A clip",
            hooks=[], reason="because", tip="post it", words=words,
        )
    ]
    transcriber = SimpleNamespace(
        transcribe=lambda src, on_progress=None: Transcript(words=words, text="word")
    )

    def fake_render(src, plan, out_path, opts, settings, on_progress=None, track=None):
        time.sleep(0.02)
        Path(out_path).write_bytes(b"rendered mp4 bytes")
        return RenderedClip(plan=plan, path=str(out_path), width=720, height=1280)

    monkeypatch.setattr(worker, "probe_media", lambda src, s: MediaInfo(30.0, 640, 360, True))
    monkeypatch.setattr(
        worker, "extract_audio_features", lambda src, s: AudioFeatures([1.0], 0.1, [])
    )
    monkeypatch.setattr(worker, "get_transcriber", lambda s: transcriber)
    monkeypatch.setattr(worker, "diarization_enabled", lambda s: False)
    monkeypatch.setattr(worker, "plan_clips", lambda t, f, o: list(plans))
    monkeypatch.setattr(worker, "detect_faces", lambda *a, **k: [])
    monkeypatch.setattr(worker, "render_clip", fake_render)


def test_the_sweep_and_the_worker_never_both_settle_the_same_job(
    quiet_sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unsynchronised version of the two tests above, on real threads.

    Both sides used to settle blind, so the outcome depended on write order:
    the sweep could refund a job the worker was about to mark `done`, and the
    worker could mark `done` a job the sweep had just refunded. Either way a
    clip was delivered and nobody paid for it.

    The status guard and the settlement are now one transaction, so whichever
    side wins the row owns the whole outcome and the loser knows it lost. The
    invariant asserted here is the money one, per round and in total: clips a
    customer can download == clips billed.
    """
    import threading

    from clipcatalyst_api import db, worker

    rounds = 16
    client = quiet_sandbox.client
    body = _register(client, "threadrace@example.com")
    token, user_id = body["token"], body["user"]["id"]
    # Unlimited, so no round is ever refused and the ONLY thing moving the
    # counter is settlement — which is what is under test.
    db.update_user(user_id, plan="enterprise", plan_status="active")
    month = _month()

    _fast_pipeline(monkeypatch, worker)
    outcomes: list[str] = []
    delivered = 0

    for index in range(rounds):
        job_id = _ready_job(client, token, count=1)
        assert _start(client, job_id, token).status_code == 202
        reserved_total = db.get_usage(user_id, month)
        # The row is already past the stall window when the worker picks it up,
        # so the sweep and the worker are both entitled to it at the same instant.
        _stall(quiet_sandbox, job_id)

        gate = threading.Barrier(2)
        errors: list[BaseException] = []

        def run(fn) -> None:  # noqa: ANN001
            gate.wait()
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the parent
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(lambda: worker.process_job(job_id),)),
            threading.Thread(target=run, args=(worker.reconcile_stalled,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "a racing thread deadlocked"
        assert not errors, f"round {index}: {errors!r}"

        job = db.get_job(job_id)
        usage = db.get_usage(user_id, month)
        served = _download(client, job_id, token) == 200
        outcomes.append(job["status"])

        assert job["usage_reserved"] == 0, "every round must end settled"
        assert job["status"] in ("done", "failed"), job["status"]
        if job["status"] == "done":
            # The worker held the row: the clip is real, and it is billed.
            assert served and len(job["clips"]) == 1
            assert usage == reserved_total, "a delivered clip was refunded anyway"
            delivered += 1
        else:
            # The sweep held the row: full refund, and nothing was handed over.
            assert not served, "a refunded job served its clip"
            assert job["clips"] == []
            assert usage == reserved_total - 1, "the reservation did not go back"

    assert len(outcomes) == rounds
    # Both sides really do win rounds here — the interleaving is left to the
    # scheduler on purpose, so the assertion below is the invariant, not a
    # prediction about who wins.
    assert delivered == db.get_usage(user_id, month), (
        f"delivered={delivered} billed={db.get_usage(user_id, month)} {outcomes}"
    )


def test_a_status_write_must_always_name_the_status_it_replaces(
    sandbox: SimpleNamespace,
) -> None:
    """The structural half of the fix. Every defect in this section began with
    an unguarded `update_job(status=...)` overwriting a row somebody else had
    already moved, so `status` is no longer an updatable column at all: it moves
    only through transition_status / finalize_job, which state what they expect
    to replace and report whether they actually won."""
    from clipcatalyst_api import db

    client = sandbox.client
    job_id = _ready_job(client, None)

    with pytest.raises(ValueError, match="status"):
        db.update_job(job_id, status="done")
    assert db.get_job(job_id)["status"] == "awaiting_upload", "the write must not land"
    # The other bookkeeping columns are untouched by the tightening.
    db.update_job(job_id, detail="still fine")
    assert db.get_job(job_id)["detail"] == "still fine"

    # A losing transition changes nothing and says so; the winner says so too.
    assert db.transition_status(job_id, expect="processing", to="done") is False
    assert db.get_job(job_id)["status"] == "awaiting_upload"
    assert db.transition_status(job_id, expect="awaiting_upload", to="queued") is True
    assert db.get_job(job_id)["status"] == "queued"

    # finalize_job carries the same guard, and only the winner settles.
    assert db.finalize_job(job_id, expect="processing", to="done", rendered=1) is False
    assert db.get_job(job_id)["status"] == "queued"
    assert db.finalize_job(job_id, expect=db.LIVE_STATUSES, to="failed", rendered=0) is True
    assert db.get_job(job_id)["status"] == "failed"
    # Terminal is terminal: nothing moves the row out of it, from either side.
    assert db.finalize_job(job_id, expect=db.LIVE_STATUSES, to="done", rendered=1) is False
    assert db.transition_status(job_id, expect="failed", to="queued") is True  # only an
    assert db.transition_status(job_id, expect="queued", to="failed") is True  # explicit
    assert db.get_job(job_id)["status"] == "failed"                            # ask does


# --------------------------------------------------------------------------- #
# 14. Round four's own fallout, from the skeptic that cleared the rest. Both
#     defects are in file CLEANUP rather than money or access: a duplicate
#     delivery deleted the source belonging to the run that owns the job, and
#     the reaper's ordering could strand a file with no row left to reap it.
# --------------------------------------------------------------------------- #


def _source_file(sandbox: SimpleNamespace, job_id: str) -> Path:
    """Where LocalStorage puts the uploaded source — what ffmpeg reads."""
    return sandbox.data_dir / "uploads" / f"{job_id}.src"


def test_a_duplicate_delivery_does_not_delete_the_owners_source(
    quiet_sandbox: SimpleNamespace
) -> None:
    """[N-1] Celery delivers at least once, so a worker restart or an expired
    visibility timeout starts a SECOND run of a job another worker is already
    rendering. That duplicate loses the claim and stops — but its `finally`
    still removed the uploaded source, mid-ffmpeg, out from under the run that
    owns the job. The owner's render then died on a missing input and the user
    was told to re-export a file that was never the problem:

        clip 1/1 failed: Rendering failed (ffmpeg exit 254).
        Error opening input file .../uploads/<job>.src

    Nothing here is an attack — it is ordinary at-least-once delivery. A run
    that owns nothing must clean up nothing.
    """
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    token = _register(client, "duplicate@example.com")["token"]
    job_id = _ready_job(client, token, count=1)
    assert _start(client, job_id, token).status_code == 202

    source = _source_file(quiet_sandbox, job_id)
    assert source.exists(), "precondition: the upload is on disk"

    # The owning worker claims the row and is now inside its render.
    assert db.transition_status(job_id, expect="queued", to="processing") is True

    # The duplicate pops the same job and loses the claim.
    worker.process_job(job_id)

    assert source.exists(), "the duplicate deleted the owner's source mid-render"
    assert db.get_job(job_id)["status"] == "processing", "it also wrote a terminal status"


def test_the_reaper_drops_the_row_before_the_files_it_authorizes(
    quiet_sandbox: SimpleNamespace
) -> None:
    """[N-2] The reaper settled, removed the clips directory, and only then
    deleted the row. A worker finishing inside that window still won its
    processing→done guard against a row that was about to disappear, wrote its
    clips after the rmtree, and left files on disk with no row left to reap
    them — an orphan that nothing would ever collect.

    Dropping the row first closes the window from both ends: a worker that
    publishes afterwards has already lost its claim (there is no row to
    finalize against), and the rmtree that follows sweeps whatever it left.

    The publish is simulated at the exact instant the row goes, which is the
    moment that used to strand the file.
    """
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    token = _register(client, "orphan@example.com")["token"]
    job_id = _ready_job(client, token, count=1)
    assert _start(client, job_id, token).status_code == 202
    _age(quiet_sandbox, job_id, created_at=ANCIENT)

    clip = _clip_file(quiet_sandbox, job_id)
    real_delete = db.delete_job
    published: list[Path] = []

    def delete_and_publish(target_id: str) -> None:
        # A worker crossing the finish line exactly as the reaper takes the row.
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"rendered mp4 bytes")
        published.append(clip)
        real_delete(target_id)

    original = worker.db.delete_job
    worker.db.delete_job = delete_and_publish
    try:
        assert worker.reap_expired() == 1
    finally:
        worker.db.delete_job = original

    assert published, "precondition: the simulated worker published a clip"
    assert db.get_job(job_id) is None, "the row is gone"
    assert not clip.exists(), "a clip file outlived the row that authorizes it"
    assert not clip.parent.exists(), "the job's clips directory outlived its row"
