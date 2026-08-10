"""Account auth: scrypt password hashing, opaque session tokens, rate limits.

Secrets discipline (ACCOUNTS.md): passwords and raw session tokens are never
logged and never stored — only the scrypt hash of a password and the sha256 of
a token ever touch the database, and every comparison is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import Header, HTTPException

from . import db
from .settings import get_settings

# --------------------------------------------------------------------------- #
# Passwords: stdlib scrypt (no extra dependency).
# --------------------------------------------------------------------------- #

# Parameters ride inside each stored hash (`scrypt$N$r$p$salt_b64$hash_b64`),
# so they can be raised later without invalidating existing rows.
_SCRYPT_N = 16384  # 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# n=2**14, r=8 needs 16 MiB per hash; give OpenSSL explicit headroom so the
# default maxmem never rejects our own parameters.
_SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 8

# Deliberately simple shape check: a non-space local part, an @, a dotted
# domain. Deliverability is only really proven once CC_MAILER can send mail.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL_RE.match(email))


def hash_password(password: str) -> str:
    """Hash for storage: `scrypt$16384$8$1$<salt_b64>$<hash_b64>`."""
    salt = secrets.token_bytes(32)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored scrypt string; never raises."""
    try:
        algorithm, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """A throwaway hash so login burns scrypt time for unknown emails too,
    keeping response timing from becoming a user-exists oracle."""
    return hash_password(secrets.token_urlsafe(16))


# --------------------------------------------------------------------------- #
# Sessions: opaque bearer tokens, sha256-hashed at rest.
# --------------------------------------------------------------------------- #

SESSION_TOKEN_PREFIX = "cc_sess_"
_BEARER_PREFIX = "Bearer "


def new_session_token() -> str:
    """A raw session token — returned to the client once, never stored."""
    return SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_session_token(raw_token: str) -> str:
    """The sha256 hex that IS stored; a leaked DB cannot replay sessions."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def session_expires_at() -> str:
    ttl = timedelta(days=get_settings().session_ttl_days)
    return (datetime.now(timezone.utc) + ttl).isoformat(timespec="milliseconds")


def bearer_session_token(authorization: str | None) -> str | None:
    """The raw `cc_sess_…` token from an Authorization header, else None."""
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        return None
    raw = authorization[len(_BEARER_PREFIX) :]
    return raw if raw.startswith(SESSION_TOKEN_PREFIX) else None


def resolve_session_user(authorization: str | None) -> dict | None:
    """The user row behind a session bearer, else None (absent/unknown/expired)."""
    raw = bearer_session_token(authorization)
    if raw is None:
        return None
    return db.get_session_user(hash_session_token(raw))


def require_session(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """FastAPI dependency: the signed-in user row, or one generic 401."""
    user = resolve_session_user(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return user


# --------------------------------------------------------------------------- #
# Callers of the job routes: session user, founder token, or open dev.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Actor:
    """Resolved caller identity for the job routes."""

    user: dict | None = None  # session-authenticated account row
    founder: bool = False  # presented the configured CC_API_TOKEN

    @property
    def user_id(self) -> str:
        return str(self.user["id"]) if self.user is not None else ""


def require_actor(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Actor:
    """Resolve the caller of a gated job route.

    A session bearer (`Bearer cc_sess_…`) always resolves as that user (401
    when unknown/expired). Otherwise the pre-accounts founder-token rules
    apply unchanged: ``CC_API_TOKEN=""`` keeps the API open (dev default);
    a configured token must match ``Authorization: Bearer <token>`` exactly,
    compared in constant time.
    """
    if bearer_session_token(authorization) is not None:
        return Actor(user=require_session(authorization))
    token = get_settings().api_token
    if not token:
        return Actor()
    expected = _BEARER_PREFIX + token
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")
    return Actor(founder=True)


def optional_actor(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Actor:
    """require_actor for the open status route: no credential → anonymous.

    A presented session is still validated (bad/expired → 401) so a stale
    token fails loudly instead of silently reading as anonymous; a founder
    bearer is honoured; anything else polls as the anonymous actor, which
    sees exactly the jobs it always could — the unowned ones.
    """
    if bearer_session_token(authorization) is not None:
        return Actor(user=require_session(authorization))
    token = get_settings().api_token
    if (
        token
        and authorization is not None
        and hmac.compare_digest(authorization, _BEARER_PREFIX + token)
    ):
        return Actor(founder=True)
    return Actor()


# --------------------------------------------------------------------------- #
# Rate limiting for the credential endpoints.
# --------------------------------------------------------------------------- #

# Fixed-window counter per (client ip, route). Honest limitation: this is
# in-process memory — counters reset on every restart and are not shared
# across workers or replicas, so a multi-process deploy multiplies the
# effective limit by its process count. Good enough to blunt online
# credential guessing on the current single-box deploy; move the counters to
# a shared store (redis) if that changes.
RATE_LIMIT_PER_MINUTE = 10
_rate_windows: dict[tuple[str, str], tuple[int, int]] = {}


def _current_minute() -> int:
    return int(time.time() // 60)


def enforce_rate_limit(client_ip: str, route: str) -> None:
    """Count one attempt; 429 beyond RATE_LIMIT_PER_MINUTE in the window."""
    minute = _current_minute()
    key = (client_ip, route)
    window, count = _rate_windows.get(key, (minute, 0))
    if window != minute:
        window, count = minute, 0
    count += 1
    _rate_windows[key] = (window, count)
    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts — please wait a minute and try again.",
        )


def reset_rate_limits() -> None:
    """Forget all rate-limit windows (test isolation hook)."""
    _rate_windows.clear()
