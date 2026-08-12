"""Account auth: scrypt password hashing, opaque session tokens, rate limits.

Secrets discipline (ACCOUNTS.md): passwords and raw session tokens are never
logged and never stored — only the scrypt hash of a password and the sha256 of
a token ever touch the database, and every comparison is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Sequence

from fastapi import Header, HTTPException

from . import db
from .settings import Settings, get_settings

logger = logging.getLogger(__name__)

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
    """The IDENTITY form: the string an account is keyed on.

    Case and surrounding whitespace are typing, not identity, so they are
    flattened — and nothing else is. In particular ``alex+work@gmail.com`` and
    ``alex@gmail.com`` stay two different accounts here. See ``mailbox_key``
    below, and the paragraph in it about why these two functions must not be
    merged.
    """
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL_RE.match(email))


#: Domains whose mail servers ignore dots in the local part, so that
#: ``a.lex@gmail.com`` and ``alex@gmail.com`` reach one inbox. An allow-list,
#: never a rule applied to every domain: plenty of providers treat dots as
#: significant, and folding them there would let one person's hourly cap refuse
#: mail to a genuinely different human's mailbox.
_DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com"})

#: Domains that are another domain's second name on the wire. Google's own
#: alias, so it is folded before the dot rule above is consulted.
_DOMAIN_ALIASES = {"googlemail.com": "gmail.com"}


def mailbox_key(email: str) -> str:
    """The DELIVERABILITY form: which INBOX an address lands in.

    IDENTITY AND DELIVERABILITY ARE TWO DIFFERENT QUESTIONS, and this function
    answers only the second. Do not merge it with ``normalize_email``, and do
    not "simplify" either one into the other — they disagree on purpose, and
    the disagreement is the point:

    * ``normalize_email`` answers "WHICH ACCOUNT is this?", and its answer keys
      the users table, the login_codes row and the code digest. Plus-addressing
      is something people use deliberately to hold separate accounts on one
      mailbox, so ``alex+work@gmail.com`` is not ``alex@gmail.com`` — folding
      them would let a code minted for one sign into the other, which is the
      worst class of bug this file can have.
    * ``mailbox_key`` answers "WHOSE INBOX does this fill up?", and its answer
      is used for exactly one thing: the per-address hourly cap in
      ``enforce_email_code_limit``. That cap exists so nobody can be mail-
      bombed by a stranger typing their address over and over (EMAILAUTH.md).
      Counted on the identity form it was decoration: one Gmail mailbox has
      unbounded distinct spellings — every ``+tag``, every placement of dots in
      the local part, a trailing dot on the domain — so an attacker simply
      never reused a string and never met the cap.

    The folding is deliberately conservative, because over-merging has a victim
    too: two people who share a key share an allowance, and one of them can
    then deny the other their sign-in mail. So only rules that are TRUE of the
    receiving mail server are applied — a single trailing dot on the domain
    (the same host, spelled absolutely), a ``+tag`` (RFC 5233 sub-addressing,
    which the providers that implement it all strip), and dots in the local
    part ONLY for the domains listed above, which document ignoring them.
    Everything else is left exactly as typed; an unknown domain's dots are
    significant until proven otherwise.
    """
    normalized = normalize_email(email)
    local, separator, domain = normalized.rpartition("@")
    if not separator:
        return normalized  # not an address shape; count the string as given
    if domain.endswith("."):
        domain = domain[:-1]  # the root-anchored spelling of the same host
    domain = _DOMAIN_ALIASES.get(domain, domain)
    tagless = local.partition("+")[0]
    # A local part that is ONLY a tag ("+promo@gmail.com") folds to nothing,
    # and an empty key would pool every such string into one bucket. Keep the
    # address as typed instead: refusing to guess is the safe direction.
    if tagless:
        local = tagless
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def normalize_password(password: str) -> str:
    """NFKC — the form a password is hashed and compared in.

    The same typed password has several Unicode encodings (a composed "é" vs
    "e" + U+0301, a fullwidth "Ａ" vs "A"): without normalizing, signing in
    from a different keyboard or IME than the one used at signup fails with a
    generic "incorrect password". ASCII passwords are returned untouched.
    """
    return unicodedata.normalize("NFKC", password)


def _scrypt(password: str, salt: bytes, params: tuple[int, int, int], dklen: int) -> bytes:
    n, r, p = params
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_SCRYPT_MAXMEM,
        dklen=dklen,
    )


def hash_password(password: str) -> str:
    """Hash for storage: `scrypt$16384$8$1$<salt_b64>$<hash_b64>`."""
    salt = secrets.token_bytes(32)
    digest = _scrypt(
        normalize_password(password), salt, (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P), _SCRYPT_DKLEN
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored scrypt string; never raises.

    The NFKC form is tried first — that is what `hash_password` stores. Then,
    only when the raw string differs from it, the raw form is tried too:
    accounts created before normalization landed stored the un-normalized
    bytes, and there is no way to rewrite those rows without the plaintext, so
    they can only be re-hashed the next time their owner signs in. Rejecting
    them until then would lock existing users out of their own accounts. An
    already-normalized password (every ASCII one) still runs exactly one
    scrypt, so the common path costs nothing.

    An EMPTY `stored` is refused before anything is parsed or compared. That
    is a password-less account — one created by Sign in with Google, whose
    row carries ``password_hash = ''`` (LIBRARY.md Part 1) — and the empty
    string is not a hash of anything, least of all of the empty password.
    Parsing would reject it anyway (``"".split("$")`` cannot unpack), but
    "no password set means no password login" is a rule this function states
    rather than a behaviour that falls out of an exception somewhere else.
    """
    if not stored:
        return False
    try:
        algorithm, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        params = (int(n_s), int(r_s), int(p_s))
    except (ValueError, TypeError):
        return False
    candidates = [normalize_password(password)]
    if candidates[0] != password:
        candidates.append(password)
    for candidate in candidates:
        try:
            digest = _scrypt(candidate, salt, params, len(expected))
        except (ValueError, TypeError):
            return False
        if hmac.compare_digest(digest, expected):
            return True
    return False


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


