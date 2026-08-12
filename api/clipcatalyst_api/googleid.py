"""Sign in with Google: ID token verification (LIBRARY.md Part 1).

Google is an IDENTITY provider here and nothing else. It says who somebody
is; it never gets to publish on their behalf. TikTok, Instagram and YouTube
arrive later as CONNECTIONS on an account that already exists, precisely so
that a creator whose TikTok is banned does not lose their ClipCatalyst
account and their library with it.

The signature check belongs to ``google-auth``. Hand-rolling JWKS handling
and RS256 is the kind of code that is quietly wrong for a year, and a whole
account sits behind this boundary. What lives here is the part that library
does not do:

  * Google's key set is CACHED, and refetched when a token names a key id we
    have not seen — that is what a key rotation looks like from this side.
  * The claim rules this product needs: issuer, audience, expiry AND
    not-before with a bounded clock skew (google-auth checks ``iat``/``exp``
    and never looks at ``nbf``), and a VERIFIED email.

``email_verified`` is the whole security boundary, not a formality: a
matching email LINKS an existing password account, so an unverified address
would be an account takeover with an extra step. It is checked strictly —
the claim must be a JSON ``true``, never merely truthy.

Nothing here decides who is signed in; it answers what Google said, and
``main.py`` decides what that means for the users table.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import exceptions as google_exceptions
from google.auth import jwt as google_jwt

from . import auth

logger = logging.getLogger(__name__)

#: Google's JWK Set, as named by their OpenID discovery document at
#: https://accounts.google.com/.well-known/openid-configuration.
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

#: Both spellings Google issues ID tokens under; both are legitimate, and a
#: verifier that knows only one of them rejects real sign-ins.
ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

#: How far our clock and Google's may disagree before a token reads as early
#: or late. 60 s is the ceiling LIBRARY.md sets, and it is applied to `exp`,
#: `iat` and `nbf` alike.
CLOCK_SKEW_S = 60

#: A Google ID token is on the order of a kilobyte. The cap is here so a
#: multi-megabyte body is refused before any parsing is attempted at all.
MAX_TOKEN_BYTES = 8192

#: How long a fetched key set is served before it is fetched again.
_JWKS_TTL_S = 3600

#: The floor between fetches once something IS cached. The unknown-`kid`
#: refresh is triggered by an attacker-chosen string — `kid` is just a field
#: in an unverified header — so without a floor a stream of forged tokens
#: becomes a stream of requests to Google with our address on them. A
#: rotation Google announces days ahead can afford to be seen a minute late.
_MIN_REFRESH_INTERVAL_S = 60

#: Bounds on the one outbound request this module makes.
_FETCH_TIMEOUT_S = 5
_MAX_JWKS_BYTES = 128 * 1024


class InvalidIdToken(Exception):
    """The token is not a current, verified Google identity for this server.

    Every reason collapses to one answer at the HTTP edge — 401 — so the
    message is for the server log, never for the client.
    """


class KeysUnavailable(RuntimeError):
    """Google's key set could not be fetched and nothing was cached.

    Deliberately NOT an InvalidIdToken: the token may be perfectly good and
    the fault is ours, so the caller owes an honest 503 rather than telling
    somebody their sign-in was rejected.
    """


@dataclass(frozen=True)
class GoogleIdentity:
    """What a verified ID token establishes: who, and at what address.

    `sub` is Google's immutable account id — the thing an account is keyed
    on, because an email can be changed and a `sub` cannot. `email` is
    already in the normal form ``auth.normalize_email`` produces, which is
    what makes matching it against an existing account meaningful.
    """

    sub: str
    email: str


# The cached key set and the two timestamps that govern it. `_fetched_at` is
# when the cached keys were last successfully replaced (None = nothing
# cached); `_attempted_at` is when a fetch was last TRIED, successfully or
# not, and is what the refresh floor is measured against — a failing endpoint
# must not be retried once per request.
_lock = threading.Lock()
_keys: dict[str, bytes] = {}
_fetched_at: float | None = None
_attempted_at: float | None = None


def _b64url_int(value: str) -> int:
    """A JWK's base64url big-endian integer (`n`, `e`) as a Python int."""
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


