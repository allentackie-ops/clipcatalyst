"""Full in-process integration test of the ClipCatalyst Cloud API.

Runs the REAL pipeline end to end: create job → PUT upload → start (Celery in
eager mode executes probe/transcribe/analyze/render inline, with real ffmpeg
renders) → poll → download a rendered clip and probe it as portrait H.264 with
audio. Only transcription is faked (CC_TRANSCRIBER=fake) via a generated
transcript JSON, because whisper models are not available in the sandbox.

All CC_* env vars are set BEFORE any app module is imported; get_settings()
is lru_cached, so the cache is cleared and settings-snapshotting modules
(queue_app / worker / main) are purged for a clean re-import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

VIDEO_SECONDS = 40
MAX_UPLOAD_BYTES = 20_000_000

# Hooky sentences the highlights engine scores well; timed inside the video.
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


@pytest.fixture(scope="session")
def test_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_video(tmp_path_factory.mktemp("media") / "source.mp4")


@pytest.fixture(scope="session")
def app_env(tmp_path_factory: pytest.TempPathFactory, test_video: Path) -> dict:
    """Set CC_* env BEFORE app imports; reset caches and snapshot modules."""
    transcript = _make_transcript(tmp_path_factory.mktemp("fixtures") / "transcript.json")
    data_dir = tmp_path_factory.mktemp("data")
    env = {
        "CC_QUEUE": "eager",
        "CC_TRANSCRIBER": "fake",
        "CC_STORAGE": "local",
        "CC_DATA_DIR": str(data_dir),
        "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
        "CC_FAKE_TRANSCRIPT_PATH": str(transcript),
        "CC_PUBLIC_BASE_URL": "",
        "CC_MAX_UPLOAD_BYTES": str(MAX_UPLOAD_BYTES),
    }
    os.environ.update(env)

    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    # queue_app/worker/main snapshot settings at import time — force re-import.
    for name in (
        "clipcatalyst_api.main",
        "clipcatalyst_api.worker",
        "clipcatalyst_api.queue_app",
    ):
        sys.modules.pop(name, None)
    return env


@pytest.fixture(scope="session")
def client(app_env: dict) -> Iterator["TestClient"]:  # noqa: F821
    from fastapi.testclient import TestClient

    from clipcatalyst_api.main import app

    with TestClient(app) as c:
        yield c


def _create_job(client, test_video: Path, **overrides) -> dict:
    payload = {
        "filename": "source.mp4",
        "size_bytes": test_video.stat().st_size,
        "target_length": 15,
        "count": 2,
        "height": 960,
    }
    payload.update(overrides)
    resp = client.post("/v1/jobs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_healthz(client) -> None:
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["queue"] == "eager"
    assert body["storage"] == "local"
    assert body["transcriber"] == "fake"
    assert body["version"]


def test_full_flow_renders_real_portrait_clips(client, test_video: Path, tmp_path: Path) -> None:
    created = _create_job(client, test_video)
    job_id = created["job_id"]
    assert created["upload"]["mode"] == "put"
    assert created["upload"]["url"] == f"/v1/uploads/{job_id}"

    # Job starts life awaiting its upload.
    status = client.get(f"/v1/jobs/{job_id}").json()
    assert status["status"] == "awaiting_upload"

    # PUT the raw bytes.
    resp = client.put(created["upload"]["url"], content=test_video.read_bytes())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    # Start: eager queue runs the WHOLE pipeline inline, real ffmpeg renders.
    resp = client.post(f"/v1/jobs/{job_id}/start")
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"job_id": job_id, "status": "queued"}

    body = client.get(f"/v1/jobs/{job_id}").json()
    assert body["status"] == "done", f"pipeline failed: {body.get('error')!r} ({body})"
    assert body["error"] is None
    assert body["stage"] == "render"
    assert body["progress"] == 1.0
    assert body["detail"]

    clips = body["clips"]
    assert len(clips) >= 1
    for clip in clips:
        assert clip["id"]
        assert isinstance(clip["index"], int) and clip["index"] >= 0
        assert 0 <= clip["score"] <= 100
        assert clip["title"].strip()
        assert isinstance(clip["hooks"], list) and clip["hooks"]
        assert clip["reason"].strip()
        assert clip["tip"].strip()
        assert 0 <= clip["start"] < clip["end"] <= VIDEO_SECONDS + 1
        assert clip["duration"] == pytest.approx(clip["end"] - clip["start"], abs=0.01)
        assert clip["url"].startswith(f"/v1/files/{job_id}/")
        assert clip["width"] == 540  # 9:16 of the requested 960 height
        assert clip["height"] == 960

    # Download the first clip through the files route.
    resp = client.get(clips[0]["url"])
    assert resp.status_code == 200
    data = resp.content
    assert len(data) > 10_000, "rendered clip should be non-trivial mp4 bytes"

    # Probe the downloaded mp4: portrait dimensions + an audio track.
    downloaded = tmp_path / "clip.mp4"
    downloaded.write_bytes(data)
    from clipcatalyst_api.pipeline.probe import probe_media
    from clipcatalyst_api.settings import get_settings

    info = probe_media(downloaded, get_settings())
    assert info.width == 540
    assert info.height == 960
    assert info.height > info.width, "clip must be portrait"
    assert info.has_audio
    assert info.duration > 5


def test_get_unknown_job_returns_404(client) -> None:
    assert client.get("/v1/jobs/does-not-exist").status_code == 404


def test_upload_for_missing_job_returns_404(client) -> None:
    resp = client.put("/v1/uploads/does-not-exist", content=b"video bytes")
    assert resp.status_code == 404


def test_start_without_upload_returns_409(client, test_video: Path) -> None:
    job_id = _create_job(client, test_video)["job_id"]
    resp = client.post(f"/v1/jobs/{job_id}/start")
    assert resp.status_code == 409
    # And the job is untouched, still awaiting its upload.
    assert client.get(f"/v1/jobs/{job_id}").json()["status"] == "awaiting_upload"


def test_upload_without_content_length_returns_411(client, test_video: Path) -> None:
    job_id = _create_job(client, test_video)["job_id"]
    # A bytes generator makes httpx send chunked with no Content-Length.
    resp = client.put(f"/v1/uploads/{job_id}", content=iter([b"chunk"]))
    assert resp.status_code == 411


def test_upload_over_limit_returns_413(client, test_video: Path) -> None:
    job_id = _create_job(client, test_video)["job_id"]
    resp = client.put(f"/v1/uploads/{job_id}", content=b"x" * (MAX_UPLOAD_BYTES + 1))
    assert resp.status_code == 413


def test_create_job_rejects_invalid_options(client, test_video: Path) -> None:
    size = test_video.stat().st_size
    base = {"filename": "a.mp4", "size_bytes": size}
    for bad in (
        {"target_length": 20},
        {"count": 0},
        {"count": 4},
        {"height": 720},
    ):
        resp = client.post("/v1/jobs", json={**base, **bad})
        assert resp.status_code == 422, f"{bad} should be rejected: {resp.text}"


def test_files_route_rejects_traversal_names(client) -> None:
    resp = client.get("/v1/files/somejob/..%2F..%2Fjobs.sqlite3")
    assert resp.status_code == 404