# --------------------------------------------------------------------------- #
# Email sign-in codes: 6 digits, hashed at rest, single use (EMAILAUTH.md).
# --------------------------------------------------------------------------- #

#: Digits in a login code. Six is what somebody can read off a phone
#: notification and type into another device — and it is also only a million
#: possibilities, which is why the attempt cap below, the TTL, the single-use
#: rule and the two rate limits are not decoration around this feature: they
#: ARE this feature's security. Widening the code is not the lever; keeping
#: every one of those rules is.
LOGIN_CODE_DIGITS = 6

#: Guesses one code will ever accept before it is deleted and the user has to
#: ask for another. At five, an attacker's odds against a single code are
#: 5-in-a-million. Without a cap, 6 digits is a million guesses at whatever
#: rate the per-client limiter allows — which is a matter of patience, not of
#: security. Nothing about the UX improves by raising this.
LOGIN_CODE_MAX_ATTEMPTS = 5


def new_login_code() -> str:
    """A fresh 6-digit code, zero-padded — the only place one is generated.

    ``secrets.randbelow`` (a CSPRNG), never ``random``: this is a credential
    for a whole account, and a predictable credential is not one. The
    zero-padding is part of the security, not the formatting: printing the
    integer plainly would leak its magnitude in the string's length and shrink
    the space to the 900_000 numbers that have six digits.
    """
    return f"{secrets.randbelow(10 ** LOGIN_CODE_DIGITS):0{LOGIN_CODE_DIGITS}d}"


def hash_login_code(email: str, code: str) -> str:
    """The sha256 hex that IS stored — the code itself never is.

    The digest covers the ADDRESS as well as the digits, and that is what
    makes hashing something this small worth anything. A bare sha256 of six
    digits is a million-entry table that anybody can precompute in about a
    second, so a leaked database file would hand over every live code in
    plaintext and "hashed storage" would be a word rather than a defence.
    Binding the address gives each row its own digest space — there is no one
    table that covers every address — and, for free, means a code minted for
    one address can never verify another.

    Do not "simplify" this to ``sha256(code)``: it would still pass every test
    that only checks the plaintext is absent, and it would give a database
    reader ten minutes of unrestricted account access.
    """
    return hashlib.sha256(
        f"{normalize_email(email)}\x00{code}".encode("utf-8")
    ).hexdigest()


