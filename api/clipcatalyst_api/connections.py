"""Connected publishing accounts: OAuth in, encrypted tokens at rest.

The half of publishing that is not about video (PUBLISH.md Part 2). A creator
authorizes a channel once; what we keep afterwards is an access token, a
refresh token, and the channel's name — and the first two are the reason this
module is written the way it is. A refresh token is a standing permission to
upload to somebody's channel that does not expire on its own, so a database
file that leaks one is not an incident about our service, it is an incident
about their audience.

Four rules carry that weight, and none of them is optional:

  * **Tokens are ciphertext everywhere below this module.** Fernet, keyed by
    ``CC_TOKEN_KEY``. ``db.py`` has no key and cannot read what it stores; no
    response model has a field one could go in. With no key configured,
    connecting is REFUSED (503) and nothing at all is written — never
    "plaintext for now".
  * **The `state` is signed, single-use and bound to the session.** Signed
    (HMAC, keyed off the same secret) so a value we did not mint is refused
    before any lookup; single-use because ``db.consume_oauth_state`` spends the
    row in the same transaction it reads it; bound because the row — not the
    callback request, which arrives as a bare browser redirect with no
    credentials — is what says whose account this connection lands on, and the
    flow is refused if that session has since ended.
  * **PKCE is required, not offered.** The verifier is generated at `start`,
    stored encrypted, and produced at the exchange. No verifier, no tokens —
    an intercepted authorization code is worth nothing on its own.
  * **A decrypt failure is a disconnect, not a crash.** A rotated or
    mis-restored key makes ciphertext unreadable. That must degrade to "please
    reconnect your channel", never to a 500 in the middle of somebody's
    publish.

All provider HTTP goes through ONE function, ``http_request`` — the seam
``googleid.fetch_jwks`` and ``mailer.post_json`` already establish — so the
tests drive the whole authorize → exchange → identify → refresh → revoke path
against a stub, for real, with no network.

Nothing here uploads anything. The YouTube adapter (PUBLISH.md Part 3) asks
this module for a live access token and does the rest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .settings import Settings

logger = logging.getLogger(__name__)

#: How long an authorization may sit half-finished at the provider's consent
#: screen. Ten minutes is generous for "click Allow" and short enough that an
#: abandoned flow is not a state somebody can come back to tomorrow.
STATE_TTL_MINUTES = 10

#: Refresh an access token when it is within this of expiry. Five minutes
#: (PUBLISH.md) covers an upload that starts with a token which would have died
#: mid-transfer — the failure mode a "refresh only when expired" rule creates.
REFRESH_SKEW_S = 300

#: Bounds on the provider calls this module makes.
_TIMEOUT_S = 10
_MAX_RESPONSE_BYTES = 64 * 1024

#: A parser guard on the `state` coming back from a provider: ours is ~90
#: characters, and anything past this is refused before a signature is computed.
MAX_STATE_LENGTH = 512


class TokenKeyUnavailable(RuntimeError):
    """No usable ``CC_TOKEN_KEY``: unset, or not a valid Fernet key.

    A DEPLOYMENT state, not a user error — the caller owes an honest 503 and
    must store nothing. Deliberately distinct from TokenUnreadable: a missing
    key means every stored connection is temporarily unreadable, and treating
    that as "this connection is broken" would delete every one of them the
    first time somebody forgot an environment variable.
    """


class TokenUnreadable(RuntimeError):
    """Stored ciphertext will not decrypt under the key this box has.

    A rotated key, a restored backup, a hand-edited row. The connection cannot
    be used again and cannot be revoked, so the honest response is to forget it
    and ask for a reconnect (see NeedsReconnect).
    """


class NeedsReconnect(RuntimeError):
    """The connection is gone and the user has to authorize again.

    Raised only AFTER the unusable row has been removed, so the account page
    the message sends somebody to already shows the channel as disconnected.
    """


class ProviderRefused(RuntimeError):
    """The provider said no (4xx): the grant is dead, not delayed.

    A revoked refresh token, an authorization code already spent, a mismatched
    redirect URI. Retrying changes nothing.
    """


class ProviderUnavailable(RuntimeError):
    """We could not reach the provider, or it answered 5xx. Try again later."""


class AccountMissing(RuntimeError):
    """Authorization succeeded but there is nothing to publish to.

    A Google account with no YouTube channel is the real case: consent is
    granted, the token works, and ``channels.list?mine=true`` comes back empty.
    Storing that connection would put a row on the account page that can never
    complete a post.
    """


# --------------------------------------------------------------------------- #
# The providers. One entry per platform ClipCatalyst knows about — including
# the ones that cannot connect yet, because the account page has to say so out
# loud rather than offer a button that goes nowhere (PUBLISH.md Part 4).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Provider:
    """Everything the OAuth flow needs to know about one platform.

    Declarative on purpose: TikTok and Instagram arrive as entries here plus an
    ``identify`` function, not as branches through the flow below.
    """

    platform: str
    label: str
    authorize_url: str
    token_url: str
    revoke_url: str
    #: What we call, with a fresh access token, to learn which account we are
    #: connected to. Its reply goes to `identify`.
    identity_url: str
    scopes: tuple[str, ...]
    #: (account_id, account_name) from that reply.
    identify: Callable[[Mapping[str, object]], tuple[str, str]]
    #: Provider-specific query parameters the authorize URL must carry.
    authorize_extra: tuple[tuple[str, str], ...] = ()
    #: False = the product knows this platform but cannot complete a post on it
    #: yet (an audit or a review is outstanding). `reason` says why, in words a
    #: creator reads on the account page.
    connectable: bool = True
    reason: str = ""
    #: The standing caveat about posting here, shown whether or not it is
    #: connected. Empty when there is nothing to warn about.
    note: str = ""


def _youtube_identity(payload: Mapping[str, object]) -> tuple[str, str]:
    """(channel id, channel title) from a ``youtube/v3/channels`` reply.

    An empty ``items`` is the Google-account-with-no-YouTube-channel case, and
    it is an AccountMissing rather than a blank name: the consent screen
    happily grants upload scopes to an account that has nowhere to upload.
    """
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise AccountMissing(
            "That Google account doesn't have a YouTube channel yet. Create one "
            "on YouTube, then connect again."
        )
    first = items[0] if isinstance(items[0], Mapping) else {}
    channel_id = str(first.get("id") or "")
    snippet = first.get("snippet")
    title = str(snippet.get("title") or "") if isinstance(snippet, Mapping) else ""
    if not channel_id:
        raise AccountMissing(
            "YouTube didn't name a channel for that account. Please try again."
        )
    return channel_id, title or "YouTube channel"


PROVIDERS: dict[str, Provider] = {
    "youtube": Provider(
        platform="youtube",
        label="YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        # Revoking either token of a Google grant revokes the whole grant.
        revoke_url="https://oauth2.googleapis.com/revoke",
        identity_url=(
            "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
        ),
        # `upload` is the point. `readonly` is here for exactly one thing —
        # naming the channel in the UI, which channels.list will not do without
        # it — and for nothing else; both are sensitive scopes and go through
        # the same verification, so this is not a scope we get for free.
        scopes=(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ),
        identify=_youtube_identity,
        # Both halves are load-bearing and neither is a default: without
        # `access_type=offline` Google issues no refresh token at all, and
        # without `prompt=consent` it stops re-issuing one on every later
        # authorization — so a user who reconnects would get a connection that
        # works until the first hour is up and then cannot be refreshed.
        authorize_extra=(("access_type", "offline"), ("prompt", "consent")),
        note=(
            "Until Google finishes reviewing this app, uploads land on your "
            "channel as private videos. You publish them from YouTube."
        ),
    ),
    # Known, and honestly unavailable. TikTok's Direct Post needs an audit and
    # Instagram needs a Business account plus App Review (PUBLISH.md), so
    # neither can complete a post today. They are listed rather than hidden so
    # the account page can say why instead of leaving a creator wondering.
    "tiktok": Provider(
        platform="tiktok",
        label="TikTok",
        authorize_url="",
        token_url="",
        revoke_url="",
        identity_url="",
        scopes=(),
        identify=lambda payload: ("", ""),
        connectable=False,
        reason=(
            "TikTok posting is awaiting review. Until it lands, use the share "
            "button on a clip to post it from your phone."
        ),
    ),
    "instagram": Provider(
        platform="instagram",
        label="Instagram",
        authorize_url="",
        token_url="",
        revoke_url="",
        identity_url="",
        scopes=(),
        identify=lambda payload: ("", ""),
        connectable=False,
        reason=(
            "Instagram posting is awaiting review. Until it lands, use the "
            "share button on a clip to post it from your phone."
        ),
    ),
}


def provider_for(platform: str) -> Provider | None:
    """The provider a platform name means, else None (an unknown platform)."""
    return PROVIDERS.get(platform.strip().lower())


def credentials(settings: Settings, platform: str) -> tuple[str, str]:
    """(client id, client secret) for a platform — ('', '') when unconfigured.

    An explicit map rather than a naming convention over Settings: which
    credential a platform runs as is a decision, and the day YouTube and a
    future Google-owned platform share (or deliberately do not share) a client
    is a decision somebody should have to write down here.
    """
    if platform == "youtube":
        return settings.youtube_client_id, settings.youtube_client_secret
    return "", ""


def redirect_uri(settings: Settings, platform: str) -> str:
    """Where the provider sends the browser back, or '' when we cannot say.

    Built from ``CC_PUBLIC_BASE_URL`` because it must be an absolute URL that
    the provider's console has been told about — a relative path, which is what
    the clip and logo URLs fall back to, is not something Google can redirect
    to. Empty means this box has no public identity yet and cannot honestly
    start an OAuth flow.
    """
    base = settings.public_base_url
    return f"{base}/v1/connections/{platform}/callback" if base else ""


# --------------------------------------------------------------------------- #
# Encryption at rest. The only key in the product, and the only module holding
# it. Everything above stores and forwards ciphertext.
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=4)
def _fernet_for(key: str) -> Fernet | None:
    """The Fernet for a key string, or None when it is not a usable key.

    Cached because a publish refreshes tokens per upload and this is otherwise
    a base64 decode per call — and because the warning below then happens once
    per distinct bad key rather than once per request. The key never reaches a
    log line: what is logged is that it did not parse.
    """
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        logger.warning(
            "CC_TOKEN_KEY is set but is not a valid Fernet key, so connections "
            "are refused rather than stored unencrypted. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
        return None


def _fernet(settings: Settings) -> Fernet | None:
    key = settings.token_key.strip()
    return _fernet_for(key) if key else None


def is_configured(settings: Settings) -> bool:
    """Can this box hold a connection at all? (a usable ``CC_TOKEN_KEY``)

    Mirrors ``mailer.is_configured``: callers check it FIRST and answer an
    honest 503, because the alternative to encrypting a refresh token is not
    storing it another way — it is not storing it.
    """
    return _fernet(settings) is not None


def encrypt(settings: Settings, plaintext: str) -> str:
    """Ciphertext for one secret. Raises TokenKeyUnavailable with no key."""
    fernet = _fernet(settings)
    if fernet is None:
        raise TokenKeyUnavailable("CC_TOKEN_KEY is unset or unusable")
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(settings: Settings, ciphertext: str) -> str:
    """One secret back. TokenKeyUnavailable with no key; TokenUnreadable else.

    The two failures are different events with different answers, which is why
    they are different exceptions — see TokenKeyUnavailable. Neither message
    carries any part of the value.
    """
    fernet = _fernet(settings)
    if fernet is None:
        raise TokenKeyUnavailable("CC_TOKEN_KEY is unset or unusable")
    if not ciphertext:
        raise TokenUnreadable("nothing stored")
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as error:
        raise TokenUnreadable(
            f"stored value did not decrypt under this key ({type(error).__name__})"
        ) from error


# --------------------------------------------------------------------------- #
# The `state`: signed, single-use, session-bound. PKCE beside it.
# --------------------------------------------------------------------------- #


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _state_key(settings: Settings) -> bytes:
    """The HMAC key the state is signed with, derived from ``CC_TOKEN_KEY``.

    Domain-separated so it is a different key from the one Fernet uses, and
    derived rather than configured separately so there is one secret to deploy
    and one to rotate. Rotating the token key therefore also invalidates
    in-flight authorizations, which is correct: they are half-finished flows,
    and starting again costs a click.
    """
    return hashlib.sha256(
        b"clipcatalyst/oauth-state\x00" + settings.token_key.strip().encode("utf-8")
    ).digest()


def _signature(settings: Settings, platform: str, nonce: str) -> str:
    return _b64u(
        hmac.new(
            _state_key(settings),
            f"{platform}\x00{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )


def hash_state(nonce: str) -> str:
    """What is STORED for a state — the nonce itself never is."""
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def new_state(settings: Settings, platform: str) -> tuple[str, str]:
    """A fresh state: (the value the provider echoes back, its stored hash).

    The nonce is the unguessable part; the signature binds it to the PLATFORM,
    so a state minted for one platform's flow cannot be presented at another's
    callback — and so a value we never issued is refused by arithmetic, before
    it reaches the database.
    """
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_signature(settings, platform, nonce)}", hash_state(nonce)


def state_nonce(settings: Settings, platform: str, state: str) -> str:
    """The nonce inside a state THIS server signed for THIS platform, else ''.

    Constant-time comparison, like every other secret comparison in the
    product: the signature is what stands between a forged state and a database
    lookup, and a byte-by-byte `==` on it is an oracle.
    """
    if not state or len(state) > MAX_STATE_LENGTH:
        return ""
    nonce, separator, signature = state.partition(".")
    if not separator or not nonce or not signature:
        return ""
    if not hmac.compare_digest(signature, _signature(settings, platform, nonce)):
        return ""
    return nonce


def state_expires_at() -> str:
    ttl = timedelta(minutes=STATE_TTL_MINUTES)
    return (datetime.now(timezone.utc) + ttl).isoformat(timespec="milliseconds")


def new_verifier() -> str:
    """A PKCE code verifier (RFC 7636): 86 URL-safe characters from a CSPRNG."""
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier — the only method this product sends."""
    return _b64u(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(
    settings: Settings,
    provider: Provider,
    *,
    state: str,
    verifier: str,
    redirect: str,
) -> str:
    """The URL to send the browser to, PKCE challenge and all."""
    client_id, _ = credentials(settings, provider.platform)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        **dict(provider.authorize_extra),
    }
    return f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
