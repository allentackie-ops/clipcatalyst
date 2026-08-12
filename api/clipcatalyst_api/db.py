"""SQLite store for jobs, accounts and the clip library (WAL, short conns).

Plain sqlite3 with a tiny DAO. Every call opens its own connection so the
module is safe across threads and across the API / worker process boundary.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
#
# `status` is NOT here either, and that is the point: a blind
# `update_job(status=...)` can overwrite a row somebody else already moved to a
# terminal state. That is exactly how a worker used to resurrect a job the
# reconciler had already failed AND REFUNDED — flipping it back to `done` and
# handing over clips against a reservation that no longer existed. Every status
# write therefore goes through transition_status / finalize_job, which name the
# status they expect to be replacing and report whether they actually won.
_UPDATABLE = {
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

#: One saved clip (LIBRARY.md Part 2). Deliberately NOT the jobs row: a job is
#: pipeline state and gets reaped at CC_JOB_TTL_HOURS, while the library
#: outlives it — the metadata here is permanent, and only the FILE expires.
_CLIP_COLUMNS = (
    "id",
    "user_id",
    "job_id",
    "clip_index",
    "title",
    "score",
    "hooks",
    "reason",
    "tip",
    "start",
    "end",
    "duration",
    "width",
    "height",
    "speaker_count",
    "words",
    "engine",
    "file_path",
    "bytes",
    "created_at",
    "expires_at",
)

#: One connected publishing account (PUBLISH.md Part 2). The `*_enc` pair is
#: ciphertext at every layer above this one: nothing in db.py can read it,
#: nothing in models.py can carry it, and no route returns it.
_CONNECTION_COLUMNS = (
    "id",
    "user_id",
    "platform",
    "account_name",
    "account_id",
    "access_token_enc",
    "refresh_token_enc",
    "expires_at",
    "scopes",
    "created_at",
    "updated_at",
)

# What a stored connection may have rewritten after the fact: its tokens, their
# deadline, the granted scopes, and the channel's display name.
#
# `user_id`, `platform` and `account_id` are deliberately absent — together they
# ARE the connection's identity (the UNIQUE index is built on them), so an
# update that could move any of them would be able to re-point one account's
# stored tokens at another account's row.
_CONNECTION_UPDATABLE = {
    "account_name",
    "access_token_enc",
    "refresh_token_enc",
    "expires_at",
    "scopes",
}

_OAUTH_STATE_COLUMNS = (
    "state_hash",
    "user_id",
    "session_hash",
    "platform",
    "verifier_enc",
    "redirect_uri",
    "created_at",
    "expires_at",
)

#: One attempt to post one clip to one connected channel (PUBLISH.md Part 3).
#: Deliberately the same SHAPE as a jobs row — a status somebody polls, a
#: fraction, a line of prose and a user-facing error — because it is the same
#: kind of thing: work handed to the queue that a browser watches.
#:
#: What it does NOT carry is any secret. The access token comes from
#: connections.py at upload time and the resumable session URL (which is itself
#: a bearer capability for the channel) lives only in the running task, so a
#: publish row is safe to read, log and hand back.
_PUBLISH_COLUMNS = (
    "id",
    "user_id",
    "clip_id",
    "connection_id",
    "platform",
    "status",
    "progress",
    "detail",
    "error",
    "title",
    "description",
    "privacy",
    "video_id",
    "video_url",
    "created_at",
    "updated_at",
)

# Progress bookkeeping and the result, and nothing else. `status` is absent for
# exactly the reason it is absent from _UPDATABLE: every status write names the
# status it expects to replace (transition_publish_status), so a task that has
# lost its row cannot overwrite a terminal state somebody else already wrote.
# The metadata columns are absent too — what was asked for is what was asked
# for, and a row that rewrote its own title would make the record of a post
# disagree with the post.
_PUBLISH_UPDATABLE = {
    "progress",
    "detail",
    "error",
    "video_id",
    "video_url",
}

#: The non-terminal publish statuses: the only ones a worker may claim, and the
#: only ones the stall sweep may fail. `done` and `failed` are terminal.
LIVE_PUBLISH_STATUSES = ("queued", "uploading")

_PUBLISH_INTERRUPTED_ERROR = (
    "The upload stopped before it finished. Please try posting this clip again."
)

_USER_COLUMNS = (
    "id",
    "email",
    "password_hash",
    "google_sub",
    "created_at",
    "stripe_customer_id",
    "plan",
    "plan_status",
    "current_period_end",
    "brand_logo_path",
    "brand_caption_color",
    "brand_show_logo",
)

# Plan fields change ONLY through billing (verified webhooks) or the founder;
# password_hash changes through the auth flows. Email is deliberately not
# updatable — it is the account's identity.
#
# The brand_* fields are the one group a user writes directly (PUT/DELETE
# /v1/me/brand). They carry no entitlement: what the plan allows is re-read at
# render time from the plan itself, so a stored kit can never grant anything.
#
# `google_sub` is deliberately absent too — it is a second identity for the
# account, as load-bearing as `email`. It moves only through link_google_sub,
# which fills an EMPTY sub and nothing else, so no code path can silently
# re-point an existing account at a different Google identity.
_USER_UPDATABLE = {
    "password_hash",
    "stripe_customer_id",
    "plan",
    "plan_status",
    "current_period_end",
    "brand_logo_path",
    "brand_caption_color",
    "brand_show_logo",
}

# The non-terminal statuses a job passes through once it has been started: the
# only ones a worker may claim, and the only ones the stall reconciler may fail.
# `done` and `failed` are terminal — nothing moves a job out of them.
LIVE_STATUSES = ("queued", "processing")

_INTERRUPTED_ERROR = "Processing was interrupted. Please try again."

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
    id                  TEXT PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    google_sub          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    stripe_customer_id  TEXT NOT NULL DEFAULT '',
    plan                TEXT NOT NULL DEFAULT 'free',
    plan_status         TEXT NOT NULL DEFAULT '',
    current_period_end  TEXT NOT NULL DEFAULT '',
    brand_logo_path     TEXT NOT NULL DEFAULT '',
    brand_caption_color TEXT NOT NULL DEFAULT '',
    brand_show_logo     INTEGER NOT NULL DEFAULT 1
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
    # Email sign-in codes (EMAILAUTH.md). The ADDRESS is the primary key, which
    # is the storage rule doing the work: one live code per address, so a
    # second `start` REPLACES the row and the first code stops working there
    # and then. `code_hash` is a sha256 (auth.hash_login_code) — the six digits
    # themselves are never written here, to a log, or to any response body.
    # `attempts` is what makes a small secret safe (auth.LOGIN_CODE_MAX_
    # ATTEMPTS); rows go on success, at the cap, and on the hourly sweep.
    """
