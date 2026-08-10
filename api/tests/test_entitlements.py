"""Entitlement enforcement: height clamping, forced watermarks, monthly quota.

This is the suite that makes the pricing page's promises true, so every
assertion is against what the SERVER decided (the stored job row, the usage
table, the 402 body) rather than against anything a client sent.

Mirrors the env/import dance of ``test_auth.py`` — all CC_* vars are set BEFORE
any app module is imported, ``get_settings`` is lru_cached so its cache is
cleared, and the settings-snapshotting modules (queue_app / worker / main) are
purged for a clean re-import. The whole os.environ is snapshotted and restored
around each client.

Quota-only tests stub the pipeline out (``quiet_sandbox``): /start RESERVES
the quota and claims the job, then dispatches nothing, so the gate can be
exercised without paying for a render — and that is also the exact shape of
the production (redis) queue, where /start returns long before anything
renders. The usage-accounting tests run the real pipeline in eager mode
(``CC_QUEUE=eager``) with a faked transcriber and a generated test video,
because "settles down to the clips ACTUALLY rendered" is only true if real
clips were actually rendered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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


def _month() -> str:
    """The current 'YYYY-MM' UTC month key — how usage rows are keyed."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _next_month(month: str) -> str:
    """The month after a 'YYYY-MM' — computed independently of main.py's helper."""
    year, mon = (int(part) for part in month.split("-"))
    first_next = (
        datetime(year, mon, 28, tzinfo=timezone.utc) + timedelta(days=7)
    ).replace(day=1)
    return first_next.strftime("%Y-%m")


@pytest.fixture(scope="session")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_video(tmp_path_factory.mktemp("entmedia") / "source.mp4")


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; founder token starts OFF."""
    saved_env = dict(os.environ)
    transcript = _make_transcript(tmp_path_factory.mktemp("entfix") / "transcript.json")
    data_dir = tmp_path_factory.mktemp("entdata")

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
            "CC_BILLING": "off",  # plans are set directly here; billing is tested elsewhere
        }
    )
    os.environ.pop("CC_API_TOKEN", None)  # open by default
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth
    from clipcatalyst_api.main import app

    # auth's rate-limit windows are per-process and survive the purge (the
    # submodule stays reachable as a package attribute) — every test here
    # registers accounts, so clear the window table on the way in AND on the
    # way out: this module neither inherits nor leaks credential-route counters.
    auth.reset_rate_limits()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, data_dir=data_dir)
    finally:
        auth.reset_rate_limits()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


@pytest.fixture()
def quiet_sandbox(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    """`sandbox` with the pipeline stubbed: /start still claims the job and
    reserves its quota, then dispatches nothing. Renders cost ~seconds each and
    prove nothing extra about a 402 — and a job that never completes is exactly
    the state the quota bypass lived in."""
    from clipcatalyst_api import main

    monkeypatch.setattr(main, "process_job", SimpleNamespace(delay=lambda job_id: None))
    return sandbox


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
        db.update_user(user_id, plan=plan, plan_status=status)
    return token, user_id


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


def _create_job(client, token: str | None, **overrides) -> dict:
    headers = _bearer(token) if token else {}
    resp = client.post("/v1/jobs", json=_job_payload(**overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ready_job(client, token: str | None = None, **overrides) -> str:
    """A job whose source is on disk — the state /start's quota check runs in."""
    headers = _bearer(token) if token else {}
    job_id = _create_job(client, token, **overrides)["job_id"]
    ack = client.put(
        f"/v1/uploads/{job_id}", content=b"stand-in source bytes", headers=headers
    )
    assert ack.status_code == 200, ack.text
    return job_id


def _start(client, job_id: str, token: str | None = None):  # noqa: ANN202 - Response
    headers = _bearer(token) if token else {}
    return client.post(f"/v1/jobs/{job_id}/start", headers=headers)


# --------------------------------------------------------------------------- #
# 1. Height clamping + forced watermark, driven by the EFFECTIVE plan.
# --------------------------------------------------------------------------- #


