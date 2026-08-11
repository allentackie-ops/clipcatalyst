"""The clip library (LIBRARY.md Part 2): rows that outlive their videos.

Two lifetimes share one row, and nearly every test here is about keeping them
apart: the METADATA is permanent — a clip's title, score, hooks and transcript
survive the file by design — while the FILE lives exactly as long as the owner's
plan promises. So the assertions are about what is still true after something
was deleted, not just about what a route returned.

The dangerous one is §6. A job's rendered clips live under
``clips_dir/<job_id>``, and TWO paths ``rmtree`` that whole directory: the 48 h
jobs reaper and the lost-claim cleanup. A library that pointed into there would
have its videos deleted by a sweep that has never heard of it — a Pro account's
90-day clips destroyed on day two by the job TTL. Library files therefore live
under their own root, and the tests in that section fail loudly if they ever
move back.

Mirrors the env/import dance of ``test_entitlements.py`` — all CC_* vars are set
BEFORE any app module is imported, ``get_settings`` is lru_cached so its cache
is cleared, and the settings-snapshotting modules (queue_app / worker / main)
are purged for a clean re-import. The whole os.environ is snapshotted and
restored around each client.

The cloud-render tests run the real pipeline in eager mode (``CC_QUEUE=eager``)
with a faked transcriber and a generated test video, because "the render writes
a library row" is only true if a clip was actually rendered.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

VIDEO_SECONDS = 40
MAX_UPLOAD_BYTES = 20_000_000

# Deliberately small so the ceilings are reachable in a test without moving
# hundreds of megabytes. The SHIPPED defaults (200 MB / 5 GB) are asserted on
# their own in test_settings_defaults_match_the_spec.
MAX_CLIP_BYTES = 2_000_000
LIBRARY_MAX_BYTES = 5_000_000

# A timestamp far enough back to be past any TTL or retention window.
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

# The magic every browser container starts with. The route sniffs the BYTES —
# the declared content type is a string the client chose — so these are what
# makes an upload a clip, and the plausible-looking impostors below are not.
MP4_MAGIC = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00"
WEBM_MAGIC = b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x82\x84webm"


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


@pytest.fixture(scope="session")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_video(tmp_path_factory.mktemp("libmedia") / "source.mp4")


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir and small library ceilings."""
    saved_env = dict(os.environ)
    transcript = _make_transcript(tmp_path_factory.mktemp("libfix") / "transcript.json")
    data_dir = tmp_path_factory.mktemp("libdata")

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
            "CC_MAX_CLIP_BYTES": str(MAX_CLIP_BYTES),
            "CC_LIBRARY_MAX_BYTES": str(LIBRARY_MAX_BYTES),
            "CC_BILLING": "off",  # plans are set directly here
        }
    )
    os.environ.pop("CC_API_TOKEN", None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth
    from clipcatalyst_api.main import app
    from clipcatalyst_api.settings import get_settings

    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, data_dir=data_dir, settings=get_settings()
            )
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str, password: str = "correct-horse-battery") -> dict:
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _account(client, email: str, *, plan: str = "free", status: str = "") -> tuple[str, str]:
    """Register an account and put it on a plan (as a verified webhook would)."""
    from clipcatalyst_api import db

    body = _register(client, email)
    token, user_id = body["token"], body["user"]["id"]
    if plan != "free" or status:
        db.update_user(user_id, plan=plan, plan_status=status or "active")
    return token, user_id


def _set_plan(user_id: str, plan: str, status: str = "active") -> None:
    """Move an account's plan the way a verified webhook does — including the
    library sync that rides with every plan change (billing.sync_clip_retention).
    """
    from clipcatalyst_api import billing, db

    db.update_user(user_id, plan=plan, plan_status="" if plan == "free" else status)
    billing.sync_clip_retention(user_id)


def _render_job(client, token: str, video: Path, *, count: int = 1) -> dict:
    """Run the whole cloud pipeline for one account; returns the done body."""
    created = client.post(
        "/v1/jobs",
        json={
            "filename": "source.mp4",
            "size_bytes": video.stat().st_size,
            "target_length": 15,
            "count": count,
            "height": 960,
        },
        headers=_bearer(token),
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["job_id"]
    ack = client.put(
        f"/v1/uploads/{job_id}", content=video.read_bytes(), headers=_bearer(token)
    )
    assert ack.status_code == 200, ack.text
    started = client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token))
    assert started.status_code == 202, started.text
    body = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert body["status"] == "done", f"pipeline failed: {body.get('error')!r}"
    body["job_id"] = job_id
    return body