CREATE TABLE IF NOT EXISTS login_codes (
    email      TEXT PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
""",
    # The clip library. `file_path` is relative to the storage backend's
    # library root (local: settings.library_dir; s3: the bucket key) and goes
    # back to '' when the reaper deletes the file — the row itself stays, which
    # is the whole point: a clip's title, score, hooks and transcript outlive
    # its video. `end` is quoted because it is a SQL keyword.
    """
CREATE TABLE IF NOT EXISTS clips (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    job_id        TEXT NOT NULL DEFAULT '',
    clip_index    INTEGER NOT NULL DEFAULT 0,
    title         TEXT NOT NULL DEFAULT '',
    score         INTEGER NOT NULL DEFAULT 0,
    hooks         TEXT NOT NULL DEFAULT '[]',
    reason        TEXT NOT NULL DEFAULT '',
    tip           TEXT NOT NULL DEFAULT '',
    start         REAL NOT NULL DEFAULT 0,
    "end"         REAL NOT NULL DEFAULT 0,
    duration      REAL NOT NULL DEFAULT 0,
    width         INTEGER NOT NULL DEFAULT 0,
    height        INTEGER NOT NULL DEFAULT 0,
    speaker_count INTEGER NOT NULL DEFAULT 0,
    words         TEXT NOT NULL DEFAULT '',
    engine        TEXT NOT NULL DEFAULT 'cloud',
    file_path     TEXT NOT NULL DEFAULT '',
    bytes         INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL DEFAULT ''
)
""",
    # A connected publishing account (PUBLISH.md Part 2). The two `*_enc`
    # columns are Fernet CIPHERTEXT and nothing else ever goes in them — see
    # connections.py, which is the only module that can read them. `expires_at`
    # is the ACCESS token's deadline (the refresh token has none), and
    # `account_id` is the provider's own id for the channel, which is what
    # makes reconnecting the same channel an update rather than a duplicate.
    """
CREATE TABLE IF NOT EXISTS connections (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,
    account_name      TEXT NOT NULL DEFAULT '',
    account_id        TEXT NOT NULL DEFAULT '',
    access_token_enc  TEXT NOT NULL DEFAULT '',
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    expires_at        TEXT NOT NULL DEFAULT '',
    scopes            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)
""",
    # One account connects one channel once: reconnecting the same channel
    # REPLACES its tokens (see upsert_connection) instead of leaving a second
    # row nobody can tell apart, and two simultaneous callbacks race here
    # rather than both inserting.
    """
CREATE UNIQUE INDEX IF NOT EXISTS connections_identity
    ON connections (user_id, platform, account_id)
""",
    # The in-flight half of an OAuth authorization (PUBLISH.md Part 2). One row
    # per `start`, spent by the matching callback and gone either way.
    #
    # `state_hash` is the sha256 of the state's nonce, exactly as sessions and
    # sign-in codes are stored: the value that travels through the browser and
    # the provider is never the value on disk. `verifier_enc` is the PKCE
    # verifier, encrypted with the same key the tokens are — it is the secret
    # that turns an intercepted authorization code into tokens, so it is not
    # kept in plaintext either. `session_hash` is what "bound to the session"
    # means here: the callback arrives as a bare browser redirect with no
    # Authorization header, so the account a connection lands on comes from
    # THIS row, and the flow is refused if that session has since ended.
    """
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    platform     TEXT NOT NULL,
    verifier_enc TEXT NOT NULL,
    redirect_uri TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
)
""",
    # One publish attempt (PUBLISH.md Part 3). It points at a clip and at a
    # connection rather than copying either: the clip's file may expire between
    # the request and the upload (which is a refusal, not a 500), and the
    # connection's tokens may be refreshed or revoked in the same window, so
    # both are re-read when the task actually runs.
    #
    # `title`, `description` and `privacy` ARE copied, because they are what the
    # user asked for at the moment they asked — the record of a post must not
    # change under it if the clip is later renamed.
    """