def test_free_plan_clamps_height_and_forces_the_watermark(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db, worker

    client = sandbox.client
    token, _ = _account(client, "free@example.com")

    created = _create_job(client, token, height=1920)
    assert created["height"] == 1280, "free tops out at 720p-wide (1280 tall)"
    assert created["watermark"] is True

    # The reply is not the promise — the STORED row is what the renderer reads.
    job = db.get_job(created["job_id"])
    assert job["height"] == 1280
    assert worker._watermark_for(job) is True


@pytest.mark.parametrize(
    ("plan", "status", "requested", "height", "watermark"),
    [
        ("free", "", 1920, 1280, True),
        ("free", "", 3840, 1280, True),
        ("starter", "active", 3840, 1920, False),
        ("starter", "trialing", 3840, 1920, False),
        ("starter", "past_due", 3840, 1920, False),  # dunning grace keeps entitlements
        ("pro", "active", 3840, 3840, False),  # 4K is cloud-only and real
        ("pro", "active", 1280, 1280, False),  # a smaller ask is never raised
        ("enterprise", "active", 3840, 3840, False),
        ("pro", "canceled", 3840, 1280, True),  # lapsed → free
        ("pro", "unpaid", 3840, 1280, True),
        ("pro", "", 3840, 1280, True),  # a plan name with no live status
    ],
)
def test_height_and_watermark_follow_the_effective_plan(
    sandbox: SimpleNamespace,
    plan: str,
    status: str,
    requested: int,
    height: int,
    watermark: bool,
) -> None:
    from clipcatalyst_api import db, worker

    client = sandbox.client
    token, _ = _account(client, f"{plan}-{status or 'none'}@example.com", plan=plan, status=status)

    created = _create_job(client, token, height=requested)
    assert created["height"] == height
    assert created["watermark"] is watermark

    job = db.get_job(created["job_id"])
    assert job["height"] == height
    assert worker._watermark_for(job) is watermark


def test_nothing_the_client_sends_can_raise_its_own_entitlements(
    sandbox: SimpleNamespace,
) -> None:
    """Entitlements are server-side facts; extra request fields are noise."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "liar@example.com")

    resp = client.post(
        "/v1/jobs",
        json={
            **_job_payload(height=1920),
            "watermark": False,
            "plan": "pro",
            "max_height": 3840,
            "user_id": "somebody-else",
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["height"] == 1280
    assert body["watermark"] is True
    job = db.get_job(body["job_id"])
    assert job["height"] == 1280
    assert job["user_id"] == user_id  # ownership comes from the session, not the body


def test_watermark_is_decided_from_the_owner_at_render_time(
    sandbox: SimpleNamespace,
) -> None:
    """An upgrade landing between create and start is honoured, and a lapsed
    plan cannot keep exporting clean clips off an old job row."""
    from clipcatalyst_api import db, worker

    client = sandbox.client
    token, user_id = _account(client, "midflight@example.com")
    job = db.get_job(_create_job(client, token)["job_id"])
    assert worker._watermark_for(job) is True

    db.update_user(user_id, plan="pro", plan_status="active")
    assert worker._watermark_for(job) is False

    db.update_user(user_id, plan_status="canceled")
    assert worker._watermark_for(job) is True

    # Anonymous founder/dev jobs keep the pre-accounts cloud default.
    assert worker._watermark_for({"user_id": ""}) is True


def test_anonymous_and_founder_jobs_are_never_clamped(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client

    # Dev-open mode: no credential at all, exactly as before accounts existed.
    created = _create_job(client, None, height=3840)
    assert created["height"] == 3840
    assert created["watermark"] is True
    assert db.get_job(created["job_id"])["user_id"] == ""

    _set_token(FOUNDER_TOKEN)
    try:
        created = _create_job(client, FOUNDER_TOKEN, height=3840)
        assert created["height"] == 3840
        assert created["watermark"] is True
        assert db.get_job(created["job_id"])["user_id"] == ""
    finally:
        _set_token(None)


# --------------------------------------------------------------------------- #
# 2. Monthly quota: 402 at /start, honest message, nothing burned.
# --------------------------------------------------------------------------- #


def test_free_quota_returns_402_naming_the_plan_and_limit(
    quiet_sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "spent@example.com")
    month = _month()
    db.add_usage(user_id, month, 3)  # the whole free month, already rendered

    job_id = _ready_job(client, token, count=1)
    resp = _start(client, job_id, token)
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert "free plan includes 3 clips per month" in detail
    assert month in detail
    assert _next_month(month) in detail

    # A 402 does not burn the job: it stays startable for when the plan grows.
    assert db.get_job(job_id)["status"] == "awaiting_upload"
    db.update_user(user_id, plan="starter", plan_status="active")
    assert _start(client, job_id, token).status_code == 202


def test_quota_counts_the_whole_job_not_one_clip(
    quiet_sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "partial@example.com")
    db.add_usage(user_id, _month(), 2)

    # 2 used + 2 requested = 4 > 3: refused before anything is claimed.
    assert _start(client, _ready_job(client, token, count=2), token).status_code == 402
    # 2 used + 1 requested = 3: exactly the limit, allowed.
    assert _start(client, _ready_job(client, token, count=1), token).status_code == 202


def test_canceled_plan_is_enforced_exactly_as_free(
    quiet_sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "lapsed@example.com", plan="pro", status="canceled")
    db.add_usage(user_id, _month(), 3)

    created = _create_job(client, token, height=3840)
    assert created["height"] == 1280
    assert created["watermark"] is True

    job_id = _ready_job(client, token, count=1)
    resp = _start(client, job_id, token)
    assert resp.status_code == 402
    # The message names the plan being ENFORCED (free), not the stored name.
    assert "free plan includes 3 clips per month" in resp.json()["detail"]

    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == "pro"  # the stored subscription fact is still shown
    assert me["quota"]["limit"] == 3  # but the enforced entitlement is free's


def test_month_rollover_frees_the_quota(quiet_sandbox: SimpleNamespace) -> None:
    """Usage is keyed by UTC month, so last month's spend never blocks today."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "rollover@example.com")

    month = _month()
    previous = "2000-01"  # unambiguously a past month key
    db.add_usage(user_id, previous, 3)
    assert db.get_usage(user_id, previous) == 3
    assert db.get_usage(user_id, month) == 0

    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["quota"] == {"limit": 3, "used": 0, "month": month}
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202

    # And the spent month still blocks if the clock is standing in it: seed the
    # CURRENT key and the very same request is refused.
    db.add_usage(user_id, month, 3)
    assert _start(client, _ready_job(client, token, count=1), token).status_code == 402


def test_paid_plans_get_their_larger_ceilings(quiet_sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "starter@example.com", plan="starter", status="active")
    db.add_usage(user_id, _month(), 29)  # 29 of 30 spent

    assert _start(client, _ready_job(client, token, count=2), token).status_code == 402
    assert _start(client, _ready_job(client, token, count=1), token).status_code == 202


def test_enterprise_has_no_monthly_ceiling(quiet_sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(
        client, "unlimited@example.com", plan="enterprise", status="active"
    )
    db.add_usage(user_id, _month(), 100_000)

    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["limit"] is None
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202


def test_founder_and_anonymous_starts_have_no_quota(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """Owner-less jobs bill no one, so no counter can ever gate them."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    # A signed-in user with a spent month exists alongside — their usage must
    # not leak onto unowned jobs.
    _, user_id = _account(client, "noisy-neighbour@example.com")
    db.add_usage(user_id, _month(), 999)

    assert _start(client, _ready_job(client, None, count=3), None).status_code == 202

    _set_token(FOUNDER_TOKEN)
    try:
        job_id = _ready_job(client, FOUNDER_TOKEN, count=3, height=3840)
        assert db.get_job(job_id)["height"] == 3840
        assert _start(client, job_id, FOUNDER_TOKEN).status_code == 202
    finally:
        _set_token(None)


# --------------------------------------------------------------------------- #
# 3. Usage accounting: the counter moves by the clips that really rendered.
# --------------------------------------------------------------------------- #


def test_usage_increments_by_the_clips_actually_rendered(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "renderer@example.com")
    month = _month()
    assert db.get_usage(user_id, month) == 0

    created = _create_job(
        client, token, size_bytes=source_video.stat().st_size, count=2, height=960
    )
    job_id = created["job_id"]
    assert created["height"] == 960  # already under the free ceiling: untouched

    assert (
        client.put(
            f"/v1/uploads/{job_id}",
            content=source_video.read_bytes(),
            headers=_bearer(token),
        ).status_code
        == 200
    )
    # Eager queue: the whole pipeline (real ffmpeg renders) runs inline here.
    assert _start(client, job_id, token).status_code == 202

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "done", f"pipeline failed: {status.get('error')!r}"
    rendered = len(status["clips"])
    assert rendered >= 1

    # Billed for what was rendered — not for what was requested or planned.
    assert db.get_usage(user_id, month) == rendered
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["quota"] == {"limit": 3, "used": rendered, "month": month}

    # The account page and the enforcement path read the same row: a follow-up
    # job that would cross the line is refused with that same number.
    second = _ready_job(client, token, count=3)
    refused = _start(client, second, token)
    assert refused.status_code == 402
    assert f"{rendered} are used for {month}" in refused.json()["detail"]

    # Nobody else was billed, and no other month moved.
    assert db.get_usage(user_id, "2000-01") == 0
    assert db.get_usage("someone-else", month) == 0


def test_anonymous_renders_bill_no_one(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    """Founder/dev jobs have no owner, so completion writes no usage row."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "watcher@example.com")
    month = _month()

    created = _create_job(client, None, size_bytes=source_video.stat().st_size, count=1)
    job_id = created["job_id"]
    assert (
        client.put(f"/v1/uploads/{job_id}", content=source_video.read_bytes()).status_code
        == 200
    )
    assert _start(client, job_id, None).status_code == 202

    status = client.get(f"/v1/jobs/{job_id}").json()
    assert status["status"] == "done", f"pipeline failed: {status.get('error')!r}"
    assert status["clips"]
    assert db.get_usage(user_id, month) == 0
    assert db.get_usage("", month) == 0
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 0


# --------------------------------------------------------------------------- #
# 4. The quota is RESERVED at /start, then settled — the two proven bypasses.
#
# The counter used to move only at COMPLETION while /start merely read it, so
# the ceiling only bound jobs that had already finished. Both holes below were
# demonstrated end to end against the old code; each test here fails on it.
# --------------------------------------------------------------------------- #


def test_queued_jobs_cannot_outrun_the_quota(quiet_sandbox: SimpleNamespace) -> None:
    """PROVEN BYPASS: five starts on a three-clip plan were all accepted.

    Production runs CC_QUEUE=redis, so /start returns 202 the moment the task
    is enqueued and the counter stays at 0 until a render finishes — every job
    an attacker could queue before the first one completed read that same 0
    and was admitted, rendering and billing all of them. `quiet_sandbox` is
    that shape exactly: nothing here ever completes, so on the old code
    nothing would ever bill.
    """
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "queue-flood@example.com")
    month = _month()

    codes = [
        _start(client, _ready_job(client, token, count=1), token).status_code
        for _ in range(5)
    ]
    assert codes == [202, 202, 202, 402, 402], codes

    # Three clips are spoken for even though not one of them has rendered, and
    # the refusals cost nothing on top.
    assert db.get_usage(user_id, month) == 3
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 3


def test_concurrent_starts_cannot_both_take_the_last_clip(
    quiet_sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROVEN RACE: two starts that both read `used` before either wrote it
    both fit through a one-clip gap.

    The barrier makes the interleaving deterministic instead of hoping for
    unlucky scheduling: both requests are held together at the owner lookup
    that immediately precedes the quota gate, and released at the same instant.
    Whatever the threads do next, the database is the one deciding who gets
    the last clip.
    """
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "racer@example.com")
    month = _month()
    db.add_usage(user_id, month, 2)  # exactly ONE clip left on the free plan

    job_ids = [_ready_job(client, token, count=1) for _ in range(2)]

    gate = threading.Barrier(2, timeout=30)
    owner_lookup = db.get_user_by_id

    def synchronized(owner_id: str) -> dict | None:
        owner = owner_lookup(owner_id)
        gate.wait()  # both starts stand at the quota gate together
        return owner

    monkeypatch.setattr(db, "get_user_by_id", synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda j: _start(client, j, token).status_code, job_ids))

    assert codes == [202, 402], codes
    assert db.get_usage(user_id, month) == 3
    # The loser's job was never burned either: it is still startable.
    assert db.get_job(job_ids[0])["status"] in {"queued", "awaiting_upload"}


def test_a_start_that_loses_the_claim_never_burns_quota(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """A duplicate /start is a 409, and a 409 must cost nothing: the job is
    claimed BEFORE the reservation precisely so a start that loses the claim
    cannot spend a clip the winner is already holding."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "double-start@example.com")
    month = _month()
    job_id = _ready_job(client, token, count=1)

    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 1
    assert _start(client, job_id, token).status_code == 409
    assert db.get_usage(user_id, month) == 1


def test_a_refused_start_leaves_the_counter_and_the_job_alone(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """The reservation is all-or-nothing: a job that doesn't fit takes no part
    of the quota, and the claim it briefly held is handed straight back."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "no-partials@example.com")
    month = _month()
    db.add_usage(user_id, month, 2)  # one clip left; this job wants three

    job_id = _ready_job(client, token, count=3)
    resp = _start(client, job_id, token)
    assert resp.status_code == 402
    assert "2 are used for" in resp.json()["detail"]
    assert db.get_usage(user_id, month) == 2  # not 2 + a partial 1
    job = db.get_job(job_id)
    assert job["status"] == "awaiting_upload"  # startable again after an upgrade
    assert job["usage_reserved"] == 0


def test_the_reservation_is_booked_in_the_month_it_was_taken(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """Usage is keyed by UTC month; the settlement follows the reservation into
    the month whose ceiling it actually consumed, not whatever month the render
    happens to finish in."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "booked@example.com")
    month = _month()

    job_id = _ready_job(client, token, count=2)
    assert _start(client, job_id, token).status_code == 202

    job = db.get_job(job_id)
    assert job["usage_reserved"] == 2
    assert job["usage_month"] == month
    assert db.get_usage(user_id, month) == 2
    assert db.get_usage(user_id, "2000-01") == 0

    assert db.settle_usage(job_id, 1) == -1
    assert db.get_usage(user_id, month) == 1
    assert db.get_usage(user_id, "2000-01") == 0


def test_settlement_bills_only_the_clips_that_rendered(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """A three-clip job holds three; if two render, the third comes back and
    is immediately spendable again."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "settler@example.com")
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 3  # the whole free month, up front

    # What worker._run does on completion, with two of the three clips rendered.
    assert db.settle_usage(job_id, 2) == -1
    assert db.get_usage(user_id, month) == 2
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 2
    assert db.get_job(job_id)["usage_reserved"] == 0

    # The returned clip is real quota: the next single-clip job fits.
    assert _start(client, _ready_job(client, token, count=1), token).status_code == 202
    assert _start(client, _ready_job(client, token, count=1), token).status_code == 402


def test_settling_twice_never_double_bills(quiet_sandbox: SimpleNamespace) -> None:
    """Celery can redeliver a task, and a job that fails after clips were
    already settled runs the release path on top of the completion path.
    Settlement is once-only, so every extra call is a no-op."""
    from clipcatalyst_api import db, worker

    client = quiet_sandbox.client
    token, user_id = _account(client, "retried@example.com")
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202
    assert db.get_usage(user_id, month) == 3

    assert db.settle_usage(job_id, 2) == -1
    assert db.settle_usage(job_id, 2) == 0  # the retry
    assert db.settle_usage(job_id, 0) == 0  # a failure path arriving afterwards
    worker._fail(job_id, "late failure")  # the same, through the worker
    assert db.get_usage(user_id, month) == 2

    # A job that never reserved anything (anonymous) settles to nothing at all.
    anon_job = _ready_job(client, None, count=3)
    assert db.settle_usage(anon_job, 3) == 0
    assert db.get_usage("", month) == 0


def test_the_monthly_counter_never_goes_negative(sandbox: SimpleNamespace) -> None:
    """Releases are negative deltas on a shared counter — no ordering of them
    may ever leave a user with a negative bill (i.e. free clips)."""
    from clipcatalyst_api import db

    month = _month()
    db.add_usage("counter@example.com", month, 2)
    db.add_usage("counter@example.com", month, -5)
    assert db.get_usage("counter@example.com", month) == 0
    db.add_usage("fresh@example.com", month, -5)  # no row yet
    assert db.get_usage("fresh@example.com", month) == 0
    db.add_usage("fresh@example.com", month, 1)
    assert db.get_usage("fresh@example.com", month) == 1


def test_crash_recovery_hands_back_a_stranded_reservation(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """A worker killed mid-render never settles. Boot-time reconciliation
    fails the stranded row, so it has to return the quota too — otherwise a
    crash silently costs the owner a month they never rendered."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(client, "stranded@example.com")
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202  # queued, never runs
    assert db.get_usage(user_id, month) == 3

    # What _lifespan does on the next boot, with a cutoff past this row.
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="milliseconds"
    )
    assert db.reconcile_stalled(future) == 1
    assert db.get_job(job_id)["status"] == "failed"
    assert db.get_usage(user_id, month) == 0
    assert db.get_job(job_id)["usage_reserved"] == 0
    # Running it again neither re-fails nor re-refunds anything.
    assert db.reconcile_stalled(future) == 0
    assert db.get_usage(user_id, month) == 0


def test_a_reservation_with_no_job_to_settle_it_is_refused(
    sandbox: SimpleNamespace,
) -> None:
    """The counter and the settlement token move together or not at all: a
    reservation that cannot be stamped on a job (the reaper deleted it) would
    bill forever with nothing to hand it back."""
    from clipcatalyst_api import db

    month = _month()
    assert db.reserve_usage("f" * 32, "ghost@example.com", month, 2, limit=3) is False
    assert db.get_usage("ghost@example.com", month) == 0
    # Unlimited plans take the same path and get the same protection.
    assert db.reserve_usage("f" * 32, "ghost@example.com", month, 2, limit=None) is False
    assert db.get_usage("ghost@example.com", month) == 0


def test_unlimited_plans_reserve_without_a_ceiling(
    quiet_sandbox: SimpleNamespace,
) -> None:
    """Enterprise has no clips_per_month, so the reservation records the work
    and never refuses it."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    token, user_id = _account(
        client, "unlimited-reserve@example.com", plan="enterprise", status="active"
    )
    month = _month()
    db.add_usage(user_id, month, 100_000)

    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202
    assert db.get_usage(user_id, month) == 100_003
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"] == {
        "limit": None,
        "used": 100_003,
        "month": month,
    }


def test_owner_less_starts_reserve_nothing(quiet_sandbox: SimpleNamespace) -> None:
    """Founder and dev-open jobs bill no one, so they hold no reservation —
    there is nothing for the worker to settle and nobody to charge."""
    from clipcatalyst_api import db

    client = quiet_sandbox.client
    month = _month()

    job_id = _ready_job(client, None, count=3)
    assert _start(client, job_id, None).status_code == 202
    assert db.get_job(job_id)["usage_reserved"] == 0
    assert db.get_usage("", month) == 0

    _set_token(FOUNDER_TOKEN)
    try:
        founder_job = _ready_job(client, FOUNDER_TOKEN, count=3)
        assert _start(client, founder_job, FOUNDER_TOKEN).status_code == 202
        assert db.get_job(founder_job)["usage_reserved"] == 0
    finally:
        _set_token(None)
    assert db.get_usage("", month) == 0


def test_a_failed_job_returns_the_whole_reservation(sandbox: SimpleNamespace) -> None:
    """A job that renders nothing costs nothing. The uploaded "source" here is
    21 bytes of junk, so the real pipeline fails at the probe — the worker's
    failure path has to hand the reservation back or a free account could be
    locked out of its own month by three broken uploads."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "doomed@example.com")
    month = _month()

    job_id = _ready_job(client, token, count=3)
    assert _start(client, job_id, token).status_code == 202  # eager: fails inline
    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "failed", status

    assert db.get_usage(user_id, month) == 0
    assert db.get_job(job_id)["usage_reserved"] == 0
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["used"] == 0
    # The whole month is genuinely available again.
    assert _start(client, _ready_job(client, token, count=3), token).status_code == 202


def test_a_completed_job_settles_down_to_what_it_rendered(
    sandbox: SimpleNamespace, source_video: Path
) -> None:
    """End to end through the real worker: the fixture video plans ONE clip, so
    a three-clip job holds three at /start and hands the rest back when it
    finishes — the counter ends on the clips that exist, not the ones asked
    for."""
    from clipcatalyst_api import db

    client = sandbox.client
    token, user_id = _account(client, "settle-e2e@example.com")
    month = _month()

    created = _create_job(
        client, token, size_bytes=source_video.stat().st_size, count=3, height=960
    )
    job_id = created["job_id"]
    assert (
        client.put(
            f"/v1/uploads/{job_id}",
            content=source_video.read_bytes(),
            headers=_bearer(token),
        ).status_code
        == 200
    )
    assert _start(client, job_id, token).status_code == 202  # eager: renders inline

    status = client.get(f"/v1/jobs/{job_id}", headers=_bearer(token)).json()
    assert status["status"] == "done", f"pipeline failed: {status.get('error')!r}"
    rendered = len(status["clips"])
    assert 1 <= rendered < 3, rendered  # fewer rendered than reserved

    assert db.get_usage(user_id, month) == rendered
    assert db.get_job(job_id)["usage_reserved"] == 0
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"] == {
        "limit": 3,
        "used": rendered,
        "month": month,
    }