def login_code_matches(email: str, code: str, stored_hash: str) -> bool:
    """Constant-time check of a submitted code against a stored digest.

    ``hmac.compare_digest``, the same discipline sessions and the founder
    token get: a plain ``==`` returns as soon as two hex strings differ, and
    the time it took says how much of the digest was right. Both sides here
    are our own ASCII hex, so there is nothing for the comparison to choke on.
    """
    return hmac.compare_digest(hash_login_code(email, code), stored_hash)


def login_code_expires_at() -> str:
    """When a code minted now stops working (CC_EMAIL_CODE_TTL_MINUTES)."""
    ttl = timedelta(minutes=get_settings().email_code_ttl_minutes)
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


def matches_api_token(authorization: str | None, token: str) -> bool:
    """Constant-time `Authorization: Bearer <CC_API_TOKEN>` check.

    Compares BYTES, not str: ``hmac.compare_digest`` raises TypeError the
    moment either str carries a non-ASCII codepoint, and this header is
    attacker-controlled (starlette decodes headers as latin-1, so a raw
    ``Authorization: Bearer é`` on the wire arrives as a perfectly ordinary
    Python str). Comparing str turned that into an unhandled 500 on every
    route that resolves an actor — including the deliberately credential-free
    ``GET /v1/jobs/{id}``. Encoding first makes it a plain mismatch.
    """
    if authorization is None or not token:
        return False
    return hmac.compare_digest(
        authorization.encode("utf-8"), (_BEARER_PREFIX + token).encode("utf-8")
    )


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
    if not matches_api_token(authorization, token):
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
    if matches_api_token(authorization, get_settings().api_token):
        return Actor(founder=True)
    return Actor()


# --------------------------------------------------------------------------- #
# Rate limiting for the credential endpoints.
# --------------------------------------------------------------------------- #

# A fixed-window counter per (client, route): RATE_LIMIT_PER_MINUTE attempts a
# minute, then 429 until the minute turns over.
#
# The counters live in REDIS — the same instance Celery already brokers on, so
# production gains no dependency it did not already have — because every
# property this limiter needs is a property of the STORE, not of code defending
# a dict:
#
#   * atomic: INCR is one operation, so two concurrent attempts cannot lose a
#     count or tear a read-modify-write, whatever thread they land on;
#   * self-expiring: the key names its own window and is given a TTL, so a dead
#     counter is removed by Redis and there is nothing to sweep — a sweep of a
#     shared table is what the previous version raced on;
#   * bounded by the store: the key space costs memory Redis manages and evicts
#     by its own policy, not memory this process rations with a ceiling on
#     attacker-supplied strings. That ceiling was itself the bug: past it the
#     limiter changed MODE, and an attacker who can flip the limiter into a
#     different mode owns the limiter.
#
# Without Redis — dev, CC_QUEUE=eager, the test suite — the counters fall back
# to a fixed-size in-process table (see _count_in_memory), which is bounded by
# construction rather than by a cap anybody has to enforce.
RATE_LIMIT_PER_MINUTE = 10

_RATE_LIMITED = "Too many attempts. Please wait a minute and try again."

# Redis key layout: cc:rl:<route>:<minute>:<client>. The window is IN the key,
# so a new minute is a new key rather than a counter somebody has to reset.
_KEY_PREFIX = "cc:rl:"

# How long a counter lives once created. Its window is over one minute after
# it was first written, by definition, so a one-window TTL is all Redis needs
# to reclaim it.
_WINDOW_TTL_S = 60

# Short and explicit: the credential routes are `def`, so FastAPI runs them in
# the anyio threadpool, and a Redis that hangs instead of refusing would
# otherwise pin one worker per attempt for as long as the kernel would wait.
_REDIS_TIMEOUT_S = 1.0

# INCR, and EXPIRE only on the call that CREATED the key. Redis runs a script
# atomically, so this cannot interleave with another caller's INCR and cannot
# leave the key immortal by dying between the two commands.
_INCR_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

_OFF_VALUES = {"", "0", "off", "false", "no", "none", "disabled"}