CREATE TABLE IF NOT EXISTS publish_jobs (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    clip_id       TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    platform      TEXT NOT NULL,
    status        TEXT NOT NULL,
    progress      REAL NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL DEFAULT '',
    error         TEXT,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    privacy       TEXT NOT NULL DEFAULT 'private',
    video_id      TEXT NOT NULL DEFAULT '',
    video_url     TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
""",
    # The library list is always "this user's clips, newest first" — the index
    # is that query.
    """
CREATE INDEX IF NOT EXISTS clips_user_created
    ON clips (user_id, created_at DESC)
""",
    # "Is this clip already on its way to that platform?" — the guard that keeps
    # a double-tapped Post button from spending two of a very small daily upload
    # quota on one video (see create_publish_job).
    """
CREATE INDEX IF NOT EXISTS publish_jobs_clip
    ON publish_jobs (clip_id, platform)
""",
    # The reaper's query is "every clip whose file has outlived its window",
    # across all users; '' means never and sorts before every real timestamp,
    # so the partial index keeps the unlimited rows out of it entirely.
    """
CREATE INDEX IF NOT EXISTS clips_expiring
    ON clips (expires_at) WHERE file_path != '' AND expires_at != ''
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
    # The brand kit arrived after accounts shipped; upgrade those DBs in place
    # too. Defaults describe an account that has never opened the panel: no
    # logo, no colour, and showLogo on — the same EMPTY_KIT the browser starts
    # from, so a row that predates the feature reads as "unbranded" rather than
    # as "logo deliberately hidden".
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "brand_logo_path" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN brand_logo_path TEXT NOT NULL DEFAULT ''"
        )
    if "brand_caption_color" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN brand_caption_color TEXT NOT NULL DEFAULT ''"
        )
    if "brand_show_logo" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN brand_show_logo INTEGER NOT NULL DEFAULT 1"
        )
    # Sign in with Google (LIBRARY.md Part 1). '' means "no Google identity",
    # which is what every account that predates this column is.
    if "google_sub" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT NOT NULL DEFAULT ''")
    # One Google identity belongs to at most ONE account — the index is what
    # makes that true under concurrency, rather than a lookup racing an insert.
    # It is PARTIAL because '' is the "no Google identity" value and every
    # password-only row carries it: a plain UNIQUE index would let exactly one
    # such account exist. Created here rather than in _SCHEMA_STATEMENTS
    # because on an upgraded DB the column only exists after the ALTER above.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_google_sub_unique"
        " ON users (google_sub) WHERE google_sub != ''"
    )


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
    """Update whitelisted columns; always bumps updated_at.

    Progress bookkeeping only — `status` is not writable here (see _UPDATABLE).
    """
    if not fields:
        return
    if "status" in fields:
        raise ValueError(
            "update_job: status is not updatable. Move it with transition_status"
            " or finalize_job, which name the status they expect to replace"
        )
    _check_fields("update_job", fields)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values())
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
            (*values, _now(), job_id),
        )


def encode_clips(clips: list[dict]) -> str:
    """The `clips_json` column value for `clips`.

    Exposed so a caller that must publish the clips in the SAME statement as
    the terminal status (finalize_job) encodes them exactly like set_clips.
    """
    return json.dumps(clips, ensure_ascii=False)


def set_clips(job_id: str, clips: list[dict]) -> None:
    update_job(job_id, clips_json=encode_clips(clips))


def _expected(expect: str | tuple[str, ...]) -> tuple[str, ...]:
    return (expect,) if isinstance(expect, str) else tuple(expect)


def _check_fields(caller: str, fields: dict[str, object]) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"{caller}: unknown fields {sorted(unknown)!r}")


def _transition_locked(
    conn: sqlite3.Connection,
    job_id: str,
    expected: tuple[str, ...],
    to: str,
    fields: dict[str, object],
    stale_before: str | None,
) -> bool:
    """The compare-and-swap every status write is built on. True = we won.

    The WHERE clause carries the caller's whole precondition — the statuses it
    expects, and optionally the staleness that made the row a candidate — so
    SQLite, not the caller's earlier read, decides who moves the row.
    """
    extra = "".join(f", {name} = ?" for name in fields)
    placeholders = ", ".join("?" for _ in expected)
    sql = (
        f"UPDATE jobs SET status = ?{extra}, updated_at = ?"
        f" WHERE id = ? AND status IN ({placeholders})"
    )
    params: list[object] = [to, *fields.values(), _now(), job_id, *expected]
    if stale_before is not None:
        sql += " AND updated_at < ?"
        params.append(stale_before)
    return conn.execute(sql, params).rowcount == 1


def transition_status(
    job_id: str, *, expect: str | tuple[str, ...], to: str, **fields: object
) -> bool:
    """Atomically move a job from `expect` to `to`, guarding races.

    Returns True only if this call performed the transition (the row was in
    `expect`). Concurrent duplicate callers get False and must not proceed.
    `expect` may name several acceptable current statuses.

    A transition that must ALSO settle the job's quota reservation — i.e. any
    move to a terminal status — belongs in finalize_job, which does both in one
    transaction instead of leaving a window between them.
    """
    _check_fields("transition_status", fields)
    with contextlib.closing(_connect()) as conn:
        return _transition_locked(conn, job_id, _expected(expect), to, fields, None)


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

    Returns the number of rows failed. Called on API startup AND from the
    hourly beat sweep (worker.reconcile_stalled), so a worker that died mid-job
    never strands a row in a non-terminal state forever — and, with it, the
    monthly quota that job reserved at /start. A process that died rendered
    nothing the owner can use, so the reservation goes back here exactly as it
    would on an ordinary failure.

    The SELECT below only nominates candidates; the authority is the
    compare-and-swap inside finalize_job, which re-checks BOTH halves of what
    made a row stalled — still non-terminal, still untouched since the cutoff —
    and settles in the same transaction as the status write. So a job whose
    worker is alive (a progress write moves updated_at) or which has just
    finished (a terminal status) is left alone rather than failed and refunded
    out from under it, and the refund can never happen without the failure.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        candidates = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM jobs"
                " WHERE status IN ('queued', 'processing') AND updated_at < ?",
                (processing_cutoff_iso,),
            )
        ]
    return sum(
        finalize_job(
            job_id,
            expect=LIVE_STATUSES,
            to="failed",
            rendered=0,
            stale_before=processing_cutoff_iso,
            error=_INTERRUPTED_ERROR,
        )
        for job_id in candidates
    )


