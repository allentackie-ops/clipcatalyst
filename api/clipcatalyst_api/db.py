"""SQLite store for jobs and accounts (WAL, short-lived connections).

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
    "user_id",
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

_USER_COLUMNS = (
    "id",
    "email",
    "password_hash",
    "created_at",
    "stripe_customer_id",
    "plan",
    "plan_status",
    "current_period_end",
)

# Plan fields change ONLY through billing (verified webhooks) or the founder;
# password_hash changes through the auth flows. Email is deliberately not
# updatable — it is the account's identity.
_USER_UPDATABLE = {
    "password_hash",
    "stripe_customer_id",
    "plan",
    "plan_status",
    "current_period_end",
}

_SCHEMA_STATEMENTS = (
    """
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
    clips_json    TEXT NOT NULL DEFAULT '[]',
    user_id       TEXT NOT NULL DEFAULT ''
)
""",
    """
CREATE TABLE IF NOT EXISTS users (
    id                 TEXT PRIMARY KEY,
    email              TEXT NOT NULL UNIQUE,
    password_hash      TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    stripe_customer_id TEXT NOT NULL DEFAULT '',
    plan               TEXT NOT NULL DEFAULT 'free',
    plan_status        TEXT NOT NULL DEFAULT '',
    current_period_end TEXT NOT NULL DEFAULT ''
)
""",
    """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS usage (
    user_id    TEXT NOT NULL,
    month      TEXT NOT NULL,
    clips_used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
)
""",
)


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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create every table if needed and apply guarded ALTERs. Idempotent."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    # jobs.user_id arrived with accounts; upgrade pre-accounts DBs in place.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "user_id" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")


def init_db() -> None:
    """Create the tables if needed. Idempotent and cheap."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)


def create_job(
    job_id: str,
    *,
    filename: str,
    size_bytes: int,
    target_length: int,
    count: int,
    height: int,
    user_id: str = "",
) -> dict:
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO jobs (id, status, stage, progress, detail, error, filename,"
            " size_bytes, target_length, count, height, created_at, updated_at,"
            " clips_json, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                user_id,
            ),
        )
    job = get_job(job_id)
    assert job is not None
    return job


def get_job(job_id: str) -> dict | None:
    """Fetch one job as a plain dict (clips_json parsed into `clips`)."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
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
        _ensure_schema(conn)
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
        _ensure_schema(conn)
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


# --------------------------------------------------------------------------- #
# Accounts: users, sessions, monthly usage.
# --------------------------------------------------------------------------- #


def _user_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _USER_COLUMNS}


def create_user(user_id: str, *, email: str, password_hash: str) -> dict | None:
    """Insert a user; returns the row, or None when the email is taken.

    The UNIQUE(email) constraint is the race-safe duplicate check — callers
    map None to a 409 instead of racing a lookup against the insert.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (user_id, email, password_hash, _now()),
            )
        except sqlite3.IntegrityError:
            return None
    return get_user_by_id(user_id)


def get_user_by_email(email: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return None if row is None else _user_dict(row)


def get_user_by_id(user_id: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else _user_dict(row)


def update_user(user_id: str, **fields: object) -> None:
    """Update whitelisted user columns (plan/billing/password fields)."""
    if not fields:
        return
    unknown = set(fields) - _USER_UPDATABLE
    if unknown:
        raise ValueError(f"update_user: unknown fields {sorted(unknown)!r}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            f"UPDATE users SET {assignments} WHERE id = ?",
            (*fields.values(), user_id),
        )


def create_session(token_hash: str, *, user_id: str, expires_at: str) -> None:
    """Store a session by its sha256 hex — the raw token is never persisted."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (token_hash, user_id, _now(), expires_at),
        )


def get_session_user(token_hash: str) -> dict | None:
    """The user behind an unexpired session, else None (unknown or expired).

    ISO-8601 UTC strings from ``_now()`` compare correctly as text, so the
    expiry check happens in SQL.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, _now()),
        ).fetchone()
    return None if row is None else _user_dict(row)


def revoke_session(token_hash: str) -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def purge_expired_sessions() -> int:
    """Delete sessions past their expiry (startup hygiene); returns the count."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
        return cur.rowcount


def get_user_by_customer(customer_id: str) -> dict | None:
    """The user owning a Stripe customer id (webhook events resolve via this)."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
    return None if row is None else _user_dict(row)


def stripe_event_seen(event_id: str) -> bool:
    """Has this webhook event id already been processed? (replay → no-op)."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    return row is not None


def record_stripe_event(event_id: str) -> None:
    """Mark a webhook event processed. Recorded AFTER its handler succeeds, so
    a crash mid-apply leaves the id unrecorded and Stripe's retry re-runs the
    (absolute, repeat-safe) update instead of being swallowed."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO stripe_events (event_id, processed_at) VALUES (?, ?)",
            (event_id, _now()),
        )


def get_usage(user_id: str, month: str) -> int:
    """Clips rendered by a user in a 'YYYY-MM' month; 0 when no row exists."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT clips_used FROM usage WHERE user_id = ? AND month = ?",
            (user_id, month),
        ).fetchone()
    return 0 if row is None else int(row["clips_used"])


def add_usage(user_id: str, month: str, clips: int) -> None:
    """Atomically add rendered clips to a user's monthly counter (upsert)."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO usage (user_id, month, clips_used) VALUES (?, ?, ?)"
            " ON CONFLICT (user_id, month)"
            " DO UPDATE SET clips_used = clips_used + excluded.clips_used",
            (user_id, month, int(clips)),
        )