def _jwk_to_pem(jwk: Mapping[str, object]) -> bytes | None:
    """One JWK as a PEM public key, or None if this build cannot use it.

    Only the modulus and exponent are read; everything else Google publishes
    about a key is metadata. An entry we do not understand is SKIPPED rather
    than raised on, so a key set that one day gains an unfamiliar member
    still verifies the tokens signed by the rest of it — a future addition on
    Google's side must not be an outage on ours.
    """
    if str(jwk.get("kty") or "") != "RSA":
        return None
    # An absent `use`/`alg` is the JWK spec's "unrestricted"; Google sends
    # both, and anything that names something other than RS256 signing is
    # not a key an ID token of ours was signed with.
    if str(jwk.get("use") or "sig") != "sig":
        return None
    if str(jwk.get("alg") or "RS256") != "RS256":
        return None
    try:
        public_key = rsa.RSAPublicNumbers(
            _b64url_int(str(jwk["e"])), _b64url_int(str(jwk["n"]))
        ).public_key()
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    return public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _parse_jwks(document: Mapping[str, object]) -> dict[str, bytes]:
    """kid → PEM, for every usable RS256 signing key in a JWK Set."""
    keys: dict[str, bytes] = {}
    entries = document.get("keys")
    if not isinstance(entries, list):
        return keys
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        kid = str(entry.get("kid") or "")
        pem = _jwk_to_pem(entry) if kid else None
        if pem is not None:
            keys[kid] = pem
    return keys