def delete_job(job_id: str) -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# --------------------------------------------------------------------------- #
# Accounts: users, sessions, monthly usage.
# --------------------------------------------------------------------------- #


def _user_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _USER_COLUMNS}


def create_user(
    user_id: str, *, email: str, password_hash: str, google_sub: str = ""
) -> dict | None:
    """Insert a user; returns the row, or None when email/google_sub is taken.

    The UNIQUE(email) constraint is the race-safe duplicate check — callers
    map None to a 409 instead of racing a lookup against the insert. The
    partial UNIQUE index on google_sub does the same for the Google identity,
    so two simultaneous first-time Google sign-ins cannot fork one person into
    two accounts.

    `password_hash=''` creates a PASSWORD-LESS account (Sign in with Google,
    LIBRARY.md Part 1). Nothing hashes to the empty string, and the password
    login path refuses it outright rather than comparing against it.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, google_sub, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (user_id, email, password_hash, google_sub, _now()),
            )
        except sqlite3.IntegrityError:
            return None
    return get_user_by_id(user_id)


def get_user_by_email(email: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return None if row is None else _user_dict(row)


def get_user_by_google_sub(google_sub: str) -> dict | None:
    """The account a Google identity signs into, else None.

    An empty sub matches NOBODY, and the guard lives here rather than in each
    caller: '' is the value every password-only row carries, so a query built
    from an absent claim would otherwise hand back somebody else's account.
    """
    if not google_sub:
        return None
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM users WHERE google_sub = ?", (google_sub,)
        ).fetchone()
    return None if row is None else _user_dict(row)


def link_google_sub(user_id: str, google_sub: str) -> bool:
    """Attach a Google identity to an account that has none. True = we linked.

    Guarded twice, and both guards matter. `google_sub = ''` in the WHERE
    clause means a link only ever FILLS an empty slot — an account already
    signed in with one Google identity is never re-pointed at another by a
    later request. The partial UNIQUE index means the same identity cannot be
    attached to a second account, so two concurrent links race in SQLite and
    exactly one wins; the loser is told so instead of silently forking.
    """
    if not google_sub:
        return False
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        try:
            cur = conn.execute(
                "UPDATE users SET google_sub = ? WHERE id = ? AND google_sub = ''",
                (google_sub, user_id),
            )
        except sqlite3.IntegrityError:
            return False
    return cur.rowcount == 1


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


# --------------------------------------------------------------------------- #
# Email sign-in codes (EMAILAUTH.md). One live code per address, stored only as
# a hash, spent exactly once. Every function here is a state MOVE — read,
# guess, spend, expire — and each is a single statement or a single guarded
# transaction, because the safety of a 6-digit secret is entirely in how
# carefully its row is accounted for.
# --------------------------------------------------------------------------- #

_LOGIN_CODE_COLUMNS = ("email", "code_hash", "expires_at", "attempts", "created_at")


def _login_code_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _LOGIN_CODE_COLUMNS}


def put_login_code(email: str, *, code_hash: str, expires_at: str) -> None:
    """Store THE live code for an address, replacing whatever was there.

    The upsert is the "a second start invalidates the first code" rule: one
    row per address, and `attempts` goes back to 0 with the new code because
    the guesses spent against the old one were spent against a different
    secret. (Carrying them over would let anyone disable an account's code
    sign-in by burning five guesses and never asking for a code again.)
    """
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO login_codes (email, code_hash, expires_at, attempts,"
            " created_at) VALUES (?, ?, ?, 0, ?)"
            " ON CONFLICT (email) DO UPDATE SET code_hash = excluded.code_hash,"
            " expires_at = excluded.expires_at, attempts = 0,"
            " created_at = excluded.created_at",
            (email, code_hash, expires_at, now),
        )


def get_login_code(email: str) -> dict | None:
    """The LIVE code row for an address, else None — never a code itself.

    Expiry is part of the query, exactly as it is for sessions
    (get_session_user): ISO-8601 UTC strings from ``_now()`` compare correctly
    as text, so a code past its TTL simply is not found. That is what makes
    the TTL enforced rather than swept — the hourly purge only tidies rows
    that already stopped working, and a sweep that never ran could not hand
    anybody a stale code.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM login_codes WHERE email = ? AND expires_at > ?",
            (email, _now()),
        ).fetchone()
    return None if row is None else _login_code_dict(row)


def count_login_code_attempt(email: str, *, max_attempts: int) -> int:
    """Charge one wrong guess to an address's code; the running count.

    Returns 0 when there was no row to charge. At `max_attempts` the row is
    DELETED in the same transaction as the increment that reached it, so the
    cap cannot be walked past by two guesses arriving together: the count and
    the deletion are one decision, not a read followed by a hopeful write.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?",
                (email,),
            )
            if cur.rowcount != 1:
                conn.execute("COMMIT")
                return 0
            row = conn.execute(
                "SELECT attempts FROM login_codes WHERE email = ?", (email,)
            ).fetchone()
            attempts = 0 if row is None else int(row["attempts"])
            if attempts >= max_attempts:
                conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return attempts


def consume_login_code(email: str, code_hash: str) -> bool:
    """Spend the code, once. True = this caller is the one that spent it.

    The DELETE carries the hash it expects, so the row is removed only if it
    is still the same code this caller verified — and `rowcount` is what says
    whether we won. That is the single-use guarantee under concurrency: two
    requests carrying the same correct code race here, SQLite serializes them,
    and the loser is refused rather than both being signed in off one secret.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM login_codes WHERE email = ? AND code_hash = ?",
            (email, code_hash),
        )
    return cur.rowcount == 1


