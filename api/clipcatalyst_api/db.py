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
    "usage_reserved",
    "usage_month",
)

# `usage_reserved`/`usage_month` are deliberately NOT updatable: the quota a
# job is holding changes only through reserve_usage / settle_usage, which move
# it in the same transaction as the counter it is holding.
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
    user_id       TEXT NOT NULL DEFAULT '',
    usage_reserved INTEGER NOT NULL DEFAULT 0,
    usage_month    TEXT NOT NULL DEFAULT ''
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
    # The quota a running job is holding (see reserve_usage). Older rows carry
    # 0 = "holding nothing", which is exactly how a settled job reads.
    if "usage_reserved" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN usage_reserved INTEGER NOT NULL DEFAULT 0"
        )
    if "usage_month" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN usage_month TEXT NOT NULL DEFAULT ''")


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
    died mid-job never strands a row in a non-terminal state forever — and,
    with it, the monthly quota that job reserved at /start. A process that
    died rendered nothing the owner can use, so the reservation goes back
    here exactly as it would on an ordinary failure.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        stalled = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM jobs"
                " WHERE status IN ('queued', 'processing') AND updated_at < ?",
                (processing_cutoff_iso,),
            )
        ]
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed',"
            " error = 'Processing was interrupted — please try again.',"
            " updated_at = ?"
            " WHERE status IN ('queued', 'processing') AND updated_at < ?",
            (_now(), processing_cutoff_iso),
        )
        failed = cur.rowcount
    # Settlement is once-only, so a job that finished between the two reads
    # keeps the bill its own worker settled.
    for job_id in stalled:
        settle_usage(job_id, 0)
    return failed


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


# One upsert for every unconditional move of the monthly counter (add,
# settle, release). The delta is bound twice — once for the row that may not
# exist yet, once for the one that does — and clamped so it never goes below
# zero on either branch.
_USAGE_DELTA_SQL = (
    "INSERT INTO usage (user_id, month, clips_used) VALUES (?, ?, MAX(0, ?))"
    " ON CONFLICT (user_id, month)"
    " DO UPDATE SET clips_used = MAX(0, usage.clips_used + ?)"
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
    """Atomically move a user's monthly counter by `clips` (upsert).

    Negative deltas are how a reservation is settled or returned, so the
    counter is clamped at zero — no bookkeeping mistake can ever hand somebody
    a negative bill (i.e. free quota).
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(_USAGE_DELTA_SQL, (user_id, month, int(clips), int(clips)))


def reserve_usage(
    job_id: str, user_id: str, month: str, clips: int, *, limit: int | None
) -> bool:
    """Claim `clips` of a user's monthly quota UP FRONT; False = over quota.

    The counter has to move before the work is queued, not after it renders:
    a read-then-render check lets every job that starts before the first one
    finishes read the same pre-render number and each decide it fits, which is
    unbounded on an async queue. Here the ceiling is part of the write —

        clips_used + clips <= limit

    is evaluated by SQLite inside the single UPDATE, so concurrent starts are
    serialized by the database and exactly one of them can take the last slot.
    A refusal leaves the counter untouched.

    `limit=None` (unlimited plans) skips the ceiling entirely but still records
    the reservation, so `used` stays truthful and settlement is symmetric.

    The reservation is stamped on the job row in the SAME transaction as the
    counter, so a counter that moved always carries exactly one settlement
    token to move it back (see settle_usage).
    """
    clips = int(clips)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if limit is not None and clips > limit:
                # A single job bigger than the whole month can never fit, and
                # the upsert's INSERT branch (no row yet) has no ceiling to
                # stop it — this is that branch's guard.
                applied = False
            elif limit is None:
                conn.execute(_USAGE_DELTA_SQL, (user_id, month, clips, clips))
                applied = True
            else:
                cur = conn.execute(
                    "INSERT INTO usage (user_id, month, clips_used) VALUES (?, ?, ?)"
                    " ON CONFLICT (user_id, month)"
                    " DO UPDATE SET clips_used = usage.clips_used + excluded.clips_used"
                    " WHERE usage.clips_used + excluded.clips_used <= ?",
                    (user_id, month, clips, limit),
                )
                applied = cur.rowcount == 1
            if applied:
                stamped = conn.execute(
                    "UPDATE jobs SET usage_reserved = ?, usage_month = ?,"
                    " updated_at = ? WHERE id = ?",
                    (clips, month, _now(), job_id),
                )
                if stamped.rowcount != 1:
                    # The job vanished under us (the reaper). A counter with no
                    # job to settle it would bill forever — take nothing.
                    conn.execute("ROLLBACK")
                    return False
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return applied


def settle_usage(job_id: str, rendered: int) -> int:
    """Settle a job's reservation against what it really rendered, ONCE.

    The owner keeps `rendered` clips of the reservation and gets the rest
    back; `rendered=0` is a full release (a failed job costs nothing). Returns
    the delta applied to the counter.

    The token is cleared in the same transaction that moves the counter, so a
    second call — a Celery retry, a failure path running after a completion,
    a job that never reserved anything (anonymous/founder) — finds nothing to
    settle and is a no-op returning 0. The month is the one the reservation
    was taken in, so a job that starts in one month and finishes in the next
    settles against the ceiling it actually consumed.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT user_id, usage_reserved, usage_month FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            delta = 0
            reserved = 0 if row is None else int(row["usage_reserved"] or 0)
            if reserved and row["user_id"]:
                delta = int(rendered) - reserved
                conn.execute(
                    "UPDATE jobs SET usage_reserved = 0, updated_at = ? WHERE id = ?",
                    (_now(), job_id),
                )
                if delta:
                    conn.execute(
                        _USAGE_DELTA_SQL,
                        (row["user_id"], row["usage_month"], delta, delta),
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return delta