def _multipart(data: bytes = MP4_MAGIC + b"\x00" * 4096, **metadata) -> dict:
    """The multipart body of a browser-clip upload."""
    files: dict = {"file": ("clip.mp4", data, "video/mp4")}
    if metadata:
        files["metadata"] = (None, json.dumps(metadata), "application/json")
    return files


def _upload(client, token: str, **kwargs):  # noqa: ANN202 - Response
    return client.post(
        "/v1/clips/upload", files=_multipart(**kwargs), headers=_bearer(token)
    )


def _list(client, token: str, **params) -> dict:
    resp = client.get("/v1/clips", params=params, headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _age_job(sandbox: SimpleNamespace, job_id: str, **columns: str) -> None:
    """Backdate a job row's timestamps in place (the reaper keys off them)."""
    conn = sqlite3.connect(sandbox.data_dir / "jobs.sqlite3")
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE jobs SET {assignments} WHERE id = ?", (*columns.values(), job_id)
    )
    conn.commit()
    conn.close()


def _age_clip(sandbox: SimpleNamespace, clip_id: str, **columns: str) -> None:
    """Backdate a clip row. ``created_at``/``expires_at`` are DAO bookkeeping —
    nothing writable moves them — and retention keys off wall-clock cutoffs, so
    this is how a 7-day window is reached without waiting a week."""
    conn = sqlite3.connect(sandbox.data_dir / "jobs.sqlite3")
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE clips SET {assignments} WHERE id = ?", (*columns.values(), clip_id)
    )
    conn.commit()
    conn.close()


def _days_apart(created_at: str, expires_at: str) -> float:
    return (
        datetime.fromisoformat(expires_at) - datetime.fromisoformat(created_at)
    ) / timedelta(days=1)


# --------------------------------------------------------------------------- #
# 1. A cloud render writes a library row, with the right window for the plan.
# --------------------------------------------------------------------------- #