def delete_login_code(email: str) -> None:
    """Drop an address's live code (expired, or a send that never went out)."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))


def purge_expired_login_codes(now_iso: str) -> int:
    """Delete codes past their expiry; returns the count (hourly sweep).

    Hygiene, never security: an expired row is already refused at verify (the
    TTL is checked against the row, not against whether a sweep has run), so
    this exists so the table does not accumulate dead secrets — not so that
    they stop working.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute("DELETE FROM login_codes WHERE expires_at <= ?", (now_iso,))
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


def _settle_locked(conn: sqlite3.Connection, job_id: str, rendered: int) -> int:
    """Settle inside an already-open transaction. See settle_usage."""
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
                _USAGE_DELTA_SQL, (row["user_id"], row["usage_month"], delta, delta)
            )
    return delta


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
            delta = _settle_locked(conn, job_id, rendered)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return delta


def finalize_job(
    job_id: str,
    *,
    expect: str | tuple[str, ...],
    to: str,
    rendered: int,
    stale_before: str | None = None,
    **fields: object,
) -> bool:
    """Move a job to a TERMINAL status and settle its reservation, together.

    One BEGIN IMMEDIATE covers both halves, so the pair is indivisible: the
    caller that wins the status guard is the one — and the only one — that
    settles. Returns True if this call moved the row; False means somebody else
    already finished the job, and the caller owns nothing: not the quota, not
    the right to publish output, not the row.

    That is what makes settle-exactly-once structural rather than a matter of
    who happened to write first. The two racers are a worker completing a job
    and the stall reconciler failing it, and BOTH used to settle: whichever
    settled first cleared the token, and the loser silently no-op'd — so a
    reconciled job could be flipped back to `done` and delivered for free, and
    a job completing under the reaper's nose could be refunded after billing.
    Now the loser knows it lost.

    `rendered` is what the owner actually got (0 releases the whole
    reservation); `stale_before` adds "and the row has not been touched since"
    to the guard, which is how reconciliation avoids failing a live worker.
    """
    _check_fields("finalize_job", fields)
    expected = _expected(expect)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            won = _transition_locked(conn, job_id, expected, to, fields, stale_before)
            if won:
                _settle_locked(conn, job_id, rendered)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return won


# --------------------------------------------------------------------------- #
# The clip library (LIBRARY.md Part 2).
#
# Two lifetimes in one row, and keeping them apart is the entire design: the
# METADATA is permanent — it is deleted only when the owner deletes the clip or
# the account goes — while the FILE lives until `expires_at`, after which the
# reaper unlinks it and blanks `file_path`. Nothing here deletes a row on a
# timer, and nothing here is derived from the jobs table: a job is reaped after
# 48 h and its library rows carry on without it.
# --------------------------------------------------------------------------- #


def clip_expires_at(created_at: str, retention_days: int | None) -> str:
    """When a clip's FILE expires: ``created_at + retention_days``, or ''.

    '' means never (the enterprise window, ``retention_days=None``) and is the
    value every comparison here treats as the LATEST possible expiry — never as
    "expired long ago", which is what an empty string sorts like.

    An unparseable ``created_at`` cannot produce an honest deadline, so it
    yields '' as well: keeping a file we cannot date is a storage bill, while
    deleting it on a guess is somebody's clip.
    """
    if retention_days is None:
        return ""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    expires = created.astimezone(timezone.utc) + timedelta(days=int(retention_days))
    return expires.isoformat(timespec="milliseconds")


def _clip_dict(row: sqlite3.Row) -> dict:
    """One clips row as a plain dict, with the JSON columns parsed.

    `hooks` and `words` are stored as JSON text. A row whose JSON is somehow
    unreadable degrades to an empty list rather than raising: the library must
    still list, and a card with no hooks beats an account page that 500s.
    """
    clip = {key: row[key] for key in _CLIP_COLUMNS}
    for key in ("hooks", "words"):
        raw = clip.get(key) or ""
        try:
            value = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            value = []
        clip[key] = value if isinstance(value, list) else []
    return clip


def create_clip(
    clip_id: str,
    *,
    user_id: str,
    engine: str,
    clip_index: int = 0,
    job_id: str = "",
    title: str = "",
    score: int = 0,
    hooks: list | None = None,
    reason: str = "",
    tip: str = "",
    start: float = 0.0,
    end: float = 0.0,
    duration: float = 0.0,
    width: int = 0,
    height: int = 0,
    speaker_count: int = 0,
    words: list | None = None,
    file_path: str = "",
    size_bytes: int = 0,
    retention_days: int | None = None,
) -> dict:
    """Insert one library row and return it.

    ``retention_days`` comes from the owner's plan AT THIS MOMENT and is turned
    into a concrete ``expires_at`` here, rather than being stored and re-read
    later: a plan change must be able to EXTEND an existing clip (see
    extend_clip_expiry) without ever being able to shorten one, and a stored
    window would silently do both the moment somebody downgraded.
    """
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO clips (id, user_id, job_id, clip_index, title, score,"
            " hooks, reason, tip, start, \"end\", duration, width, height,"
            " speaker_count, words, engine, file_path, bytes, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?)",
            (
                clip_id,
                user_id,
                job_id,
                int(clip_index),
                title,
                int(score),
                json.dumps(list(hooks or []), ensure_ascii=False),
                reason,
                tip,
                float(start),
                float(end),
                float(duration),
                int(width),
                int(height),
                int(speaker_count),
                json.dumps(list(words or []), ensure_ascii=False),
                engine,
                file_path,
                int(size_bytes),
                now,
                clip_expires_at(now, retention_days),
            ),
        )
    clip = get_clip(clip_id)
    assert clip is not None
    return clip


