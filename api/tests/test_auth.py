"""Accounts auth tests: register/login/logout/me, sessions, IDOR, rate limits.

Mirrors the env/import dance of ``test_security.py`` — all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules (queue_app / worker / main,
plus auth for its in-process rate-limit windows) are purged for a clean
re-import. The whole os.environ is snapshotted and restored around each client.

The IDOR test runs the real pipeline in eager mode (``CC_QUEUE=eager``) with a
faked transcriber and a tiny generated test video so the founder-token
counter-check can download an actually rendered clip.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

VIDEO_SECONDS = 40
MAX_UPLOAD_BYTES = 20_000_000
FOUNDER_TOKEN = "s3cr3t-founder-token"

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
    "clipcatalyst_api.auth",
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


def _set_token(value: str | None) -> None:
    """Flip CC_API_TOKEN at runtime; the actor deps read get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    if value is None:
        os.environ.pop("CC_API_TOKEN", None)
    else:
        os.environ["CC_API_TOKEN"] = value
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def idor_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_video(tmp_path_factory.mktemp("authmedia") / "source.mp4")


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; founder token starts OFF."""
    saved_env = dict(os.environ)
    transcript = _make_transcript(tmp_path_factory.mktemp("authfix") / "transcript.json")
    data_dir = tmp_path_factory.mktemp("authdata")

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
        }
    )
    os.environ.pop("CC_API_TOKEN", None)  # open by default
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api.main import app

    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, data_dir=data_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str, password: str = "correct-horse-battery") -> dict:
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


# --------------------------------------------------------------------------- #
# 1. Password hashing: scrypt format, verify, salting, malformed inputs.
# --------------------------------------------------------------------------- #


def test_scrypt_hash_format_and_verify() -> None:
    from clipcatalyst_api import auth

    stored = auth.hash_password("correct horse battery staple")
    algorithm, n, r, p, salt_b64, hash_b64 = stored.split("$")
    assert algorithm == "scrypt"
    assert (n, r, p) == ("16384", "8", "1")
    assert len(base64.b64decode(salt_b64)) == 32
    assert len(base64.b64decode(hash_b64)) == 32

    assert auth.verify_password("correct horse battery staple", stored)
    assert not auth.verify_password("correct horse battery stapl", stored)
    # Fresh salt every time: same password, different stored string.
    assert stored != auth.hash_password("correct horse battery staple")
    # Malformed stored values are False, never an exception.
    assert not auth.verify_password("anything", "")
    assert not auth.verify_password("anything", "not-a-hash")
    assert not auth.verify_password("anything", "bcrypt$1$2$3$AAAA$AAAA")
    assert not auth.verify_password("anything", "scrypt$16384$8$1$!!$!!")


# --------------------------------------------------------------------------- #
# 2. Register / login / logout / me round-trip.
# --------------------------------------------------------------------------- #


def test_register_login_logout_me_round_trip(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    body = _register(client, "  Creator@Example.COM ", "hunter2hunter2")
    token = body["token"]
    assert token.startswith("cc_sess_")
    assert body["user"]["email"] == "creator@example.com"  # stripped + lowercased
    assert body["user"]["plan"] == "free"
    assert body["user"]["plan_status"] == ""

    me = client.get("/v1/me", headers=_bearer(token))
    assert me.status_code == 200, me.text
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert me.json() == {
        "email": "creator@example.com",
        "plan": "free",
        "plan_status": "",
        # Registered with a password and nothing else; Sign in with Google
        # adds "google" to this list (LIBRARY.md Part 1, test_google_auth.py).
        "auth_methods": ["password"],
        "quota": {"limit": 3, "used": 0, "month": month},
        "entitlements": {
            "max_height": 1280,
            "watermark_required": True,
            "clips_per_month": 3,
            # Free carries no brand kit — its own promise, not the inverse of
            # the watermark (BRANDKIT.md §3a; test_brandkit.py owns the rest).
            "brand_kit": False,
            # How long a saved clip's FILE is kept (LIBRARY.md Part 2). The
            # account page says it out loud from here, so the line on screen is
            # the window the reaper enforces; test_library.py owns the rest.
            "retention_days": 7,
        },
        # A fresh account has never opened the panel: no logo, no colour, and
        # showLogo on — the EMPTY_KIT the browser starts from.
        "brand": {"logo_url": None, "caption_color": None, "show_logo": True},
    }

    # A fresh login issues a distinct token; both sessions are live at once.
    login = client.post(
        "/v1/auth/login",
        json={"email": "Creator@example.com", "password": "hunter2hunter2"},
    )
    assert login.status_code == 200, login.text
    token2 = login.json()["token"]
    assert token2 != token
    assert client.get("/v1/me", headers=_bearer(token2)).status_code == 200

    # Logout revokes exactly the presented session; the other keeps working.
    assert client.post("/v1/auth/logout", headers=_bearer(token)).status_code == 200
    assert client.get("/v1/me", headers=_bearer(token)).status_code == 401
    assert client.get("/v1/me", headers=_bearer(token2)).status_code == 200


def test_duplicate_email_returns_409(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    _register(client, "taken@example.com")
    # Same email, different case/whitespace — still one account.
    resp = client.post(
        "/v1/auth/register",
        json={"email": " TAKEN@example.com", "password": "another-password"},
    )
    assert resp.status_code == 409


def test_register_validates_email_and_password(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    for bad_email in ("no-at-sign", "a@b", "sp ace@example.com", "@example.com"):
        resp = client.post(
            "/v1/auth/register", json={"email": bad_email, "password": "long-enough-pw"}
        )
        assert resp.status_code == 400, bad_email
    # Min password length 8 (pydantic 422).
    resp = client.post(
        "/v1/auth/register", json={"email": "short@example.com", "password": "seven77"}
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 3. Login failures: one generic 401, identical for bad email and bad password.
# --------------------------------------------------------------------------- #


def test_login_failure_bodies_are_identical(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    _register(client, "known@example.com", "the-real-password")

    wrong_password = client.post(
        "/v1/auth/login",
        json={"email": "known@example.com", "password": "not-the-password"},
    )
    unknown_email = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-password"},
    )
    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    # No user-exists oracle in the error path: byte-identical bodies.
    assert wrong_password.json() == unknown_email.json()


# --------------------------------------------------------------------------- #
# 4. Sessions: malformed, expired, and revoked bearers are all 401.
# --------------------------------------------------------------------------- #


def test_malformed_expired_and_revoked_sessions_are_401(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import auth, db

    client = sandbox.client
    body = _register(client, "sessions@example.com")
    token, user_id = body["token"], body["user"]["id"]

    # Malformed shapes: no header, wrong scheme, non-session bearer, unknown
    # session token, bare prefix.
    assert client.get("/v1/me").status_code == 401
    for headers in (
        {"Authorization": token},  # missing "Bearer "
        {"Authorization": "Token abc"},
        _bearer("not-a-session-token"),
        _bearer("cc_sess_" + "a" * 43),
        _bearer("cc_sess_"),
    ):
        assert client.get("/v1/me", headers=headers).status_code == 401, headers

    # Expired: a real session whose expiry is in the past.
    expired_raw = auth.new_session_token()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
        timespec="milliseconds"
    )
    db.create_session(
        auth.hash_session_token(expired_raw), user_id=user_id, expires_at=past
    )
    assert client.get("/v1/me", headers=_bearer(expired_raw)).status_code == 401

    # Revoked: logout kills the token; reusing it (even to log out again) is 401.
    assert client.post("/v1/auth/logout", headers=_bearer(token)).status_code == 200
    assert client.get("/v1/me", headers=_bearer(token)).status_code == 401
    assert client.post("/v1/auth/logout", headers=_bearer(token)).status_code == 401


# --------------------------------------------------------------------------- #
# 5. Secrets at rest: the DB file holds hashes, never the raw token/password.
# --------------------------------------------------------------------------- #


def test_secrets_are_stored_hashed_never_raw(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import auth, db

    client = sandbox.client
    password = "sw0rdfish-Sw0rdfish"
    token = _register(client, "hashcheck@example.com", password)["token"]

    # Fold the WAL into the main file so one read covers every page, then
    # scan the raw bytes of everything SQLite has on disk.
    with contextlib.closing(db._connect()) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = (sandbox.data_dir / "jobs.sqlite3").read_bytes()
    wal = sandbox.data_dir / "jobs.sqlite3-wal"
    if wal.exists():
        blob += wal.read_bytes()

    raw_secret = token[len("cc_sess_") :]
    assert token.encode() not in blob
    assert raw_secret.encode() not in blob
    assert password.encode() not in blob
    # What IS on disk: the sha256 of the token and a scrypt password string.
    assert auth.hash_session_token(token).encode() in blob
    assert b"scrypt$16384$8$1$" in blob
    # And the session still works after the checkpoint (sanity).
    assert client.get("/v1/me", headers=_bearer(token)).status_code == 200


# --------------------------------------------------------------------------- #
# 6. Rate limiting: fixed window per (ip, route), 10/min, 429 beyond.
# --------------------------------------------------------------------------- #


def test_rate_limit_returns_429_per_route_window(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipcatalyst_api import auth

    # Pin the window so a minute rollover mid-test can't deflake the count.
    monkeypatch.setattr(auth, "_current_minute", lambda: 12345)
    client = sandbox.client

    for i in range(auth.RATE_LIMIT_PER_MINUTE):
        resp = client.post(
            "/v1/auth/login",
            json={"email": f"nobody{i}@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401, resp.text
    resp = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 429

    # The register window is its own bucket — still open.
    _register(client, "fresh@example.com")

    # A new minute clears the login window.
    monkeypatch.setattr(auth, "_current_minute", lambda: 12346)
    resp = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# 7. Job ownership: IDOR on all four job routes, founder-token counter-check.
# --------------------------------------------------------------------------- #


def test_job_ownership_blocks_idor_on_all_routes(
    sandbox: SimpleNamespace, idor_video: Path
) -> None:
    client = sandbox.client
    token_a = _register(client, "owner-a@example.com")["token"]
    token_b = _register(client, "rival-b@example.com")["token"]

    # A creates a job with their session — the row is owned.
    created = client.post(
        "/v1/jobs",
        json=_job_payload(size_bytes=idor_video.stat().st_size),
        headers=_bearer(token_a),
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    # B (a valid session) and anonymous both see 404 on every route — the job
    # must be indistinguishable from one that does not exist.
    assert client.get(f"/v1/jobs/{job_id}", headers=_bearer(token_b)).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404
    assert (
        client.put(f"/v1/uploads/{job_id}", content=b"x", headers=_bearer(token_b)).status_code
        == 404
    )
    assert (
        client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token_b)).status_code == 404
    )
    assert (
        client.get(f"/v1/files/{job_id}/clip-01.mp4", headers=_bearer(token_b)).status_code
        == 404
    )

    # A drives their own job through the whole eager pipeline.
    assert (
        client.put(
            f"/v1/uploads/{job_id}",
            content=idor_video.read_bytes(),
            headers=_bearer(token_a),
        ).status_code
        == 200
    )
    assert client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token_a)).status_code == 202
    body = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token_a)).json()
    assert body["status"] == "done", f"pipeline failed: {body.get('error')!r}"
    clip_url = body["clips"][0]["url"]
    assert client.get(clip_url, headers=_bearer(token_a)).status_code == 200

    # B still 404s on the finished artifacts, and B's 404 hides even the
    # job's state: A's own duplicate calls say 409 (state), B's say 404.
    assert client.get(clip_url, headers=_bearer(token_b)).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}", headers=_bearer(token_b)).status_code == 404
    assert (
        client.put(f"/v1/uploads/{job_id}", content=b"x", headers=_bearer(token_a)).status_code
        == 409
    )
    assert client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token_a)).status_code == 409

    # Founder token passes everything (it's the owner's box), sessions keep
    # working alongside it, and B stays locked out.
    _set_token(FOUNDER_TOKEN)
    try:
        assert (
            client.get(f"/v1/jobs/{job_id}", headers=_bearer(FOUNDER_TOKEN)).status_code == 200
        )
        assert client.get(clip_url, headers=_bearer(FOUNDER_TOKEN)).status_code == 200
        assert client.get(f"/v1/jobs/{job_id}", headers=_bearer(token_a)).status_code == 200
        assert client.get(clip_url, headers=_bearer(token_b)).status_code == 404
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404  # anonymous
    finally:
        _set_token(None)


def test_anonymous_jobs_stay_open_as_before(sandbox: SimpleNamespace) -> None:
    """Regression: dev/founder jobs carry no owner and behave exactly as today."""
    client = sandbox.client
    created = client.post("/v1/jobs", json=_job_payload())  # open mode, no session
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    token = _register(client, "bystander@example.com")["token"]
    # Anyone — anonymous or signed in — can still poll an unowned job.
    assert client.get(f"/v1/jobs/{job_id}").status_code == 200
    assert client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).status_code == 200


# --------------------------------------------------------------------------- #
# 8. /v1/me: usage comes from the usage table; entitlements follow the
#    EFFECTIVE plan (canceled → free).
# --------------------------------------------------------------------------- #


def test_usage_accessors_roll_up_per_month(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    db.add_usage("u1", "2026-08", 2)
    db.add_usage("u1", "2026-08", 1)
    db.add_usage("u1", "2026-09", 5)
    assert db.get_usage("u1", "2026-08") == 3
    assert db.get_usage("u1", "2026-09") == 5
    assert db.get_usage("u1", "2026-10") == 0
    assert db.get_usage("someone-else", "2026-08") == 0


def test_me_reports_usage_and_effective_plan(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "plans@example.com")
    token, user_id = body["token"], body["user"]["id"]

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    db.add_usage(user_id, month, 2)
    db.update_user(user_id, plan="starter", plan_status="active")

    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == "starter"
    assert me["quota"] == {"limit": 30, "used": 2, "month": month}
    assert me["entitlements"] == {
        "max_height": 1920,
        "watermark_required": False,
        "clips_per_month": 30,
        "brand_kit": True,  # promised from Starter up (BRANDKIT.md)
        "retention_days": 30,  # Starter keeps clips a month (LIBRARY.md)
    }

    # Canceled keeps the stored plan name but entitles as free.
    db.update_user(user_id, plan_status="canceled")
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == "starter"
    assert me["plan_status"] == "canceled"
    assert me["quota"]["limit"] == 3
    assert me["entitlements"]["watermark_required"] is True

    # The whitelist guards the account's identity fields.
    with pytest.raises(ValueError):
        db.update_user(user_id, email="new@example.com")
