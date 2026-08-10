"""Regression tests for worker task registration and the job reaper.

Two independent concerns:

1. The compose worker boots as `celery -A clipcatalyst_api.queue_app worker`.
   The pipeline task lives in worker.py, which queue_app does NOT import at
   module load — it is pulled in via the Celery `include=[...]` list when the
   worker finalizes. If that wiring regresses, the worker starts with ZERO
   tasks and every job hangs in `queued` forever. We reproduce a cold CLI boot
   and assert the task is actually registered.

2. `reap_expired()` must delete an expired job's row + rendered clips + source
   while leaving a fresh job untouched.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import uuid

import pytest


def _cold_import_queue_app():
    """Import queue_app the way the CLI does: from a clean module state.

    Popping both modules forces the worker.py `@task` decorators to re-run
    against the freshly-created Celery app, so registration only succeeds if
    queue_app really lists worker.py in its `include`.
    """
    for name in ("clipcatalyst_api.worker", "clipcatalyst_api.queue_app"):
        sys.modules.pop(name, None)
    return importlib.import_module("clipcatalyst_api.queue_app")


def test_process_job_task_is_registered() -> None:
    queue_app = _cold_import_queue_app()
    app = queue_app.celery_app

    # This is exactly what the worker bootstep does on startup: import the
    # modules named in conf.imports + conf.include so their tasks register.
    app.loader.import_default_modules()

    # The DOA regression: without include=[...worker], this task is absent.
    assert "clipcatalyst.process_job" in app.tasks
    # The reaper task ships from the same module and must register too.
    assert "clipcatalyst.reap_expired" in app.tasks


def test_time_limits_are_configured() -> None:
    queue_app = _cold_import_queue_app()
    conf = queue_app.celery_app.conf
    assert conf.task_time_limit and conf.task_time_limit > 0
    assert conf.task_soft_time_limit == conf.task_time_limit - 60
    # Visibility timeout must outlast a full job so acks_late doesn't redeliver.
    vt = conf.broker_transport_options["visibility_timeout"]
    assert vt > conf.task_time_limit


@pytest.fixture()
def reaper_settings(tmp_path, monkeypatch):
    """Isolated local-storage settings with a 48 h TTL for the reaper."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CC_DB_PATH", str(data_dir / "jobs.sqlite3"))
    monkeypatch.setenv("CC_STORAGE", "local")
    monkeypatch.setenv("CC_JOB_TTL_HOURS", "48")

    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    try:
        yield settings
    finally:
        get_settings.cache_clear()


def _seed_job(settings, job_id: str) -> None:
    """Create a job row plus its on-disk clips dir and source file."""
    from clipcatalyst_api import db

    db.create_job(
        job_id,
        filename="source.mp4",
        size_bytes=1234,
        target_length=15,
        count=1,
        height=960,
    )
    clip_dir = settings.clips_dir / job_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip-01.mp4").write_bytes(b"stored clip bytes")
    (settings.uploads_dir / f"{job_id}.src").write_bytes(b"uploaded source bytes")


def test_reap_expired_removes_old_keeps_fresh(reaper_settings) -> None:
    from clipcatalyst_api import db
    from clipcatalyst_api.worker import reap_expired

    settings = reaper_settings
    db.init_db()

    old_id = "old-" + uuid.uuid4().hex
    fresh_id = "fresh-" + uuid.uuid4().hex
    _seed_job(settings, old_id)
    _seed_job(settings, fresh_id)

    # Backdate the "old" job well past the 48 h TTL. list_jobs_older_than keys
    # off created_at, so this is the clock we move rather than wall time.
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "UPDATE jobs SET created_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000+00:00", old_id),
    )
    conn.commit()
    conn.close()

    # Sanity: with a far-future cutoff the reaper's query sees the old row.
    older = {j["id"] for j in db.list_jobs_older_than("2999-01-01T00:00:00.000+00:00")}
    assert old_id in older and fresh_id in older

    reaped = reap_expired()

    assert reaped == 1
    # Old job: row, clips dir, and source are all gone.
    assert db.get_job(old_id) is None
    assert not (settings.clips_dir / old_id).exists()
    assert not (settings.uploads_dir / f"{old_id}.src").exists()
    # Fresh job: everything survives.
    assert db.get_job(fresh_id) is not None
    assert (settings.clips_dir / fresh_id / "clip-01.mp4").is_file()
    assert (settings.uploads_dir / f"{fresh_id}.src").is_file()