def get_clip(clip_id: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return None if row is None else _clip_dict(row)


def get_clip_by_file(user_id: str, file_path: str) -> dict | None:
    """One account's clip stored at ``file_path``, else None.

    Scoped to the owner on purpose: this is what serves a link built from a
    job id and a clip name after the job row itself has been reaped, so it must
    never be able to answer with somebody else's row.
    """
    if not user_id or not file_path:
        return None
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM clips WHERE user_id = ? AND file_path = ?",
            (user_id, file_path),
        ).fetchone()
    return None if row is None else _clip_dict(row)


def clip_file_referenced(file_path: str) -> bool:
    """Does a live library row still point at this stored file?

    The question every cleanup path must ask before it deletes anything under
    the library root. Library files live outside ``clips_dir`` precisely so the
    job sweeps cannot reach them, and this is the second lock on the same door.
    """
    if not file_path:
        return False
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT 1 FROM clips WHERE file_path = ? LIMIT 1", (file_path,)
        ).fetchone()
    return row is not None


def list_clips(
    user_id: str, *, limit: int = 20, before: str = "", before_id: str = ""
) -> list[dict]:
    """One account's clips, newest first, cursor-paginated on ``created_at``.

    The cursor is the last item's ``created_at``, with its ``id`` as a
    tiebreak: clips rendered by one job are written milliseconds apart and can
    share a timestamp, and a page boundary landing inside such a group would
    silently drop the rest of it. Ordering and cursor use the same
    (created_at, id) pair, so every row appears on exactly one page.

    ``limit`` is clamped to 50 HERE rather than in the route, so no caller —
    route, worker, or future script — can ask the database for more.
    """
    count = max(1, min(int(limit), 50))
    sql = "SELECT * FROM clips WHERE user_id = ?"
    params: list[object] = [user_id]
    if before:
        if before_id:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params += [before, before, before_id]
        else:
            sql += " AND created_at < ?"
            params.append(before)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(count)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        rows = conn.execute(sql, params).fetchall()
    return [_clip_dict(row) for row in rows]


def library_bytes(user_id: str) -> int:
    """Bytes of stored clip FILES this account is currently holding.

    Rows whose file has been reaped weigh nothing — the ceiling is about disk,
    and their disk is already back. `bytes` stays on the row as metadata.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM clips"
            " WHERE user_id = ? AND file_path != ''",
            (user_id,),
        ).fetchone()
    return 0 if row is None else int(row["total"] or 0)


def list_expired_clips(now_iso: str) -> list[dict]:
    """Clips whose FILE has outlived its window (the library reaper's query).

    '' expires_at is unlimited and excluded by the WHERE clause rather than by
    a comparison — as text it sorts before every real timestamp, so an
    `expires_at <= now` alone would reap exactly the clips that must never be
    reaped.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, user_id, file_path, expires_at FROM clips"
            " WHERE file_path != '' AND expires_at != '' AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "file_path": r["file_path"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]


def clear_clip_file(clip_id: str) -> bool:
    """Blank a clip's ``file_path`` — the file is gone, the row is not.

    True when this call did it. Guarded on the path still being set so a repeat
    sweep is a no-op rather than a second (harmless but lying) write.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "UPDATE clips SET file_path = '' WHERE id = ? AND file_path != ''",
            (clip_id,),
        )
    return cur.rowcount == 1


def delete_clip(clip_id: str) -> bool:
    """Remove a library row outright. True when a row was actually removed.

    The ONLY thing that deletes a row (the owner asking, via
    ``DELETE /v1/clips/{id}``). Nothing on a timer calls this.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    return cur.rowcount == 1


def extend_clip_expiry(user_id: str, retention_days: int | None) -> int:
    """Re-stamp an account's clips with a retention window. EXTENDS ONLY.

    Returns how many rows moved. Called after a plan change (billing.py), and
    the asymmetry is the decision LIBRARY.md makes explicitly:

    * an upgrade recomputes every non-expired clip from its own ``created_at``
      and keeps the LATER of the two deadlines, so a creator who upgrades today
      keeps what they already made for the longer window;
    * a downgrade computes an earlier deadline, which loses the comparison and
      writes nothing. Taking away something already made is a support ticket,
      and the storage is already spent. New clips get the new plan's window.

    Already-expired clips are skipped: their file is gone, so a longer window
    would restore nothing and would only make ``available: false`` rows claim a
    future they no longer have. '' (unlimited) is the latest possible value on
    both sides of the comparison — it never loses and is never overwritten.
    """
    now = _now()
    changed = 0
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, created_at, expires_at FROM clips WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for row in rows:
            current = str(row["expires_at"] or "")
            if not current or current <= now:
                continue  # already unlimited, or already reaped
            fresh = clip_expires_at(str(row["created_at"]), retention_days)
            if fresh and fresh <= current:
                continue  # the new plan is shorter — leave the clip alone
            conn.execute(
                "UPDATE clips SET expires_at = ? WHERE id = ?", (fresh, row["id"])
            )
            changed += 1
    return changed


# --------------------------------------------------------------------------- #
# Publishing connections (PUBLISH.md Part 2).
#
# Every function here moves ciphertext it cannot read. That is not a limitation
# to work around: db.py has no key, connections.py has the only one, and the
# split is what makes "a token is never in the database in plaintext" a shape
# of the code rather than a habit somebody has to keep.
# --------------------------------------------------------------------------- #


def _connection_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _CONNECTION_COLUMNS}