# The in-process fallback. A FIXED array of (window, count) slots addressed by
# a hash of the key — not a dict with an entry per client — so there is nothing
# here an attacker can make grow: the table is exactly this size after one
# request and after ten million, needs no eviction, and needs no sweep (a slot
# whose stored window is not the current one has simply expired, which is an
# O(1) check on the one slot being touched). Two keys that land in the same
# slot SHARE a counter, and sharing can only ever refuse EARLIER — never admit
# more, and never reset anyone's count — so the error direction is toward the
# strict side. The slot is chosen by blake2b rather than hash() so it is the
# same on every run: a collision is then a fact of the code, findable and
# fixable, instead of a coin flip that shows up once in a thousand boots.
#
# Stated plainly, because it is the price of that determinism: someone who can
# choose their own key can compute which forged address shares a slot with a
# victim and push that slot over the ceiling. Reaching it needs BOTH a trusted
# proxy (otherwise client_ip returns the socket peer, which nobody chooses) and
# this fallback in use — i.e. CC_QUEUE=eager, which is dev, not a deployment.
# Production counts in Redis, where every key stands alone.
_MEMORY_SLOTS = 1 << 16
_memory_lock = threading.Lock()
_memory_windows: list[tuple[int, int]] = [(-1, 0)] * _MEMORY_SLOTS

# The per-ADDRESS hourly window for email sign-in codes (EMAILAUTH.md), in its
# own table and under its own Redis prefix rather than sharing the ones above.
# That separation is load-bearing, not tidiness: the property that makes a
# shared slot harmless — every key in a table counts the same KIND of window,
# so two keys in one slot merely add to each other and can only ever refuse
# EARLIER — holds per table. Mixing minute numbers and hour numbers in one
# array would break it in the direction that ADMITS more: a slot holding an
# hour number looks like an expired minute to the other counter, and each
# would silently reset the other's count. Two arrays cost 64 K tuples and
# make that impossible instead of unlikely.
_HOUR_TTL_S = 3600
_HOUR_KEY_PREFIX = "cc:rlh:"
_memory_hours: list[tuple[int, int]] = [(-1, 0)] * _MEMORY_SLOTS

# The route name the per-address counter files under. Not an HTTP route: the
# thing being metered is one email ADDRESS being sent codes, wherever the
# requests come from.
_EMAIL_CODE_ROUTE = "email-code"

_EMAIL_CODE_RATE_LIMITED = (
    "That address has been sent too many sign-in codes. Try again in an hour, "
    "or sign in with your password."
)

# The last minute a Redis outage was logged. Logging hygiene only — an outage
# is every single request, and a warning per login attempt is its own incident.
_logged_outage_minute = -1


_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=4)
def _trusted_networks(entries: tuple[str, ...]) -> tuple[_Network, ...]:
    """CC_TRUSTED_PROXIES parsed into networks; unparseable entries are dropped."""
    networks: list[_Network] = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(
                "CC_TRUSTED_PROXIES entry %r is not an ip address or CIDR block, "
                "so it is ignored (forwarded headers from that peer stay untrusted)",
                entry,
            )
    return tuple(networks)


def client_ip(peer: str, forwarded_for: str | None, trusted_proxies: Sequence[str]) -> str:
    """The address rate limits are counted against.

    ``peer`` is the socket's own address — the only thing here a client cannot
    choose. Behind the reverse proxy DEPLOY.md mandates, that address is the
    proxy's for EVERY caller, which would collapse the whole internet into one
    bucket: one attacker at a trickle locks every account out of login and
    signup, and per-client credential-stuffing protection stops existing.

    So ``X-Forwarded-For``'s leftmost entry is used instead — but ONLY when the
    peer is one of the proxies the operator configured in CC_TRUSTED_PROXIES
    (default: nobody). Trusting that header unconditionally is strictly worse
    than not having a limiter: it is a request header, so anyone can rotate it
    per attempt and never be counted. The proxy must be configured to REPLACE
    the header with the real peer (Caddy: `header_up X-Forwarded-For
    {remote_host}`) — a proxy that appends leaves the leftmost entry
    client-written. Anything that isn't an ip address falls back to the peer,
    so a malformed header is never a counter of its own.
    """
    if not forwarded_for or not trusted_proxies:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer  # non-ip peers (unix socket, test transport) are never proxies
    if not any(peer_address in net for net in _trusted_networks(tuple(trusted_proxies))):
        return peer
    leftmost = forwarded_for.split(",")[0].strip()
    try:
        ipaddress.ip_address(leftmost)
    except ValueError:
        return peer
    return leftmost


