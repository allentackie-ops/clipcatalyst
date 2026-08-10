"""Security hardening tests: bearer-token auth, path-traversal, atomic start.

Mirrors the env/import dance of ``test_api_flow.py`` — all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules (queue_app / worker / main)
are purged for a clean re-import. The whole os.environ is snapshotted and
restored around each client so token/data-dir flips never leak into other test
modules (their session-scoped app reads ``get_settings()`` live per request).

Runs the real pipeline in eager mode (``CC_QUEUE=eager``) with a faked
transcriber (``CC_TRANSCRIBER=fake``) and a tiny generated test video.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

VIDEO_SECONDS = 40
MAX_UPLOAD_BYTES = 20_000_000
TOKEN = "s3cr3t-deploy-token"

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


def _set_token(value: str | None) -> None:
    """Flip CC_API_TOKEN at runtime; require_token reads get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    if value is None:
        os.environ.pop("CC_API_TOKEN", None)
    else:
        os.environ["CC_API_TOKEN"] = value
    get_settings.cache_clear()


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; auth starts OFF (open)."""
    saved_env = dict(os.environ)
    video = _make_video(tmp_path_factory.mktemp("secmedia") / "source.mp4")
    transcript = _make_transcript(tmp_path_factory.mktemp("secfix") / "transcript.json")
    data_dir = tmp_path_factory.mktemp("secdata")

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
            yield SimpleNamespace(client=client, video=video, data_dir=data_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _job_payload(video: Path, **overrides) -> dict:
    payload = {
        "filename": "source.mp4",
        "size_bytes": video.stat().st_size,
        "target_length": 15,
        "count": 2,
        "height": 960,
    }
    payload.update(overrides)
    return payload


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1. Auth: open mode still works; token mode gates the mutating routes.
# --------------------------------------------------------------------------- #


def test_open_mode_allows_writes_without_token(sandbox: SimpleNamespace) -> None:
    client, video = sandbox.client, sandbox.video
    # No token configured → every route behaves exactly as before (no auth).
    created = client.post("/v1/jobs", json=_job_payload(video))
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]

    assert client.put(f"/v1/uploads/{job_id}", content=video.read_bytes()).status_code == 200
    assert client.post(f"/v1/jobs/{job_id}/start").status_code == 202

    body = client.get(f"/v1/jobs/{job_id}").json()
    assert body["status"] == "done", f"pipeline failed: {body.get('error')!r}"
    # Files download with no Authorization header.
    assert client.get(body["clips"][0]["url"]).status_code == 200


def test_token_mode_rejects_missing_and_wrong_bearer(sandbox: SimpleNamespace) -> None:
    client, video = sandbox.client, sandbox.video
    _set_token(TOKEN)
    try:
        payload = _job_payload(video)

        # Missing header → 401.
        assert client.post("/v1/jobs", json=payload).status_code == 401
        # Wrong scheme / wrong token → 401.
        assert client.post(
            "/v1/jobs", json=payload, headers={"Authorization": TOKEN}
        ).status_code == 401
        assert client.post(
            "/v1/jobs", json=payload, headers=_bearer("not-the-token")
        ).status_code == 401
        assert client.post(
            "/v1/jobs", json=payload, headers=_bearer(TOKEN + "x")
        ).status_code == 401

        # Correct bearer → accepted, and the full write path is gated the same.
        created = client.post("/v1/jobs", json=payload, headers=_bearer(TOKEN))
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]

        # Upload/start/file each reject without a token and accept with one.
        assert client.put(f"/v1/uploads/{job_id}", content=video.read_bytes()).status_code == 401
        assert client.put(
            f"/v1/uploads/{job_id}", content=video.read_bytes(), headers=_bearer(TOKEN)
        ).status_code == 200

        assert client.post(f"/v1/jobs/{job_id}/start").status_code == 401
        assert client.post(
            f"/v1/jobs/{job_id}/start", headers=_bearer(TOKEN)
        ).status_code == 202

        body = client.get(f"/v1/jobs/{job_id}").json()
        assert body["status"] == "done", f"pipeline failed: {body.get('error')!r}"
        url = body["clips"][0]["url"]
        assert client.get(url).status_code == 401
        assert client.get(url, headers=_bearer(TOKEN)).status_code == 200
    finally:
        _set_token(None)


def test_status_and_healthz_stay_open_under_token(sandbox: SimpleNamespace) -> None:
    client, video = sandbox.client, sandbox.video
    _set_token(TOKEN)
    try:
        created = client.post("/v1/jobs", json=_job_payload(video), headers=_bearer(TOKEN))
        job_id = created.json()["job_id"]
        # Status polling and health are read-only and must not require a token.
        assert client.get(f"/v1/jobs/{job_id}").status_code == 200
        assert client.get("/v1/healthz").status_code == 200
    finally:
        _set_token(None)


# --------------------------------------------------------------------------- #
# 2. Path traversal on GET /v1/files must 404 and never leak bytes.
# --------------------------------------------------------------------------- #


def _sqlite_leaked(content: bytes) -> bool:
    return b"SQLite format 3" in content


def test_files_route_blocks_traversal_and_leaks_nothing(sandbox: SimpleNamespace) -> None:
    client, video = sandbox.client, sandbox.video
    # Create a real job so the SQLite DB exists on disk next to clips_dir.
    created = client.post("/v1/jobs", json=_job_payload(video))
    valid_job = created.json()["job_id"]
    assert len(valid_job) == 32 and all(c in "0123456789abcdef" for c in valid_job)

    db_path = sandbox.data_dir / "jobs.sqlite3"
    assert db_path.is_file(), "the DB must exist for the leak assertion to be meaningful"

    # The %2E%2E segment form (job_id decodes to "..") previously leaked the DB.
    r1 = client.get("/v1/files/%2E%2E/jobs.sqlite3")
    assert r1.status_code == 404
    assert not _sqlite_leaked(r1.content)

    # Encoded-slash traversal inside the name of a well-formed job_id.
    r2 = client.get(f"/v1/files/{valid_job}/..%2F..%2Fjobs.sqlite3")
    assert r2.status_code == 404
    assert not _sqlite_leaked(r2.content)

    # A couple more shapes: bare "..", non-hex job_id, decoded traversal.
    for url in (
        "/v1/files/somejob/..%2F..%2Fjobs.sqlite3",
        f"/v1/files/{valid_job}/..",
        f"/v1/files/{'g' * 32}/clip-01.mp4",
    ):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert not _sqlite_leaked(resp.content), url


# --------------------------------------------------------------------------- #
# 3. Atomic start: a duplicate /start is a 409, never a second enqueue.
# --------------------------------------------------------------------------- #


def test_double_start_yields_one_202_and_one_409(sandbox: SimpleNamespace) -> None:
    client, video = sandbox.client, sandbox.video
    created = client.post("/v1/jobs", json=_job_payload(video))
    job_id = created.json()["job_id"]
    assert client.put(f"/v1/uploads/{job_id}", content=video.read_bytes()).status_code == 200

    # Eager mode runs the whole pipeline inline on the first (winning) start.
    codes = sorted(
        client.post(f"/v1/jobs/{job_id}/start").status_code for _ in range(2)
    )
    assert codes == [202, 409]

    body = client.get(f"/v1/jobs/{job_id}").json()
    assert body["status"] == "done", f"pipeline failed: {body.get('error')!r}"
    assert body["error"] is None