def upsert_connection(
    connection_id: str,
    *,
    user_id: str,
    platform: str,
    account_name: str,
    account_id: str,
    access_token_enc: str,
    refresh_token_enc: str,
    expires_at: str,
    scopes: str,
) -> dict:
    """Store a connected account, replacing this account's tokens for it.

    Keyed on (user_id, platform, account_id), so reconnecting the SAME channel
    is an update — new tokens on the existing row, same id, `created_at`
    preserved — and connecting a second channel on the same platform is a new
    row. Two callbacks racing for one channel meet the UNIQUE index and one of
    them takes the update path instead of both inserting.

    The refresh token is the one field a re-connect may NOT blank. Providers
    hand one out on the first authorization and often not again (Google only
    re-issues under ``prompt=consent``), so writing an empty value over a
    stored one would silently turn a working connection into one that dies at
    the next access-token expiry — with the user having just done the thing
    that was supposed to fix it.
    """
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO connections (id, user_id, platform, account_name,"
            " account_id, access_token_enc, refresh_token_enc, expires_at,"
            " scopes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, platform, account_id) DO UPDATE SET"
            " account_name = excluded.account_name,"
            " access_token_enc = excluded.access_token_enc,"
            " refresh_token_enc = CASE WHEN excluded.refresh_token_enc != ''"
            " THEN excluded.refresh_token_enc ELSE connections.refresh_token_enc END,"
            " expires_at = excluded.expires_at, scopes = excluded.scopes,"
            " updated_at = excluded.updated_at",
            (
                connection_id,
                user_id,
                platform,
                account_name,
                account_id,
                access_token_enc,
                refresh_token_enc,
                expires_at,
                scopes,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM connections WHERE user_id = ? AND platform = ?"
            " AND account_id = ?",
            (user_id, platform, account_id),
        ).fetchone()
    assert row is not None  # we just inserted or updated it
    return _connection_dict(row)


def get_connection(connection_id: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
    return None if row is None else _connection_dict(row)


def list_connections(user_id: str) -> list[dict]:
    """One account's connected channels, oldest first within a platform."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM connections WHERE user_id = ?"
            " ORDER BY platform, created_at, id",
            (user_id,),
        ).fetchall()
    return [_connection_dict(row) for row in rows]


def get_platform_connection(user_id: str, platform: str) -> dict | None:
    """The connection a publish to `platform` would use, else None.

    The oldest one, deterministically, for the day a platform allows more than
    one channel per account — a publish must never depend on row order.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM connections WHERE user_id = ? AND platform = ?"
            " ORDER BY created_at, id LIMIT 1",
            (user_id, platform),
        ).fetchone()
    return None if row is None else _connection_dict(row)


def update_connection(connection_id: str, **fields: object) -> None:
    """Rewrite a connection's tokens/scopes/name; `updated_at` moves with them.

    The whitelist is _CONNECTION_UPDATABLE, and the fields it leaves out are
    the ones that say WHOSE connection this is.
    """
    if not fields:
        return
    unknown = set(fields) - _CONNECTION_UPDATABLE
    if unknown:
        raise ValueError(f"update_connection: unknown fields {sorted(unknown)!r}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            f"UPDATE connections SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), connection_id),
        )


def delete_connection(connection_id: str) -> bool:
    """Forget a connection outright. True when a row was actually removed."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
    return cur.rowcount == 1


def _oauth_state_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _OAUTH_STATE_COLUMNS}


def put_oauth_state(
    state_hash: str,
    *,
    user_id: str,
    session_hash: str,
    platform: str,
    verifier_enc: str,
    redirect_uri: str,
    expires_at: str,
) -> None:
    """Stage one in-flight authorization. The nonce itself is never stored."""
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO oauth_states (state_hash, user_id, session_hash,"
            " platform, verifier_enc, redirect_uri, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state_hash,
                user_id,
                session_hash,
                platform,
                verifier_enc,
                redirect_uri,
                _now(),
                expires_at,
            ),
        )


def consume_oauth_state(state_hash: str) -> dict | None:
    """Spend an in-flight authorization, once. The row, or None.

    Single use is decided HERE and by the DELETE, exactly as it is for sign-in
    codes: the read and the removal are one transaction, so a state replayed by
    a second request — the CSRF attempt this whole mechanism exists for — finds
    nothing, whether it arrives an hour later or in the same millisecond as the
    first. An expired row is likewise not found, so the TTL is enforced by this
    read rather than by whether a sweep has run.
    """
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM oauth_states WHERE state_hash = ? AND expires_at > ?",
                (state_hash, now),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            cur = conn.execute(
                "DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,)
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return _oauth_state_dict(row) if cur.rowcount == 1 else None


def purge_expired_oauth_states(now_iso: str | None = None) -> int:
    """Delete authorizations nobody came back for; returns the count.

    Hygiene, never security — an expired state is already refused by
    consume_oauth_state — so this only keeps the table from collecting the
    flows people abandoned at the provider's consent screen.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM oauth_states WHERE expires_at <= ?", (now_iso or _now(),)
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Publish jobs (PUBLISH.md Part 3).
#
# The jobs table's shape, with the same rule about who may write a status: a
# publish is queue work a browser polls, and the only thing that makes its
# progress trustworthy is that a run which has lost its row cannot write to it.
# --------------------------------------------------------------------------- #


def _publish_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _PUBLISH_COLUMNS}