# Provider HTTP. One function, replaced wholesale by the tests.
# --------------------------------------------------------------------------- #


def http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    """Make one provider call; return (status, body-as-text).

    The ONE place this module touches the network, and a plain module-level
    function for the reason ``mailer.post_json`` is one: the tests replace it
    with a stub, so the token exchange, the identity call, the refresh and the
    revoke are all exercised for real against a provider that answers.

    An HTTP error status is a RETURN, not a raise — 4xx and 5xx mean different
    things to every caller here (dead grant vs. try again), and that decision
    belongs to them. Transport failures (DNS, connect, timeout, TLS) do raise,
    and callers turn them into ProviderUnavailable.
    """
    data = (
        urllib.parse.urlencode(dict(form)).encode("ascii") if form is not None else None
    )
    request = urllib.request.Request(url, data=data, method=method.upper())
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return int(response.status), response.read(_MAX_RESPONSE_BYTES).decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(_MAX_RESPONSE_BYTES).decode(
            "utf-8", "replace"
        )


def _payload(body: str) -> dict:
    """A provider's reply as a dict; {} when it is not a JSON object."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reason(payload: Mapping[str, object], status: int) -> str:
    """A one-line explanation of a refusal, safe to put in a log.

    NAMED FIELDS ONLY, never the body. A successful token response is a JSON
    object with an access token in it, and the difference between "log the
    reply on failure" and "log the reply" is one edit by somebody who does not
    know that — so nothing here can ever quote a whole body, whatever its
    status was.
    """
    raw = payload.get("error")
    if isinstance(raw, Mapping):  # the Google APIs nest theirs
        error = str(raw.get("status") or "")
        description = str(raw.get("message") or "")
    else:
        error = str(raw or "")
        description = str(payload.get("error_description") or "")
    detail = " ".join(part for part in (error, description) if part)
    return f"HTTP {status}{': ' + detail if detail else ''}"


@dataclass(frozen=True)
class TokenSet:
    """What a token endpoint hands back, in the shape the row wants."""

    access_token: str
    refresh_token: str
    expires_at: str
    scopes: str


def _expires_at(expires_in: object) -> str:
    """`expires_in` seconds as an absolute deadline; '' when unstated.

    '' means the provider never told us the token expires, and is treated as
    "do not pre-emptively refresh" rather than as "expired" — see
    ``needs_refresh``.
    """
    try:
        seconds = int(expires_in)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return deadline.isoformat(timespec="milliseconds")


def _token_set(payload: Mapping[str, object], *, keeping: str = "") -> TokenSet:
    """A TokenSet from a token reply, keeping the refresh token we already had.

    `keeping` is the stored refresh token. Providers that ROTATE theirs send a
    new one and it wins (PUBLISH.md); providers that do not — Google, on a
    refresh — send none, and the one we hold must survive the write. Getting
    this backwards is a connection that works exactly once more.
    """
    return TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or "") or keeping,
        expires_at=_expires_at(payload.get("expires_in")),
        scopes=str(payload.get("scope") or ""),
    )


def _token_call(url: str, form: Mapping[str, str], *, what: str) -> dict:
    """POST to a token endpoint; the parsed reply, or a typed failure."""
    try:
        status, body = http_request("POST", url, form=form)
    except Exception as error:  # noqa: BLE001 - every transport failure is "not reached"
        # A refused connection, a DNS failure, a TLS error and a timeout are
        # one event to the caller: we did not get an answer. Narrowing this
        # would turn some of them into a 500 on a route a provider redirects a
        # user's browser to.
        raise ProviderUnavailable(
            f"{what} failed: {type(error).__name__}: {error}"
        ) from error
    payload = _payload(body)
    if 500 <= status < 600:
        raise ProviderUnavailable(f"{what} failed: {_reason(payload, status)}")
    if status < 200 or status >= 300:
        raise ProviderRefused(f"{what} was refused: {_reason(payload, status)}")
    return payload


def exchange_code(
    settings: Settings,
    provider: Provider,
    *,
    code: str,
    verifier: str,
    redirect: str,
) -> TokenSet:
    """Trade an authorization code (plus its PKCE verifier) for tokens.

    `redirect` is the value used at `start`, replayed here because the provider
    checks the two match — which is also why it is stored on the state row
    rather than recomputed: a ``CC_PUBLIC_BASE_URL`` changed between the two
    halves of one flow must fail the exchange, not silently authorize against a
    different callback.
    """
    client_id, client_secret = credentials(settings, provider.platform)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect,
        # Not optional and not conditional: no verifier, no tokens.
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    tokens = _token_set(_token_call(provider.token_url, form, what="token exchange"))
    if not tokens.access_token:
        raise ProviderUnavailable("token exchange returned no access token")
    return tokens


def identify(provider: Provider, access: str) -> tuple[str, str]:
    """(account id, account name) for a fresh access token."""
    try:
        status, body = http_request(
            "GET",
            provider.identity_url,
            headers={"Authorization": f"Bearer {access}"},
        )
    except Exception as error:  # noqa: BLE001 - see _token_call
        raise ProviderUnavailable(
            f"{provider.label} account lookup failed: {type(error).__name__}: {error}"
        ) from error
    payload = _payload(body)
    if 500 <= status < 600:
        raise ProviderUnavailable(
            f"{provider.label} account lookup failed: {_reason(payload, status)}"
        )
    if status < 200 or status >= 300:
        raise ProviderRefused(
            f"{provider.label} account lookup was refused: {_reason(payload, status)}"
        )
    return provider.identify(payload)


def revoke(provider: Provider, token: str) -> None:
    """Ask the provider to invalidate a grant.

    A 4xx is success as far as disconnecting goes — the token is already dead,
    which is the state we were asking for. A 5xx or an unreachable provider is
    NOT: raising leaves the row in place so the user can try again, because
    deleting it would destroy the only copy of a token that is still live on
    their channel and can then never be revoked by anybody but them.
    """
    try:
        status, body = http_request("POST", provider.revoke_url, form={"token": token})
    except Exception as error:  # noqa: BLE001 - see _token_call
        raise ProviderUnavailable(
            f"{provider.label} revoke failed: {type(error).__name__}: {error}"
        ) from error
    if 500 <= status < 600:
        raise ProviderUnavailable(
            f"{provider.label} revoke failed: {_reason(_payload(body), status)}"
        )
    if status >= 400:
        logger.info(
            "%s answered %s to a revoke, so that grant was already invalid",
            provider.label,
            _reason(_payload(body), status),
        )


# --------------------------------------------------------------------------- #
# Storing, refreshing and removing a connection.
# --------------------------------------------------------------------------- #


def complete(
    settings: Settings,
    provider: Provider,
    *,
    user_id: str,
    code: str,
    verifier: str,
    redirect: str,
) -> dict:
    """Exchange, identify, store. The row, as ``db.upsert_connection`` made it.

    The account is identified BEFORE anything is written: a connection with no
    channel behind it (AccountMissing) must not leave a row that the account
    page offers to publish through.
    """
    tokens = exchange_code(
        settings, provider, code=code, verifier=verifier, redirect=redirect
    )
    account_id, account_name = identify(provider, tokens.access_token)
    connection = db.upsert_connection(
        uuid.uuid4().hex,
        user_id=user_id,
        platform=provider.platform,
        account_name=account_name,
        account_id=account_id,
        access_token_enc=encrypt(settings, tokens.access_token),
        refresh_token_enc=(
            encrypt(settings, tokens.refresh_token) if tokens.refresh_token else ""
        ),
        expires_at=tokens.expires_at,
        scopes=tokens.scopes or " ".join(provider.scopes),
    )
    logger.info(
        "connected %s for user %s (connection %s)",
        provider.platform,
        user_id,
        connection["id"],
    )
    return connection


def needs_refresh(expires_at: str) -> bool:
    """Is this access token inside the refresh window (or already past it)?

    '' — the provider never stated an expiry — is False: refreshing on a
    deadline nobody set would burn a refresh token on every single call.
    An unparseable value is True, because a deadline we cannot read is one we
    cannot claim to be inside of.
    """
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) + timedelta(seconds=REFRESH_SKEW_S) >= deadline


def _forget(connection_id: str, why: str) -> None:
    """Drop an unusable connection and say why (never what was in it)."""
    db.delete_connection(connection_id)
    logger.warning("connection %s removed: %s", connection_id, why)


#: What a user is told when the connection they had is no longer usable. Two
#: short sentences, one action: every cause (rotated key, revoked grant, a
#: refresh token that never arrived) leads to the same place.
RECONNECT_REQUIRED = (
    "That channel needs to be connected again. Open your account page and "
    "reconnect it."
)


def access_token(settings: Settings, connection: Mapping[str, object]) -> str:
    """A LIVE access token for a stored connection. The publish path's entry.

    Refreshes when the stored token is within REFRESH_SKEW_S of expiry and
    persists what comes back — the new access token, its new deadline, and a
    rotated refresh token if the provider sent one.

    Raises NeedsReconnect when the connection cannot be made to work again, and
    only after removing it: unreadable ciphertext, an expired token with no
    refresh token behind it, or a provider that refuses the refresh (the user
    revoked us from their own account settings — a perfectly ordinary thing to
    do, and not an error to shout about). ProviderUnavailable is the other
    outcome and is the opposite kind: nothing is deleted, and trying again
    later works.
    """
    connection_id = str(connection["id"])
    platform = str(connection["platform"])
    provider = provider_for(platform)
    if provider is None or not provider.connectable:
        # A row for a platform this build cannot talk to. It cannot be
        # refreshed and it cannot be revoked, so it is not kept.
        _forget(connection_id, f"platform {platform!r} is not one this build connects")
        raise NeedsReconnect(RECONNECT_REQUIRED)

    stored_refresh = str(connection.get("refresh_token_enc") or "")
    try:
        access = decrypt(settings, str(connection.get("access_token_enc") or ""))
        refresh = decrypt(settings, stored_refresh) if stored_refresh else ""
    except TokenUnreadable as error:
        # The decrypt failure PUBLISH.md names: degrade to "reconnect", never
        # raise into a publish. TokenKeyUnavailable deliberately does NOT land
        # here — it is a missing key, not a broken row, and it propagates so
        # the caller answers 503 with every connection still intact.
        _forget(connection_id, f"stored tokens are unreadable ({error})")
        raise NeedsReconnect(RECONNECT_REQUIRED) from error

    if not needs_refresh(str(connection.get("expires_at") or "")):
        return access
    if not refresh:
        _forget(connection_id, "the access token expired and there is no refresh token")
        raise NeedsReconnect(RECONNECT_REQUIRED)

    client_id, client_secret = credentials(settings, platform)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    try:
        payload = _token_call(provider.token_url, form, what="token refresh")
    except ProviderRefused as error:
        # invalid_grant: revoked, expired through disuse, or a client that no
        # longer matches. Nothing to retry.
        _forget(connection_id, f"the provider refused a refresh ({error})")
        raise NeedsReconnect(RECONNECT_REQUIRED) from error

    tokens = _token_set(payload, keeping=refresh)
    if not tokens.access_token:
        raise ProviderUnavailable("token refresh returned no access token")
    db.update_connection(
        connection_id,
        access_token_enc=encrypt(settings, tokens.access_token),
        refresh_token_enc=encrypt(settings, tokens.refresh_token),
        expires_at=tokens.expires_at,
        scopes=tokens.scopes or str(connection.get("scopes") or ""),
    )
    return tokens.access_token


def disconnect(settings: Settings, connection: Mapping[str, object]) -> None:
    """Revoke with the provider, THEN delete the row.

    The order is the point (PUBLISH.md). The row is the only copy of the
    tokens, so deleting first would leave a live standing permission on
    somebody's channel that nobody at ClipCatalyst can ever revoke again.
    ``revoke`` therefore raises when the provider could not be reached, and
    that propagates: the row survives and a retry finishes the job — the same
    shape ``DELETE /v1/clips/{id}`` uses for a file it could not unlink.

    Two cases have nothing to revoke and go straight to the delete, both
    deliberately: ciphertext that will not decrypt (the token is unrecoverable
    — refusing would strand the row on the account page forever), and a box
    with no key at all (an operator's misconfiguration must not stop somebody
    removing a connection they no longer want).
    """
    connection_id = str(connection["id"])
    provider = provider_for(str(connection["platform"]))
    token = ""
    try:
        # The REFRESH token first: revoking it takes the whole grant with it,
        # while revoking an access token leaves the refresh token alive.
        stored_refresh = str(connection.get("refresh_token_enc") or "")
        stored_access = str(connection.get("access_token_enc") or "")
        if stored_refresh:
            token = decrypt(settings, stored_refresh)
        elif stored_access:
            token = decrypt(settings, stored_access)
    except TokenUnreadable as error:
        logger.warning(
            "connection %s: nothing revocable to send %s (%s)",
            connection_id,
            provider.label if provider else "the provider",
            error,
        )
    except TokenKeyUnavailable:
        logger.warning(
            "connection %s: removed without revoking, because this box has no "
            "usable CC_TOKEN_KEY to read its tokens with",
            connection_id,
        )
    if token and provider is not None and provider.revoke_url:
        revoke(provider, token)
    db.delete_connection(connection_id)
    logger.info("disconnected %s (connection %s)", connection["platform"], connection_id)