def fetch_jwks(url: str = JWKS_URL) -> dict:
    """GET a JWK Set and return it as parsed JSON.

    The ONE place this module touches the network, and a plain module-level
    function for exactly that reason: the tests replace it with a stub
    serving a key set they generated themselves, so every signature check
    below runs for real against a real RSA key rather than being mocked away.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
        raw = response.read(_MAX_JWKS_BYTES + 1)
    if len(raw) > _MAX_JWKS_BYTES:
        raise ValueError(f"key set at {url} is larger than {_MAX_JWKS_BYTES} bytes")
    return json.loads(raw.decode("utf-8"))


def _load_keys(*, force: bool) -> dict[str, bytes]:
    """The cached key set, fetching when it is absent, stale, or forced.

    `force` is the unknown-`kid` path. It is honoured, because refusing a
    token on a key set we know to be incomplete is how a rotation becomes an
    outage — and it is floored (see _MIN_REFRESH_INTERVAL_S), because the
    trigger is attacker-chosen. With nothing cached at all the floor does not
    apply: a cold process must be able to serve its first sign-in.

    The fetch happens UNDER the lock. That is the point: a hundred concurrent
    sign-ins during a rotation make one request to Google, not a hundred.
    """
    global _keys, _fetched_at, _attempted_at
    with _lock:
        now = time.monotonic()
        # None = nothing cached, which is the one state the floor below must
        # not apply to: a cold process has to be able to serve its first
        # sign-in, and it has nothing to fall back on.
        age = None if _fetched_at is None else now - _fetched_at
        if age is not None:
            if age < _JWKS_TTL_S and not force:
                return _keys
            if (
                _attempted_at is not None
                and now - _attempted_at < _MIN_REFRESH_INTERVAL_S
            ):
                return _keys  # somebody just tried; serve what we have
        cached = age is not None
        _attempted_at = now
        try:
            document = fetch_jwks(JWKS_URL)
            parsed = _parse_jwks(document)
        except Exception as error:  # noqa: BLE001 - every failure is "no keys"
            # A transport error, a body that is not JSON, and a body that is
            # JSON but not a key set are one event here: we did not get a new
            # key set. What differs is whether we still have the old one.
            if not cached:
                raise KeysUnavailable(
                    f"could not fetch Google's key set: {type(error).__name__}: {error}"
                ) from error
            logger.warning(
                "could not refresh Google's key set (%s: %s), continuing with "
                "the %d cached key(s)",
                type(error).__name__,
                error,
                len(_keys),
            )
            return _keys
        if not parsed:
            if not cached:
                raise KeysUnavailable(
                    f"the key set at {JWKS_URL} carried no usable RS256 signing keys"
                )
            logger.warning(
                "Google's key set carried no usable RS256 signing keys, "
                "continuing with the %d cached key(s)",
                len(_keys),
            )
            return _keys
        _keys = parsed
        _fetched_at = now
        return _keys


def reset_keys_cache() -> None:
    """Forget the cached key set (test hook, like auth.reset_rate_limits)."""
    global _keys, _fetched_at, _attempted_at
    with _lock:
        _keys = {}
        _fetched_at = None
        _attempted_at = None


def _claim_is_true(value: object) -> bool:
    """A JSON ``true`` — the only thing that counts as a verified email.

    Google sends a boolean; older documentation of theirs shows the string
    "true", so that spelling is accepted as well. NOTHING else is, and
    "truthy" is deliberately not the test: the string "false" is truthy, and
    a truthiness check here would hand an unverified address the keys to
    whichever account already owns it.
    """
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def _check_not_before(claims: Mapping[str, object]) -> None:
    """Enforce `nbf` with the same skew `exp` gets.

    google-auth verifies `iat` and `exp` and never looks at `nbf`, so this is
    ours to do. Google does not normally send the claim at all, which is why
    an absent one is fine — but a token that says it is not valid yet must
    not sign anybody in.
    """
    raw = claims.get("nbf")
    if raw is None:
        return
    try:
        not_before = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InvalidIdToken("token carries an unreadable nbf claim") from error
    if time.time() < not_before - CLOCK_SKEW_S:
        raise InvalidIdToken("token is not valid yet (nbf)")


def verify_id_token(raw_token: str, client_id: str) -> GoogleIdentity:
    """Verify a Google ID token; the identity inside it, or a raise.

    Raises ``InvalidIdToken`` for everything the caller owes a 401 to — a bad
    signature, an unknown key, the wrong audience or issuer, an expired or
    not-yet-valid token, an unverified or missing email — and
    ``KeysUnavailable`` when Google's keys could not be reached at all, which
    is a deployment problem and not a credential one.
    """
    if not client_id:
        raise ValueError(
            "verify_id_token needs a client id to check `aud` against; callers "
            "must refuse an unconfigured server before getting here"
        )
    if not raw_token or len(raw_token) > MAX_TOKEN_BYTES:
        raise InvalidIdToken("token is missing or implausibly long")

    # The header is read UNVERIFIED — that is what a `kid` is for, and it is
    # trusted for exactly one thing: choosing which public key to check the
    # signature against. A wrong or forged `kid` names a key that will not
    # verify, or no key at all.
    try:
        header = google_jwt.decode_header(raw_token)
    except (google_exceptions.GoogleAuthError, ValueError, TypeError) as error:
        raise InvalidIdToken(f"unreadable token header: {error}") from error
    kid = str(header.get("kid") or "")
    if not kid:
        # Google always names its key. Requiring it keeps verification to the
        # one key that claims to have signed this token, instead of trying
        # every key in the set against an attacker-supplied signature.
        raise InvalidIdToken("token header names no key id")

    keys = _load_keys(force=False)
    if kid not in keys:
        logger.info("unknown Google signing key %r, refreshing the key set", kid)
        keys = _load_keys(force=True)
    if kid not in keys:
        raise InvalidIdToken(f"no Google signing key with id {kid!r}")

    try:
        claims = google_jwt.decode(
            raw_token,
            certs=keys,
            audience=client_id,
            clock_skew_in_seconds=CLOCK_SKEW_S,
        )
    except (google_exceptions.GoogleAuthError, ValueError, TypeError) as error:
        # A bad signature, the wrong `aud`, an `exp` in the past and an `iat`
        # in the future all land here, and all mean the same thing to the
        # caller: not a token this server signs anybody in on.
        raise InvalidIdToken(f"token failed verification: {error}") from error

    issuer = str(claims.get("iss") or "")
    if issuer not in ISSUERS:
        raise InvalidIdToken(f"issuer {issuer!r} is not Google")
    _check_not_before(claims)

    sub = str(claims.get("sub") or "")
    if not sub:
        raise InvalidIdToken("token carries no subject")
    if not _claim_is_true(claims.get("email_verified")):
        # The refusal that matters most (LIBRARY.md): a verified address is
        # the only thing that may link to, or create, an account.
        raise InvalidIdToken("Google has not verified this token's email address")
    email = auth.normalize_email(str(claims.get("email") or ""))
    if not auth.is_valid_email(email):
        raise InvalidIdToken("token carries no usable email address")
    return GoogleIdentity(sub=sub, email=email)