def create_publish_job(
    publish_id: str,
    *,
    user_id: str,
    clip_id: str,
    connection_id: str,
    platform: str,
    title: str,
    description: str,
    privacy: str,
) -> dict | None:
    """Queue one publish. None when this clip is ALREADY on its way there.

    The check and the insert are one transaction, so a double-tapped Post
    button — or two tabs — cannot both win. That guard is not tidiness: a
    default YouTube project gets a handful of uploads a day (PUBLISH.md), so a
    duplicate does not just make a duplicate video, it spends a scarce resource
    the user cannot get back until tomorrow.

    Scoped to (user, clip, platform): the same clip may be on its way to two
    different platforms at once, and two accounts never share a clip anyway.
    """
    now = _now()
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ", ".join("?" for _ in LIVE_PUBLISH_STATUSES)
            live = conn.execute(
                "SELECT * FROM publish_jobs WHERE user_id = ? AND clip_id = ?"
                f" AND platform = ? AND status IN ({placeholders})"
                " ORDER BY created_at LIMIT 1",
                (user_id, clip_id, platform, *LIVE_PUBLISH_STATUSES),
            ).fetchone()
            if live is not None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "INSERT INTO publish_jobs (id, user_id, clip_id, connection_id,"
                " platform, status, progress, detail, error, title, description,"
                " privacy, video_id, video_url, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    publish_id,
                    user_id,
                    clip_id,
                    connection_id,
                    platform,
                    "queued",
                    0.0,
                    "Queued",
                    None,
                    title,
                    description,
                    privacy,
                    "",
                    "",
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    published = get_publish_job(publish_id)
    assert published is not None  # we just inserted it
    return published


def get_publish_job(publish_id: str) -> dict | None:
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM publish_jobs WHERE id = ?", (publish_id,)
        ).fetchone()
    return None if row is None else _publish_dict(row)


def live_publish_job(user_id: str, clip_id: str, platform: str) -> dict | None:
    """The publish already in flight for this clip on this platform, else None.

    What the start route reports back when create_publish_job refuses, so the
    answer to "post this again" is the job that is already running rather than
    a bare refusal with nothing to poll.
    """
    placeholders = ", ".join("?" for _ in LIVE_PUBLISH_STATUSES)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM publish_jobs WHERE user_id = ? AND clip_id = ?"
            f" AND platform = ? AND status IN ({placeholders})"
            " ORDER BY created_at LIMIT 1",
            (user_id, clip_id, platform, *LIVE_PUBLISH_STATUSES),
        ).fetchone()
    return None if row is None else _publish_dict(row)


def update_publish_job(publish_id: str, **fields: object) -> None:
    """Update whitelisted publish columns; always bumps updated_at.

    Progress bookkeeping only. `status` is refused here for the reason
    update_job refuses it: a status write must name what it expects to replace.
    """
    if not fields:
        return
    if "status" in fields:
        raise ValueError(
            "update_publish_job: status is not updatable. Move it with"
            " transition_publish_status, which names the status it replaces"
        )
    unknown = set(fields) - _PUBLISH_UPDATABLE
    if unknown:
        raise ValueError(f"update_publish_job: unknown fields {sorted(unknown)!r}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            f"UPDATE publish_jobs SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), _now(), publish_id),
        )


def transition_publish_status(
    publish_id: str,
    *,
    expect: str | tuple[str, ...],
    to: str,
    stale_before: str | None = None,
    **fields: object,
) -> bool:
    """Atomically move a publish from `expect` to `to`. True = this call won.

    The compare-and-swap `transition_status` is for jobs, and it exists for the
    same race: a worker that has been declared stalled must not be able to write
    `done` over the failure somebody already recorded, and a second delivery of
    the same task must not upload twice.

    `stale_before` is the sweep's extra precondition — "and nothing has touched
    this row since" — so a task that is alive and writing progress is never
    failed out from under itself.
    """
    expected = _expected(expect)
    unknown = set(fields) - _PUBLISH_UPDATABLE
    if unknown:
        raise ValueError(f"transition_publish_status: unknown fields {sorted(unknown)!r}")
    extra = "".join(f", {name} = ?" for name in fields)
    placeholders = ", ".join("?" for _ in expected)
    sql = (
        f"UPDATE publish_jobs SET status = ?{extra}, updated_at = ?"
        f" WHERE id = ? AND status IN ({placeholders})"
    )
    params: list[object] = [to, *fields.values(), _now(), publish_id, *expected]
    if stale_before is not None:
        sql += " AND updated_at < ?"
        params.append(stale_before)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        return conn.execute(sql, params).rowcount == 1


def reconcile_stalled_publishes(cutoff_iso: str) -> int:
    """Fail publishes whose worker died mid-upload. Returns the count.

    Without this a killed worker leaves a row reading `uploading` forever, and
    the sheet watching it spins forever with it — the user is told their video
    is on its way to a channel nothing is uploading to. The SELECT only
    nominates; the compare-and-swap re-checks both halves of "stalled".
    """
    placeholders = ", ".join("?" for _ in LIVE_PUBLISH_STATUSES)
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        candidates = [
            row["id"]
            for row in conn.execute(
                f"SELECT id FROM publish_jobs WHERE status IN ({placeholders})"
                " AND updated_at < ?",
                (*LIVE_PUBLISH_STATUSES, cutoff_iso),
            )
        ]
    return sum(
        transition_publish_status(
            publish_id,
            expect=LIVE_PUBLISH_STATUSES,
            to="failed",
            stale_before=cutoff_iso,
            error=_PUBLISH_INTERRUPTED_ERROR,
            detail="Interrupted",
        )
        for publish_id in candidates
    )


def delete_publish_jobs_older_than(cutoff_iso: str) -> int:
    """Reap terminal publish rows past the cutoff; returns the count.

    Only `done` and `failed` — a row still queued or uploading is somebody's
    live work, and the stall sweep above is what settles those. The clip and the
    connection outlive this row: it is a receipt for one attempt, not a record
    of what is on the channel.
    """
    with contextlib.closing(_connect()) as conn:
        _ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM publish_jobs WHERE created_at < ?"
            " AND status IN ('done', 'failed')",
            (cutoff_iso,),
        )
        return cur.rowcount