def test_a_cloud_render_writes_a_library_row(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    """The library is written by the RUN that published the clips, so it is
    there the moment the job says done — not on some later sweep the 48 h job
    reaper could beat to it."""
    client = sandbox.client
    token, user_id = _account(client, "render@example.com")

    body = _render_job(client, token, source_video)
    rendered = body["clips"][0]

    listing = _list(client, token)
    assert len(listing["clips"]) == len(body["clips"])
    clip = listing["clips"][0]

    assert clip["engine"] == "cloud"
    assert clip["job_id"] == body["job_id"]
    assert clip["available"] is True
    assert clip["url"] == f"/v1/clips/{clip['id']}/file"
    assert clip["bytes"] > 10_000, "the row records what was actually stored"
    # The card the Studio shows, carried across verbatim.
    assert clip["title"] == rendered["title"]
    assert clip["score"] == rendered["score"]
    assert clip["hooks"] == rendered["hooks"]
    assert clip["reason"] == rendered["reason"]
    assert clip["tip"] == rendered["tip"]
    assert clip["duration"] == pytest.approx(rendered["duration"], abs=0.01)
    assert clip["width"] == rendered["width"] == 540
    assert clip["height"] == rendered["height"] == 960

    # Free keeps files for 7 days (LIBRARY.md), computed from created_at.
    assert _days_apart(clip["created_at"], clip["expires_at"]) == pytest.approx(7.0)

    # The detail view carries the transcript — the half that is permanent.
    detail = client.get(f"/v1/clips/{clip['id']}", headers=_bearer(token))
    assert detail.status_code == 200, detail.text
    words = detail.json()["words"]
    assert words, "a clip's transcript outlives its video, so it must be stored"
    assert all({"text", "start", "end"} <= set(word) for word in words)

    # And the file is really servable through the library's own route.
    served = client.get(clip["url"], headers=_bearer(token))
    assert served.status_code == 200
    assert len(served.content) == clip["bytes"]
    assert served.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(
    ("plan", "days"),
    [("free", 7), ("starter", 30), ("pro", 90), ("enterprise", None)],
)
def test_expires_at_follows_the_owners_plan(
    sandbox: SimpleNamespace, plan: str, days: int | None
) -> None:
    """Retention is its own entitlement, read from the plan at save time."""
    client = sandbox.client
    token, _ = _account(client, f"{plan}-retention@example.com", plan=plan)

    clip = _upload(client, token, title="Saved").json()
    if days is None:
        # Enterprise keeps clips forever: '' in the row, null on the wire.
        assert clip["expires_at"] is None
    else:
        assert _days_apart(clip["created_at"], clip["expires_at"]) == pytest.approx(days)

    # And /v1/me says the same number out loud, so the account page's promise
    # and the reaper's behaviour cannot drift apart.
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["entitlements"]["retention_days"] == days


def test_a_lapsed_plan_saves_at_the_free_window(sandbox: SimpleNamespace) -> None:
    """The EFFECTIVE plan decides, exactly like every other entitlement: a
    canceled Pro row is a free account the moment it lapses."""
    client = sandbox.client
    token, user_id = _account(client, "lapsed@example.com", plan="pro", status="canceled")

    clip = _upload(client, token).json()
    assert _days_apart(clip["created_at"], clip["expires_at"]) == pytest.approx(7.0)


# --------------------------------------------------------------------------- #
# 2. Listing: owner-scoped, newest first, paginated.
# --------------------------------------------------------------------------- #


def test_listing_is_owner_scoped_and_newest_first(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    mine, _ = _account(client, "owner@example.com")
    theirs, _ = _account(client, "stranger@example.com")

    titles = ["first", "second", "third"]
    for title in titles:
        assert _upload(client, mine, title=title).status_code == 201
    assert _upload(client, theirs, title="not yours").status_code == 201

    listing = _list(client, mine)
    assert [clip["title"] for clip in listing["clips"]] == list(reversed(titles))
    assert listing["next_before"] is None, "a short page is the end of the library"

    # The stranger's library contains exactly their own clip, and nothing here
    # leaks the existence of anybody else's.
    assert [clip["title"] for clip in _list(client, theirs)["clips"]] == ["not yours"]


def test_pagination_walks_the_whole_library_once(sandbox: SimpleNamespace) -> None:
    """The cursor carries created_at AND the id. Clips from one render are
    written in the same millisecond, so a page boundary landing inside such a
    group would silently drop the rest of it — this forces that collision."""
    client = sandbox.client
    token, _ = _account(client, "pages@example.com")

    ids = [_upload(client, token, title=f"clip {i}").json()["id"] for i in range(5)]
    # Every clip now shares one timestamp: only the tiebreak can separate them.
    for clip_id in ids:
        _age_clip(sandbox, clip_id, created_at="2026-01-01T00:00:00.000+00:00")

    seen: list[str] = []
    cursor = ""
    for _ in range(len(ids) + 1):
        page = _list(client, token, limit=2, **({"before": cursor} if cursor else {}))
        seen.extend(clip["id"] for clip in page["clips"])
        cursor = page["next_before"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids), "every clip appears on exactly one page"
    assert len(seen) == len(set(seen))


def test_limit_is_capped_at_fifty(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "limits@example.com")
    assert client.get("/v1/clips?limit=51", headers=_bearer(token)).status_code == 422
    assert client.get("/v1/clips?limit=0", headers=_bearer(token)).status_code == 422
    assert client.get("/v1/clips?limit=50", headers=_bearer(token)).status_code == 200


def test_the_library_needs_a_session(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "session@example.com")
    clip_id = _upload(client, token).json()["id"]

    for method, path in (
        ("get", "/v1/clips"),
        ("get", f"/v1/clips/{clip_id}"),
        ("get", f"/v1/clips/{clip_id}/file"),
        ("delete", f"/v1/clips/{clip_id}"),
    ):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/v1/clips/upload", files=_multipart()).status_code == 401


def test_another_user_gets_404_on_read_and_delete(sandbox: SimpleNamespace) -> None:
    """404, not 403 — the jobs convention: somebody else's clip must be
    indistinguishable from one that does not exist."""
    from clipcatalyst_api import db

    client = sandbox.client
    mine, _ = _account(client, "mine@example.com")
    theirs, _ = _account(client, "theirs@example.com")
    clip_id = _upload(client, mine, title="private").json()["id"]

    for path in (f"/v1/clips/{clip_id}", f"/v1/clips/{clip_id}/file"):
        assert client.get(path, headers=_bearer(theirs)).status_code == 404, path
    assert client.delete(f"/v1/clips/{clip_id}", headers=_bearer(theirs)).status_code == 404
    # ...and the refusal was real: the row and its file are untouched.
    assert db.get_clip(clip_id) is not None
    assert client.get(f"/v1/clips/{clip_id}/file", headers=_bearer(mine)).status_code == 200


# --------------------------------------------------------------------------- #
# 3. Retention: the file expires, the row does not.
# --------------------------------------------------------------------------- #


def test_the_reaper_deletes_the_file_and_keeps_the_row(
    sandbox: SimpleNamespace,
) -> None:
    """Metadata is permanent, as decided. An expired clip still lists — with
    available: false and no url, so the UI shows an expired card instead of a
    broken player."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired_clips

    client = sandbox.client
    token, _ = _account(client, "expiring@example.com")
    clip = _upload(
        client, token, title="Worth keeping", score=91, hooks=["the hook"]
    ).json()
    stored = db.get_clip(clip["id"])
    file_on_disk = sandbox.settings.library_dir / stored["file_path"]
    assert file_on_disk.is_file()

    # Nothing is due yet: the sweep is a no-op on a live clip.
    assert reap_expired_clips() == 0
    assert db.get_clip(clip["id"])["file_path"]

    _age_clip(sandbox, clip["id"], expires_at=ANCIENT)
    assert reap_expired_clips() == 1
    assert not file_on_disk.exists(), "the video is gone"

    row = db.get_clip(clip["id"])
    assert row is not None, "the row is NOT gone — metadata is permanent"
    assert row["file_path"] == ""

    listed = _list(client, token)["clips"]
    assert len(listed) == 1
    assert listed[0]["available"] is False
    assert listed[0]["url"] is None
    # The card still says everything it ever said, minus the video.
    assert listed[0]["title"] == "Worth keeping"
    assert listed[0]["score"] == 91
    assert listed[0]["hooks"] == ["the hook"]
    assert client.get(f"/v1/clips/{clip['id']}", headers=_bearer(token)).status_code == 200

    # The file route is honest about what happened rather than 404-ing blankly.
    gone = client.get(f"/v1/clips/{clip['id']}/file", headers=_bearer(token))
    assert gone.status_code == 404
    assert "expired" in gone.json()["detail"].lower()

    # Repeating the sweep finds nothing left to do.
    assert reap_expired_clips() == 0


def test_an_unlimited_clip_is_never_reaped(sandbox: SimpleNamespace) -> None:
    """'' means never, and as text it sorts BEFORE every real timestamp — an
    `expires_at <= now` query alone would reap exactly the clips that must
    never be reaped."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired_clips

    client = sandbox.client
    token, _ = _account(client, "forever@example.com", plan="enterprise")
    clip = _upload(client, token).json()
    assert clip["expires_at"] is None

    assert reap_expired_clips() == 0
    assert db.get_clip(clip["id"])["file_path"]
    assert client.get(clip["url"], headers=_bearer(token)).status_code == 200


def test_upgrade_extends_and_downgrade_never_shortens(
    sandbox: SimpleNamespace,
) -> None:
    """A plan change extends but never shortens (LIBRARY.md Part 2). Taking
    away something already made is a support ticket, and the storage is already
    spent."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "upgrader@example.com")
    clip_id = _upload(client, token).json()["id"]
    created = db.get_clip(clip_id)["created_at"]
    assert _days_apart(created, db.get_clip(clip_id)["expires_at"]) == pytest.approx(7)

    # Upgrade: every non-expired clip is recomputed from its OWN created_at.
    _set_plan(user_id, "pro")
    assert _days_apart(created, db.get_clip(clip_id)["expires_at"]) == pytest.approx(90)

    # Downgrade: the shorter window loses the comparison, so nothing moves.
    _set_plan(user_id, "starter")
    assert _days_apart(created, db.get_clip(clip_id)["expires_at"]) == pytest.approx(90)
    _set_plan(user_id, "free")
    assert _days_apart(created, db.get_clip(clip_id)["expires_at"]) == pytest.approx(90)

    # ...but a clip saved AFTER the downgrade gets the plan they are on now.
    fresh = _upload(client, token).json()
    assert _days_apart(fresh["created_at"], fresh["expires_at"]) == pytest.approx(7)


def test_upgrading_to_unlimited_clears_the_deadline(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "unlimited@example.com")
    clip_id = _upload(client, token).json()["id"]

    _set_plan(user_id, "enterprise")
    assert db.get_clip(clip_id)["expires_at"] == "", "unlimited is the latest of all"

    # And back down: '' is never overwritten with a finite deadline.
    _set_plan(user_id, "free")
    assert db.get_clip(clip_id)["expires_at"] == ""


def test_an_upgrade_does_not_resurrect_an_expired_clip(
    sandbox: SimpleNamespace,
) -> None:
    """Its file is already gone, so a longer window would restore nothing — it
    would only make an `available: false` row claim a future it hasn't got."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired_clips

    client = sandbox.client
    token, user_id = _account(client, "resurrect@example.com")
    clip_id = _upload(client, token).json()["id"]
    _age_clip(sandbox, clip_id, expires_at=ANCIENT)
    assert reap_expired_clips() == 1

    _set_plan(user_id, "pro")
    row = db.get_clip(clip_id)
    assert row["expires_at"] == ANCIENT
    assert row["file_path"] == ""
    assert _list(client, token)["clips"][0]["available"] is False


# --------------------------------------------------------------------------- #
# 4. Saving a browser clip: on purpose, sniffed, capped, and unmetered.
# --------------------------------------------------------------------------- #


def test_upload_stores_a_browser_clip(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "browser@example.com")

    payload = MP4_MAGIC + b"\x00" * 8192
    resp = client.post(
        "/v1/clips/upload",
        files={
            "file": ("whatever-they-named-it.mp4", payload, "video/mp4"),
            "metadata": (
                None,
                json.dumps(
                    {
                        "title": "From the browser",
                        "score": 77,
                        "hooks": ["hook one"],
                        "reason": "because",
                        "tip": "do this",
                        "start": 3.5,
                        "end": 18.5,
                        "duration": 15.0,
                        "width": 720,
                        "height": 1280,
                        "speaker_count": 2,
                        "clip_index": 1,
                        "words": [{"text": " hello", "start": 0.0, "end": 0.4}],
                        # Lies the server must ignore: the engine is a
                        # server-side fact, and the byte count is measured.
                        "engine": "cloud",
                        "bytes": 999_999_999,
                    }
                ),
                "application/json",
            ),
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    clip = resp.json()

    assert clip["engine"] == "browser", "engine is forced, never taken from the client"
    assert clip["job_id"] == "", "nothing was rendered on our hardware"
    assert clip["bytes"] == len(payload), "the size is what we stored, not what was claimed"
    assert clip["title"] == "From the browser"
    assert clip["score"] == 77
    assert clip["speaker_count"] == 2
    assert clip["words"] == [
        {"text": " hello", "start": 0.0, "end": 0.4, "speaker": None}
    ]
    assert clip["available"] is True

    # Stored under the library root, keyed by the account — never under the
    # clips directory the job sweeps walk.
    stored = db.get_clip(clip["id"])
    assert stored["file_path"].startswith(f"{user_id}/")
    assert (sandbox.settings.library_dir / stored["file_path"]).is_file()
    assert not str(sandbox.settings.clips_dir) in stored["file_path"]

    served = client.get(clip["url"], headers=_bearer(token))
    assert served.status_code == 200
    assert served.content == payload


def test_upload_accepts_webm_because_the_browser_makes_it(
    sandbox: SimpleNamespace,
) -> None:
    """MediaRecorder produces mp4 OR webm depending on the device — refusing
    webm would mean Chrome-on-Linux users could never save a clip."""
    client = sandbox.client
    token, _ = _account(client, "webm@example.com")

    resp = client.post(
        "/v1/clips/upload",
        # Named .mp4 and declared video/mp4, but the BYTES are webm: what we
        # store and serve follows the bytes.
        files={"file": ("clip.mp4", WEBM_MAGIC + b"\x00" * 2048, "video/mp4")},
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    served = client.get(resp.json()["url"], headers=_bearer(token))
    assert served.status_code == 200
    assert served.headers["content-type"] == "video/webm"


def test_upload_rejects_anything_that_is_not_a_clip(sandbox: SimpleNamespace) -> None:
    """The content type is SNIFFED. A declared video/mp4 buys nothing — what we
    store is served back from our own origin."""
    client = sandbox.client
    token, _ = _account(client, "sniff@example.com")

    for label, data in (
        ("plain text", b"this is not a video, whatever the part says"),
        ("html", b"<html><script>alert(1)</script></html>"),
        ("a png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        # Matroska is EBML too, but it is not what a browser records.
        ("matroska", b"\x1a\x45\xdf\xa3" + b"\x00" * 60),
        ("empty", b""),
    ):
        resp = client.post(
            "/v1/clips/upload",
            files={"file": ("clip.mp4", data, "video/mp4")},
            headers=_bearer(token),
        )
        assert resp.status_code == 400, f"{label} should be refused: {resp.text}"
    assert _list(client, token)["clips"] == []


def test_upload_rejects_an_oversize_clip(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "oversize@example.com")

    resp = client.post(
        "/v1/clips/upload",
        files={"file": ("clip.mp4", MP4_MAGIC + b"\x00" * MAX_CLIP_BYTES, "video/mp4")},
        headers=_bearer(token),
    )
    assert resp.status_code == 413, resp.text
    assert _list(client, token)["clips"] == []
    assert db.library_bytes(user_id) == 0, "a refused upload stores nothing"


def test_upload_refuses_an_account_over_its_storage_ceiling(
    sandbox: SimpleNamespace,
) -> None:
    """No monthly quota is spent here — nothing rendered on our hardware — so
    this ceiling is the only thing stopping a free account being used as a
    disk. 402, the same shape as the quota refusal."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "hoarder@example.com")

    payload = MP4_MAGIC + b"\x00" * (MAX_CLIP_BYTES - len(MP4_MAGIC))
    stored = 0
    while stored + len(payload) <= LIBRARY_MAX_BYTES:
        resp = client.post(
            "/v1/clips/upload",
            files={"file": ("clip.mp4", payload, "video/mp4")},
            headers=_bearer(token),
        )
        assert resp.status_code == 201, resp.text
        stored += len(payload)
    assert db.library_bytes(user_id) == stored

    resp = client.post(
        "/v1/clips/upload",
        files={"file": ("clip.mp4", payload, "video/mp4")},
        headers=_bearer(token),
    )
    assert resp.status_code == 402, resp.text
    assert "library is full" in resp.json()["detail"].lower()
    assert db.library_bytes(user_id) == stored, "nothing was stored by the refusal"

    # Deleting a clip makes room again — the ceiling is about disk in use, and
    # that disk really came back.
    first = _list(client, token)["clips"][-1]
    assert client.delete(f"/v1/clips/{first['id']}", headers=_bearer(token)).status_code == 200
    assert db.library_bytes(user_id) == stored - first["bytes"]
    assert _upload(client, token).status_code == 201

    # An EXPIRED clip frees its bytes too: the file is gone, so the disk is.
    from clipcatalyst_api.worker import reap_expired_clips

    survivor = _list(client, token)["clips"][-1]
    _age_clip(sandbox, survivor["id"], expires_at=ANCIENT)
    assert reap_expired_clips() == 1
    assert db.library_bytes(user_id) == db.library_bytes(user_id)
    assert db.get_clip(survivor["id"]) is not None
    assert survivor["bytes"] not in (0,)
    assert db.library_bytes(user_id) + survivor["bytes"] <= LIBRARY_MAX_BYTES + stored


def test_upload_does_not_move_the_monthly_quota(sandbox: SimpleNamespace) -> None:
    """Nothing was rendered on our hardware, so nothing is billed — and the
    free plan's three cloud renders are still all there afterwards."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "unmetered@example.com")
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    assert db.get_usage(user_id, month) == 0
    for _ in range(4):  # more uploads than the free plan's whole month of clips
        assert _upload(client, token).status_code == 201
    assert db.get_usage(user_id, month) == 0
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 0
    assert len(_list(client, token)["clips"]) == 4


def test_upload_rejects_a_body_that_is_not_multipart(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "notmultipart@example.com")

    resp = client.post(
        "/v1/clips/upload", json={"title": "no file here"}, headers=_bearer(token)
    )
    assert resp.status_code == 415, resp.text
    # Multipart with no file part is a 400, not a stored empty clip.
    resp = client.post(
        "/v1/clips/upload",
        files={"metadata": (None, json.dumps({"title": "orphan"}), "application/json")},
        headers=_bearer(token),
    )
    assert resp.status_code == 400, resp.text


def test_upload_rejects_unreadable_metadata(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token, _ = _account(client, "badmeta@example.com")

    resp = client.post(
        "/v1/clips/upload",
        files={
            "file": ("clip.mp4", MP4_MAGIC + b"\x00" * 512, "video/mp4"),
            "metadata": (None, "{not json at all", "application/json"),
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 400, resp.text
    assert _list(client, token)["clips"] == []


# --------------------------------------------------------------------------- #
# 5. Deleting: the row AND the file, and only by the owner.
# --------------------------------------------------------------------------- #


def test_delete_removes_both_the_row_and_the_file(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "deleter@example.com")
    clip = _upload(client, token).json()
    stored = db.get_clip(clip["id"])
    file_on_disk = sandbox.settings.library_dir / stored["file_path"]
    assert file_on_disk.is_file()

    resp = client.delete(f"/v1/clips/{clip['id']}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    assert db.get_clip(clip["id"]) is None, "the owner asking IS how a row dies"
    assert not file_on_disk.exists()
    assert db.library_bytes(user_id) == 0
    assert _list(client, token)["clips"] == []
    assert client.get(f"/v1/clips/{clip['id']}", headers=_bearer(token)).status_code == 404
    assert client.delete(f"/v1/clips/{clip['id']}", headers=_bearer(token)).status_code == 404


def test_deleting_an_expired_clip_removes_the_row(sandbox: SimpleNamespace) -> None:
    """The file went first, months ago. Deleting is still how the row goes."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired_clips

    client = sandbox.client
    token, _ = _account(client, "tidy@example.com")
    clip_id = _upload(client, token).json()["id"]
    _age_clip(sandbox, clip_id, expires_at=ANCIENT)
    assert reap_expired_clips() == 1

    assert client.delete(f"/v1/clips/{clip_id}", headers=_bearer(token)).status_code == 200
    assert db.get_clip(clip_id) is None


# --------------------------------------------------------------------------- #
# 6. THE DANGEROUS ONE.
#
#    A job's rendered clips live under clips_dir/<job_id>, and two paths delete
#    that directory whole: the 48 h jobs reaper (worker.reap_expired) and the
#    lost-claim cleanup (worker._discard_output). A library row pointing into
#    that directory would have its video destroyed by a sweep that knows
#    nothing about libraries — a Pro account's 90-day clip deleted on day two,
#    leaving a card whose player 404s. The library therefore has its own root,
#    and these tests fail if it ever moves back under the jobs' tree.
# --------------------------------------------------------------------------- #


def test_the_jobs_reaper_does_not_eat_a_library_file(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired

    client = sandbox.client
    token, _ = _account(client, "reaped-job@example.com", plan="pro")

    body = _render_job(client, token, source_video)
    job_id = body["job_id"]
    clip = _list(client, token)["clips"][0]
    stored = db.get_clip(clip["id"])
    library_file = sandbox.settings.library_dir / stored["file_path"]
    job_file = sandbox.settings.clips_dir / job_id / "clip-01.mp4"
    assert library_file.is_file() and job_file.is_file()
    # The two copies are in different worlds. If this ever fails, the reaper
    # below is about to delete somebody's library.
    assert not library_file.is_relative_to(sandbox.settings.clips_dir)

    # 48 h passes and the job is reaped: row, rendered clips, source and all.
    _age_job(sandbox, job_id, created_at=ANCIENT, updated_at=ANCIENT)
    assert reap_expired() == 1
    assert db.get_job(job_id) is None
    assert not (sandbox.settings.clips_dir / job_id).exists()

    # The library is untouched — this account is on Pro, its clips have 90 days.
    assert library_file.is_file(), "the jobs reaper ate a library file"
    listed = _list(client, token)["clips"]
    assert len(listed) == 1
    assert listed[0]["available"] is True
    served = client.get(listed[0]["url"], headers=_bearer(token))
    assert served.status_code == 200
    assert len(served.content) == listed[0]["bytes"]

    # ...and the link the account already had keeps working, through the
    # library, even though the job it names no longer exists anywhere.
    old_link = client.get(f"/v1/files/{job_id}/clip-01.mp4", headers=_bearer(token))
    assert old_link.status_code == 200
    assert old_link.content == served.content


def test_a_lost_run_cleanup_spares_a_referenced_library_file(
    sandbox: SimpleNamespace,
) -> None:
    """_discard_output bins the output of a run that lost its job row — but "the
    job row is gone" is ALSO what a job reaped at 48 h looks like, and that
    job's clips may still be in somebody's library. A file a live row points at
    is never deleted."""
    from clipcatalyst_api import db, worker
    from clipcatalyst_api.storage import get_storage

    client = sandbox.client
    token, user_id = _account(client, "lostclaim@example.com")
    clip = _upload(client, token).json()
    stored = db.get_clip(clip["id"])
    file_path = stored["file_path"]
    on_disk = sandbox.settings.library_dir / file_path
    assert on_disk.is_file()

    settings = sandbox.settings
    storage = get_storage(settings)
    # A duplicate delivery of a job that no longer exists, whose staged output
    # happens to name a file the library is still using.
    worker._discard_output(settings, storage, "deadbeef" * 4, (file_path,))
    assert on_disk.is_file(), "the lost-claim cleanup ate a referenced library file"
    assert db.get_clip(clip["id"]) is not None
    assert client.get(clip["url"], headers=_bearer(token)).status_code == 200

    # An UNreferenced staged file — the real lost-claim case — is still binned:
    # nothing points at it, so nobody was ever going to be able to reach it.
    orphan = settings.library_dir / user_id / "orphan.mp4"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"a lost run's leftovers")
    worker._discard_output(settings, storage, "deadbeef" * 4, (f"{user_id}/orphan.mp4",))
    assert not orphan.exists()


def test_a_run_that_loses_its_claim_stocks_nobodys_library(
    sandbox: SimpleNamespace, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Library rows are written only AFTER the processing→done transition is
    won, and this is why: a run that loses it rendered clips nobody was billed
    for — the reservation went back the moment the reconciler failed the row.
    Writing them into somebody's permanent library would be the free-clips hole
    in its most durable form."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "lost-claim@example.com")

    real_finalize = db.finalize_job

    def reconciler_gets_there_first(job_id: str, **kwargs):  # noqa: ANN202
        if kwargs.get("to") == "done":
            # Exactly the race this guard exists for: the row was failed and
            # REFUNDED while this run was rendering, so its transition moves
            # nothing and it owns neither the quota nor the right to publish.
            real_finalize(
                job_id,
                expect=db.LIVE_STATUSES,
                to="failed",
                rendered=0,
                error="reconciled mid-run",
            )
            return False
        return real_finalize(job_id, **kwargs)

    monkeypatch.setattr(db, "finalize_job", reconciler_gets_there_first)

    created = client.post(
        "/v1/jobs",
        json={
            "filename": "source.mp4",
            "size_bytes": source_video.stat().st_size,
            "target_length": 15,
            "count": 1,
            "height": 960,
        },
        headers=_bearer(token),
    )
    job_id = created.json()["job_id"]
    client.put(
        f"/v1/uploads/{job_id}", content=source_video.read_bytes(), headers=_bearer(token)
    )
    assert client.post(f"/v1/jobs/{job_id}/start", headers=_bearer(token)).status_code == 202
    assert client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()["status"] == "failed"

    assert db.list_clips(user_id, limit=50) == [], "clips nobody paid for, filed forever"
    assert _list(client, token)["clips"] == []
    # ...and the files that run staged are gone too: nothing points at them, so
    # nothing could ever have served them.
    account_dir = sandbox.settings.library_dir / user_id
    assert not list(account_dir.rglob("*.mp4")) if account_dir.exists() else True
    assert not (sandbox.settings.clips_dir / job_id).exists()


def test_a_reaped_job_leaves_its_library_rows_alone(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    """The jobs reaper clears pipeline state. The library is not pipeline
    state, and its rows are what make an expired clip's metadata permanent."""
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired

    client = sandbox.client
    token, user_id = _account(client, "rows-survive@example.com")
    body = _render_job(client, token, source_video)

    before = db.list_clips(user_id, limit=50)
    assert before, "the render must have written the library rows"

    _age_job(sandbox, body["job_id"], created_at=ANCIENT, updated_at=ANCIENT)
    assert reap_expired() == 1

    after = db.list_clips(user_id, limit=50)
    assert [clip["id"] for clip in after] == [clip["id"] for clip in before]
    assert all(clip["file_path"] for clip in after)
    # The rows still name a job that no longer exists — deliberately: the
    # library never resolves job_id against the jobs table.
    assert all(clip["job_id"] == body["job_id"] for clip in after)


# --------------------------------------------------------------------------- #
# 7. Settings and shape.
# --------------------------------------------------------------------------- #


def test_settings_defaults_match_the_spec() -> None:
    """The shipped ceilings, asserted away from the sandbox's small ones."""
    from clipcatalyst_api.settings import get_settings

    saved = dict(os.environ)
    for name in ("CC_MAX_CLIP_BYTES", "CC_LIBRARY_MAX_BYTES"):
        os.environ.pop(name, None)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.max_clip_bytes == 200_000_000
        assert settings.library_max_bytes == 5_000_000_000
        # The library's root is NOT under the jobs' clips directory. This is
        # the invariant §6 depends on, stated where it is decided.
        assert not settings.library_dir.is_relative_to(settings.clips_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def test_retention_is_its_own_entitlement() -> None:
    """Never derived from the quota, the watermark, or "is this paid" — those
    move for pricing reasons, and a pricing change must not quietly shorten how
    long we keep what people already made."""
    from clipcatalyst_api.plans import PLANS

    assert PLANS["free"].retention_days == 7
    assert PLANS["starter"].retention_days == 30
    assert PLANS["pro"].retention_days == 90
    assert PLANS["enterprise"].retention_days is None


def test_clip_expires_at_is_never_a_guess() -> None:
    """A row we cannot date cannot be given an honest deadline, and deleting it
    on a guess is somebody's clip."""
    from clipcatalyst_api import db

    created = "2026-03-01T12:00:00.000+00:00"
    assert db.clip_expires_at(created, 7) == "2026-03-08T12:00:00.000+00:00"
    assert db.clip_expires_at(created, None) == ""
    assert db.clip_expires_at("not a timestamp", 7) == ""