def _current_minute() -> int:
    return int(time.time() // 60)


def _current_hour() -> int:
    """The window the per-address code limit counts in.

    Derived from ``_current_minute`` so both limiters read one clock — which
    also means a test that pins the minute has pinned this too.
    """
    return _current_minute() // 60


def rate_limit_fails_open(settings: Settings) -> bool:
    """Whether an unreachable Redis lets credential attempts through.

    The default is NO — this limiter fails CLOSED — and that is a deliberate
    trade, not an oversight:

    * Failing open hands an attacker the exact thing the limiter exists to
      deny, unmetered online password guessing, and hands it over at the one
      moment nobody is reading dashboards for anything but the outage. Worse,
      it is reachable: whoever can knock Redis over can also un-limit login.
    * Closed is cheap here specifically because Redis is the Celery broker.
      A box that cannot reach it cannot queue a render either, so refusing
      credential attempts during the outage does not turn a working product
      into a broken one — it declines to widen an outage that already exists.

    The cost is real and worth stating plainly: a momentary blip turns a
    legitimate sign-in into a 429 that asks the user to wait a minute. An
    operator who would rather serve logins than refuse them sets
    ``CC_RATE_LIMIT_FAIL_OPEN=on`` and takes the other side of that trade
    knowingly, in writing, in their own environment.
    """
    value = getattr(settings, "rate_limit_fail_open", "off")
    return str(value).strip().lower() not in _OFF_VALUES


def _memory_slot(client: str, route: str) -> int:
    """The fixed table slot a key is counted in (stable across processes)."""
    digest = hashlib.blake2b(
        f"{route}\x00{client}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % _MEMORY_SLOTS


def _count_in_memory(
    client: str, route: str, window: int, table: list[tuple[int, int]]
) -> int:
    """Count one attempt in an in-process table; the running count.

    `table` is the fixed array for ONE kind of window (minutes above, hours in
    `_memory_hours`) and `window` is that kind's window number, so every key
    in a table speaks the same units — see the note beside `_memory_hours` for
    why mixing them would be a bug rather than a saving.

    One lock, held across the whole read-modify-write, is the entire
    concurrency story: there is no second structure to keep in step and
    nothing that iterates, so there is nothing for a concurrent caller to
    tear. A slot from an older window is simply overwritten, which is how a
    window ends here — no sweep, no eviction, no cap.
    """
    slot = _memory_slot(client, route)
    with _memory_lock:
        stored, count = table[slot]
        count = count + 1 if stored == window else 1
        table[slot] = (window, count)
    return count


@lru_cache(maxsize=1)
def _redis_client(url: str):  # noqa: ANN202 - redis.Redis
    """The shared Redis handle for `url`; redis-py pools connections itself."""
    import redis

    return redis.Redis.from_url(
        url,
        socket_connect_timeout=_REDIS_TIMEOUT_S,
        socket_timeout=_REDIS_TIMEOUT_S,
    )


def _log_redis_outage(minute: int, error: BaseException, *, fail_open: bool) -> None:
    """Warn once a minute that the limiter cannot reach its counters."""
    global _logged_outage_minute
    with _memory_lock:
        if _logged_outage_minute == minute:
            return
        _logged_outage_minute = minute
    logger.warning(
        "rate limiter could not reach redis (%s: %s). Credential attempts are "
        "being %s while this lasts (CC_RATE_LIMIT_FAIL_OPEN)",
        type(error).__name__,
        error,
        "allowed through" if fail_open else "refused",
    )


def _count_attempt(
    client: str,
    route: str,
    window: int,
    *,
    prefix: str,
    ttl_s: int,
    table: list[tuple[int, int]],
) -> int | None:
    """Count one attempt; the running count, or None if Redis was unreachable.

    CC_QUEUE=redis is what "this box has the Redis it needs" means here: it
    names the very instance Celery brokers on. CC_QUEUE=eager — dev and the
    test suite — has no Redis to reach, so it counts in process.

    `prefix`/`ttl_s`/`table` are the window kind: the per-client minute window
    and the per-address hour window keep separate keys in Redis and separate
    arrays in process, so neither can ever be counted as the other.
    """
    settings = get_settings()
    if settings.queue != "redis":
        return _count_in_memory(client, route, window, table)
    key = f"{prefix}{route}:{window}:{client}"
    try:
        return int(
            _redis_client(settings.redis_url).eval(_INCR_WINDOW_LUA, 1, key, ttl_s)
        )
    except Exception as error:  # noqa: BLE001 - any failure to count is an outage
        # Deliberately broad. A refused connection, a timeout, a redis module
        # that will not import and a reply this code cannot read are one event
        # to the caller — the attempt was NOT counted — and the policy in
        # enforce_rate_limit is what decides what that means. Narrowing this to
        # redis.RedisError would let some other failure become a 500 on an
        # unauthenticated route, which is a worse answer than either policy.
        #
        # The MINUTE is what the log is deduplicated on, whatever window this
        # call was counting: both limiters share one "already warned" marker,
        # so handing it an hour number from one caller and a minute number
        # from the other would make them alternate and log on every single
        # request — the exact flood this hygiene exists to prevent.
        _log_redis_outage(
            _current_minute(), error, fail_open=rate_limit_fails_open(settings)
        )
        return None


def enforce_rate_limit(client: str, route: str) -> None:
    """Count one attempt; 429 beyond RATE_LIMIT_PER_MINUTE in the window.

    ``client`` is who the attempt is attributed to — see ``client_ip``, the one
    place a forwarded header is ever believed. Nothing else about a request can
    change which counter is touched, and no amount of traffic can change the
    ceiling any other client is held to.
    """
    minute = _current_minute()
    count = _count_attempt(
        client,
        route,
        minute,
        prefix=_KEY_PREFIX,
        ttl_s=_WINDOW_TTL_S,
        table=_memory_windows,
    )
    if count is None:
        # Redis is unreachable; fail closed unless the operator said otherwise.
        if rate_limit_fails_open(get_settings()):
            return
        raise HTTPException(status_code=429, detail=_RATE_LIMITED)
    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail=_RATE_LIMITED)


def enforce_email_code_limit(email: str) -> None:
    """Count one code request for ONE address; 429 past CC_EMAIL_CODE_PER_HOUR.

    The SECOND of the two limits on the email-code routes, and it exists for a
    different attack than the first (EMAILAUTH.md). `enforce_rate_limit` meters
    a CLIENT, so it stops one machine farming codes — but it counts the sender,
    and a sender with a botnet, or simply a patient one, is metered per machine.
    This counts the RECIPIENT, so a stranger cannot turn our mailer into a way
    to bomb somebody's inbox by typing their address over and over, however
    many addresses they send from. Neither limit substitutes for the other and
    removing either leaves a real attack open.

    Counted against the MAILBOX (``mailbox_key``), not against the account
    identity: what is being protected here is an inbox, and one inbox answers
    to unboundedly many spellings — " Creator@Gmail.com ", "creator+1@…",
    "cr.eator@…", "creator@gmail.com." all fill the same one. Keyed on the
    identity form, the cap was a formatting exercise an attacker skipped by
    never repeating a string. The identity form is still what the code row,
    the digest and the account lookup use — see ``mailbox_key`` for why those
    two questions have to keep separate answers.
    """
    settings = get_settings()
    count = _count_attempt(
        mailbox_key(email),
        _EMAIL_CODE_ROUTE,
        _current_hour(),
        prefix=_HOUR_KEY_PREFIX,
        ttl_s=_HOUR_TTL_S,
        table=_memory_hours,
    )
    if count is None:
        # Same trade as the per-client limiter: an attempt that cannot be
        # counted is refused unless the operator has taken the other side.
        if rate_limit_fails_open(settings):
            return
        raise HTTPException(status_code=429, detail=_EMAIL_CODE_RATE_LIMITED)
    if count > settings.email_code_per_hour:
        raise HTTPException(status_code=429, detail=_EMAIL_CODE_RATE_LIMITED)


def reset_rate_limits() -> None:
    """Forget the in-process counters and drop the Redis handle (test hook).

    Only the in-process tables are cleared: Redis counters name their window
    and expire on their own, and a test that wants to look at them owns the
    client it injected.
    """
    global _logged_outage_minute
    with _memory_lock:
        _memory_windows[:] = [(-1, 0)] * _MEMORY_SLOTS
        _memory_hours[:] = [(-1, 0)] * _MEMORY_SLOTS
        _logged_outage_minute = -1
    _redis_client.cache_clear()
