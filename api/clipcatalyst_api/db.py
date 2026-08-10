"""SQLite job store (WAL, short-lived connections).

Plain sqlite3 with a tiny DAO. Every call opens its own connection so the
module is safe across threads and across the API / worker process boundary.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .settings import get_settings

_COLUMNS = (
    "id",
    "status",
    "stage",
    "progress",
    "detail",
    "error",
    "filename",
    "size_bytes",
    "target_length",
    "count",
    "height",
    "created_at",
    "updated_at",
    "clips_json",
)

_UPDATABLE = {
    "status",
    "stage",
    "progress",
    "detail",
    "error",
    "filename",
    "size_bytes",
    "target_length",
    "count",
    "height",
    "clips_json",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    stage         TEXT NOT NULL DEFAULT '',
    progress      REAL NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',
    error         TEXT,
    filename      TEXT NOT NULL DEFAULT '',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    target_length INTEGER NOT NULL DEFAULT 30,
    count         INTEGER NOT NULL DEFAULT 2,
    height        INTEGER NOT NULL DEFAULT 1920,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    clips_json    TEXT NOT NULL DEFAULT '[]'
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Create the jobs table if needed. Idempotent and cheap."""
    with contextlib.closing(_connect()) as conn:
        conn.execute(_SCHEMA)


def create_job(
    job_id: str,
    *,
    filename: str,
    size_bytes: int,
    target_length: int,
    count: int,
    height: int,
) -> dict:
    now = _now()
    with contextlib.closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT INTO jobs (id, status, stage, progress, detail, error, filename,"
            " size_bytes, target_length, count, height, created_at, updated_at,"
            " clips_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                "awaiting_upload",
                "",
                0.0,
                "Waiting for the video upload",
                None,
                filename,
                int(size_bytes),
                int(target_length),
                int(count),
                int(height),
                now,
                now,
                "[]",
            ),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def get_job(job_id: str) -> dict | None:
    """Fetch one job as a plain dict (clips_json parsed into `clips`)."""
    with contextlib.closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = {key: row[key] for key in _COLUMNS}
    try:
        job["clips"] = json.loads(job.get("clips_json") or "[]")
    except json.JSONDecodeError:
        job["clips"] = []
    return job


def update_job(job_id: str, **fields: object) -> None:
    """Update whitelisted columns; always bumps updated_at."""
    if not fields:
        return
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"update_job: unknown fields {sorted(unknown)!r}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
            (*values, _now(), job_id),
        )


def set_clips(job_id: str, clips: list[dict]) -> None:
    update_job(job_id, clips_json=json.dumps(clips, ensure_ascii=False))


def transition_status(job_id: str, *, expect: str, to: str, **fields: object) -> bool:
    """Atomically move a job from `expect` to `to`, guarding races.

    Returns True only if this call performed the transition (the row was in
    `expect`). Concurrent duplicate callers get False and must not proceed.
    """
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"transition_status: unknown fields {sorted(unknown)!r}")
    extra = "".join(f", {name} = ?" for name in fields)
    with contextlib.closing(_connect()) as conn:
        cur = conn.execute(
            f"UPDATE jobs SET status = ?{extra}, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (to, *fields.values(), _now(), job_id, expect),
        )
        return cur.rowcount == 1


def list_jobs_older_than(cutoff_iso: str) -> list[dict]:
    """Jobs created strictly before an ISO-8601 cutoff (for the reaper)."""
    with contextlib.closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        rows = conn.execute(
            "SELECT id, status, created_at FROM jobs WHERE created_at < ?",
            (cutoff_iso,),
        ).fetchall()
    return [{"id": r["id"], "status": r["status"], "created_at": r["created_at"]} for r in rows]


def reconcile_stalled(processing_cutoff_iso: str) -> int:
    """Fail jobs stuck in processing/queued past a cutoff (crash recovery).

    Returns the number of rows failed. Called on API startup so a worker that
    died mid-job never strands a row in a non-terminal state forever.
    """
    with contextlib.closing(_connect()) as conn:
        conn.execute(_SCHEMA)
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed',"
            " error = 'Processing was interrupted — please try again.',"
            " updated_at = ?"
            " WHERE status IN ('queued', 'processing') AND updated_at < ?",
            (_now(), processing_cutoff_iso),
        )
        return cur.rowcount


def delete_job(job_id: str) -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
