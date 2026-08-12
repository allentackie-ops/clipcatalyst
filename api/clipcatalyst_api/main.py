"""FastAPI app: accounts/auth, job creation, uploads, start, status, files."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError

# The form parser builds Starlette's UploadFile; FastAPI's is a SUBCLASS used
# for signature-declared params, so an isinstance check against it silently
# misses every part of a hand-parsed form (and quietly cleared the logo).
from starlette.datastructures import UploadFile

from . import auth, billing, brandkit, db, googleid, mailer
from .models import (
    AuthResponse,
    AuthUserOut,
    BrandKitOut,
    BrandKitRequest,
    CheckoutRequest,
    CheckoutResponse,
    ClipDeletedResponse,
    ClipDetailOut,
    ClipListResponse,
    ClipOut,
    ClipSummaryOut,
    ClipUploadRequest,
    ClipWordOut,
    CreateJobRequest,
    CreateJobResponse,
    EmailCodeStartRequest,
    EmailCodeStartResponse,
    EmailCodeVerifyRequest,
    EntitlementsOut,
    GoogleAuthRequest,
    HealthzResponse,
    JobStatusResponse,
    LoginRequest,
    LogoutResponse,
    MeResponse,
    PortalResponse,
    QuotaOut,
    RegisterRequest,
    StartJobResponse,
    UploadAckResponse,
    UploadTargetOut,
    WebhookAckResponse,
)
from .plans import PLANS, effective_plan
from .settings import Settings, get_settings
from .storage import clip_media_type, get_storage
from .worker import process_job, reconcile_stalled

logger = logging.getLogger(__name__)

API_VERSION = "0.1.0"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOGIN_FAILED = "Incorrect email or password."
# One generic refusal for every way an ID token can fail (LIBRARY.md Part 1):
# bad signature, wrong audience or issuer, expired, not yet valid, unknown
# key, unverified email. Which of them it was is a server-log fact.
_GOOGLE_SIGN_IN_FAILED = "That Google sign-in couldn't be verified — please try again."
_GOOGLE_SIGN_IN_OFF = (
    "Sign in with Google isn't enabled on this server (CC_GOOGLE_CLIENT_ID is "
    "unset) — sign in with your email and password instead."
)
_GOOGLE_KEYS_UNAVAILABLE = (
    "Google's sign-in keys couldn't be reached just now — please try again in "
    "a moment."
)
# Both halves of a Google sign-in raced: the address was claimed by another
# account while this request was resolving it. Vanishingly rare, and honest.
_GOOGLE_ACCOUNT_RACE = (
    "That account was being changed at the same moment — please try signing in "
    "again."
)
# --- email sign-in codes (EMAILAUTH.md) ------------------------------------- #
# ONE refusal for every way a code can fail to sign somebody in: wrong digits,
# a code that expired, a code whose five guesses are spent, a code that was
# never asked for, and an address nobody has ever requested a code for. Which
# of those it was is a server-side fact. Splitting this into helpful variants
# would rebuild the enumeration oracle the whole flow is shaped to avoid — "no
# code was requested for that address" tells a stranger which addresses are in
# the middle of signing in, and "that code has expired" confirms one was.
_EMAIL_CODE_FAILED = (
    "That code isn't valid — it may have expired or already been used. "
    "Request a new one."
)
_EMAIL_CODE_OFF = (
    "Email sign-in codes aren't enabled on this server (CC_MAILER is none) — "
    "sign in with your email and password instead."
)
# A send that did not happen is an ERROR, never a cheerful "check your inbox".
# The user is told to try again rather than left watching an inbox nothing is
# coming to.
_EMAIL_CODE_SEND_FAILED = (
    "We couldn't send your sign-in code just now — please try again in a "
    "moment, or sign in with your password."
)
# The account was claimed between resolving the address and creating it.
# Vanishingly rare, and honest — mirrors _GOOGLE_ACCOUNT_RACE.
_EMAIL_CODE_ACCOUNT_RACE = (
    "That account was being changed at the same moment — please try signing in "
    "again."
)
_BILLING_OFF = (
    "Billing isn't enabled on this server yet — plan upgrades will activate "
    "once Stripe is configured."
)
# Honest, not coy: it names the feature, the plan that carries it, and what
# happens meanwhile (BRANDKIT.md — the free tier's mark IS the upsell).
_BRAND_KIT_FORBIDDEN = (
    "A brand kit is included from the Starter plan up. Your clips render with "
    "the ClipCatalyst mark for now — upgrade to put your own logo and caption "
    "colour on them."
)
_BRAND_UNSUPPORTED_BODY = (
    "Send the brand kit as multipart/form-data (logo, caption_color, "
    "show_logo) or as JSON."
)
_BRAND_UNREADABLE_LOGO = (
    "That logo couldn't be read — upload the file itself, or a base64 "
    "'data:' URL of it."
)
_BRAND_BAD_COLOR = (
    "That caption colour isn't a hex value — use #rrggbb (for example #a78bfa)."
)
# The ceiling on the whole PUT body, as opposed to MAX_LOGO_BYTES on the decoded
# logo: a 2 MB logo is ~2.7 MB once base64'd into a data: URL, plus the JSON or
# multipart envelope around it. Bodies past this are refused before they are
# buffered; what survives is still measured after decoding.
_BRAND_MAX_BODY_BYTES = 4_000_000

_DATA_URL_RE = re.compile(r"^data:([^;,]*)((?:;[^;,]*)*),", re.IGNORECASE)

# --- the clip library (LIBRARY.md Part 2) ----------------------------------- #

_CLIP_UNKNOWN = "Unknown clip."
# The file is gone but the row is not, which is the whole point of the library
# — so this says so instead of pretending the clip never existed.
_CLIP_EXPIRED = (
    "This clip's video has expired and been deleted. Its title, score, hooks "
    "and transcript are still here."
)
_CLIP_NOT_A_VIDEO = (
    "That file isn't a clip we can store — save the MP4 or WebM the Studio "
    "rendered."
)
_CLIP_UNSUPPORTED_BODY = (
    "Send the clip as multipart/form-data with a `file` part and a `metadata` "
    "JSON part."
)
_CLIP_BAD_METADATA = "That clip metadata isn't valid JSON."
# The clip's own bytes are streamed and counted; this is only the JSON card
# beside them. A 60 s clip's transcript is a few kilobytes.
_CLIP_METADATA_MAX_BYTES = 256_000
# Room for the multipart envelope and the metadata part on top of the file
# itself, so a clip exactly at the limit is not refused by its own boundary.
_CLIP_BODY_SLACK_BYTES = 1_000_000
_CLIP_UPLOAD_CHUNK = 1 << 20  # 1 MiB


def _sniff_clip_type(head: bytes) -> str | None:
    """'mp4' | 'webm' read from the bytes themselves, else None.

    The declared content type of an upload is a string the client chose, and
    what we store gets served back from our own origin — so the container is
    decided here, from the file's own magic, exactly as brandkit sniffs logos.
    The Studio's MediaRecorder produces one of these two and nothing else
    (lib/studio/render.ts).
    """
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    # EBML is Matroska's header too; the DocType right after it is what says
    # WebM. Requiring it keeps this to the containers a browser really makes.
    if head[:4] == b"\x1a\x45\xdf\xa3" and b"webm" in head[:64]:
        return "webm"
    return None


def _rate_limit(request: Request, route: str) -> None:
    """Count one attempt from this request against `route`'s window.

    The address counted is whoever the trusted-proxy rules in auth.py say the
    client is: the socket peer, unless the peer is a proxy the operator named
    in CC_TRUSTED_PROXIES. The counting itself is a windowed INCR in Redis
    (auth.enforce_rate_limit), so it is shared across every process serving
    this API rather than per-worker.
    """
    peer = request.client.host if request.client is not None else "unknown"
    client = auth.client_ip(
        peer,
        request.headers.get("X-Forwarded-For"),
        get_settings().trusted_proxies,
    )
    auth.enforce_rate_limit(client, route)


def _no_store(response: Response) -> None:
    """Mark a response as never-cacheable.

    Credential and account responses carry a session token or a user's plan
    and usage; a shared cache (or a browser's back/forward store) holding onto
    either is a leak, so they are explicitly no-store rather than relying on
    an intermediary's defaults.
    """
    response.headers["Cache-Control"] = "no-store"


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _next_month(month: str) -> str:
    """The 'YYYY-MM' after a 'YYYY-MM' (quota messages name the reset month)."""
    year, mon = (int(part) for part in month.split("-"))
    return f"{year + (mon == 12)}-{mon % 12 + 1:02d}"


def _require_job_access(
    job: dict, actor: auth.Actor, *, detail: str = "Unknown job."
) -> None:
    """Owned jobs are invisible to everyone but their owner (and the founder).

    404 — not 403 — matching the path-traversal convention: another user's
    job must be indistinguishable from a job that does not exist, so `detail`
    mirrors each route's own unknown-job message. Anonymous jobs (user_id "")
    keep the pre-accounts behaviour exactly.
    """
    owner = str(job.get("user_id") or "")
    if owner and not actor.founder and actor.user_id != owner:
        raise HTTPException(status_code=404, detail=detail)


def _signed_in(user: dict) -> AuthResponse:
    """Mint a session for a user and shape the {token, user} response."""
    token = auth.new_session_token()
    db.create_session(
        auth.hash_session_token(token),
        user_id=user["id"],
        expires_at=auth.session_expires_at(),
    )
    return AuthResponse(
        token=token,
        user=AuthUserOut(
            id=user["id"],
            email=user["email"],
            plan=user["plan"],
            plan_status=user["plan_status"],
        ),
    )


def _auth_methods(user: dict) -> list[str]:
    """How this account can be signed into (LIBRARY.md Part 1, EMAILAUTH.md).

    Read off the row and off the server's own configuration rather than
    stored as a flag, so it can never drift from what the sign-in paths
    actually accept: a stored password hash is what `/v1/auth/login` needs, a
    stored `google_sub` is what `/v1/auth/google` matches on, and a configured
    mailer is what `/v1/auth/email/verify` needs — that last one is a property
    of the deployment, not of the row, because a code is sent to an ADDRESS
    and every account has one.

    "email" is therefore what a password-less, Google-less account (one this
    very flow created) reports, instead of the empty list that would leave the
    account page unable to say how its owner gets back in.
    """
    methods: list[str] = []
    if str(user.get("password_hash") or ""):
        methods.append("password")
    if str(user.get("google_sub") or ""):
        methods.append("google")
    if mailer.is_configured(get_settings()):
        methods.append("email")
    return methods


def _google_account(identity: googleid.GoogleIdentity) -> dict:
    """The account behind a verified Google identity: found, linked, or made.

    The order is the whole rule (LIBRARY.md Part 1):

      1. a row already carrying this `google_sub` — sign in;
      2. a row with this email and no `google_sub` — LINK and sign in. Google
         having verified the address is the same assurance a password reset
         would give, which is exactly why an unverified one never reaches
         here (googleid.verify_id_token refuses it);
      3. nothing — create a PASSWORD-LESS account (`password_hash = ''`).

    Steps 2 and 3 both race: two first-time sign-ins, or a registration
    landing between our read and our insert. The database arbitrates — the
    UNIQUE email and the partial UNIQUE `google_sub` index — so a lost race
    is re-resolved against whoever won rather than papered over.
    """
    for attempt in range(2):
        user = db.get_user_by_google_sub(identity.sub)
        if user is not None:
            return user
        existing = db.get_user_by_email(identity.email)
        if existing is not None:
            if db.link_google_sub(str(existing["id"]), identity.sub):
                return db.get_user_by_id(str(existing["id"])) or existing
            # Either this account already carries a DIFFERENT Google identity
            # or this identity just landed on another row. Re-read once; a
            # second failure means the email belongs to somebody whose Google
            # identity is not this one, and that must not sign anybody in.
            continue
        created = db.create_user(
            uuid.uuid4().hex,
            email=identity.email,
            password_hash="",
            google_sub=identity.sub,
        )
        if created is not None:
            return created
    raise HTTPException(status_code=409, detail=_GOOGLE_ACCOUNT_RACE)


def _email_code_account(email: str) -> dict:
    """The account a verified address signs into: found, or made (EMAILAUTH.md).

    Proving control of an inbox is the same assurance a password reset gives,
    which is what makes both branches safe:

      1. a row with this email — SIGN IN to it, and touch nothing else. A
         password account keeps its hash; a Google account keeps its
         `google_sub`. This adds a door, it does not change the locks, so
         nobody can use a code to quietly detach an identity from an account.
      2. nothing — create a PASSWORD-LESS account (`password_hash = ''`),
         exactly as a first Google sign-in does.

    Step 2 races a registration landing between our read and our insert. The
    UNIQUE email is the arbiter, so the loser re-reads and signs into whoever
    won rather than 500-ing or forking the person into two accounts.
    """
    for _ in range(2):
        user = db.get_user_by_email(email)
        if user is not None:
            return user
        created = db.create_user(uuid.uuid4().hex, email=email, password_hash="")
        if created is not None:
            return created
    raise HTTPException(status_code=409, detail=_EMAIL_CODE_ACCOUNT_RACE)


# --------------------------------------------------------------------------- #
# Brand kit storage (BRANDKIT.md §3a). The entitlement is re-read from the
# EFFECTIVE plan on every call, and the bytes are trusted for nothing: the type
# is sniffed, the size is measured after decoding, and the filename is built
# from the account id and a whitelisted extension — never from the upload.
# --------------------------------------------------------------------------- #


def _brand_logo_file(settings: Settings, user: dict) -> Path | None:
    """The account's stored logo as a real file, or None.

    A row naming a file that is no longer on disk reads as "no logo": the kit
    the client is shown must describe what a render would actually use.
    """
    path = brandkit.logo_abs_path(settings, str(user.get("brand_logo_path") or ""))
    return path if path is not None and path.is_file() else None


def _brand_out(settings: Settings, user: dict) -> BrandKitOut:
    """The stored kit as the client sees it — the logo as a URL, not bytes."""
    logo = _brand_logo_file(settings, user)
    return BrandKitOut(
        # Relative when public_base_url is empty, exactly like clip urls: the
        # SPA resolves it against the API origin it is already talking to.
        logo_url=f"{settings.public_base_url}/v1/me/brand/logo" if logo else None,
        caption_color=str(user.get("brand_caption_color") or "") or None,
        show_logo=bool(user.get("brand_show_logo", 1)),
    )


def _remove_brand_logos(settings: Settings, user_id: str) -> None:
    """Drop every stored logo for an account, whatever extension it carries.

    One account owns at most one logo, but a PNG replaced by a WebP would
    otherwise leave the old file behind — and the row would still be the only
    thing saying which of the two is current. Never raises: a kit must never
    break, and neither must its cleanup.
    """
    for content_type in brandkit.LOGO_EXTENSIONS:
        name = brandkit.logo_storage_name(user_id, content_type)
        path = brandkit.logo_abs_path(settings, name) if name else None
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _store_brand_logo(settings: Settings, user_id: str, data: bytes) -> str:
    """Write a validated logo and return the name to store on the user row."""
    content_type, error = brandkit.validate_logo_bytes(data)
    if error is not None:
        # 413 for "too big" and 400 for "not an image we can use" — the body is
        # the message the panel would have shown for the same file.
        oversize = len(data) > brandkit.MAX_LOGO_BYTES
        raise HTTPException(status_code=413 if oversize else 400, detail=error)
    assert content_type is not None
    name = brandkit.logo_storage_name(user_id, content_type)
    if name is None:  # an account id that could not make a safe filename
        raise HTTPException(
            status_code=400, detail="This account can't store a brand logo."
        )
    settings.ensure_dirs()
    _remove_brand_logos(settings, user_id)
    dest = settings.data_dir / name
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        part.write_bytes(data)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return name


def _decode_data_url(value: str) -> bytes | None:
    """The bytes inside a base64 ``data:`` URL, or None if it isn't one.

    The media type declared in the URL is read for nothing — the bytes are
    sniffed afterwards like any other upload. Only the base64 form is accepted,
    which is the only form ``FileReader.readAsDataURL`` produces.
    """
    match = _DATA_URL_RE.match(value.strip())
    if match is None or ";base64" not in (match.group(2) or "").lower():
        return None
    try:
        return base64.b64decode("".join(value[match.end() :].split()), validate=True)
    except (binascii.Error, ValueError):
        return None


def _form_flag(value: object, default: bool) -> bool:
    """An HTML form's idea of a boolean ("true"/"1"/"on"), else the default.

    A field that is absent OR empty takes the default: an empty string is what
    a form sends when it has nothing to say about a control, and reading that
    as "off" would hide a creator's logo because their panel omitted a value.
    """
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ("true", "1", "on", "yes")
    return default


async def _read_brand_body(request: Request) -> tuple[bytes | None, str, bool]:
    """Parse a brand PUT — multipart or JSON — into (logo bytes, colour, show).

    A PUT carries the WHOLE kit: an absent logo means the kit has none, not
    "keep whatever is on the server". The panel holds the complete kit locally
    and syncs it whole, and PUT that means anything else stops being PUT.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(status_code=411, detail="Content-Length header is required.")
    try:
        length = int(declared)
    except ValueError:
        raise HTTPException(
            status_code=411, detail="Content-Length header is invalid."
        ) from None
    if length > _BRAND_MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That logo is bigger than the {brandkit.format_mb(brandkit.MAX_LOGO_BYTES)}"
                " MB limit."
            ),
        )

    # Both form encodings go through the same parser: multipart carries the
    # file itself, urlencoded can still carry a data: URL and the two scalars.
    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        try:
            field = form.get("logo")
            if isinstance(field, UploadFile):
                data = await field.read()
            elif isinstance(field, str) and field.strip():
                data = _decode_data_url(field)
                if data is None:
                    raise HTTPException(status_code=400, detail=_BRAND_UNREADABLE_LOGO)
            else:
                data = None
            color = form.get("caption_color")
            return (
                data,
                color.strip() if isinstance(color, str) else "",
                _form_flag(form.get("show_logo"), default=True),
            )
        finally:
            await form.close()

    if content_type == "application/json":
        try:
            payload = BrandKitRequest.model_validate(await request.json())
        except (ValidationError, ValueError):
            raise HTTPException(
                status_code=400, detail="That brand kit payload isn't valid JSON."
            ) from None
        data = None
        if payload.logo and payload.logo.strip():
            data = _decode_data_url(payload.logo)
            if data is None:
                raise HTTPException(status_code=400, detail=_BRAND_UNREADABLE_LOGO)
        return data, (payload.caption_color or "").strip(), payload.show_logo

    raise HTTPException(status_code=415, detail=_BRAND_UNSUPPORTED_BODY)


# --------------------------------------------------------------------------- #
# Library shaping. `available` and `url` describe the FILE; everything else on
# a clip outlives it, so an expired card still carries its title, score, hooks
# and transcript rather than turning into a broken player.
# --------------------------------------------------------------------------- #


def _clip_fields(settings: Settings, clip: dict) -> dict:
    available = bool(clip.get("file_path"))
    return {
        "id": str(clip["id"]),
        "job_id": str(clip["job_id"] or ""),
        "clip_index": int(clip["clip_index"] or 0),
        "title": str(clip["title"] or ""),
        "score": int(clip["score"] or 0),
        "hooks": list(clip["hooks"] or []),
        "reason": str(clip["reason"] or ""),
        "tip": str(clip["tip"] or ""),
        "start": float(clip["start"] or 0.0),
        "end": float(clip["end"] or 0.0),
        "duration": float(clip["duration"] or 0.0),
        "width": int(clip["width"] or 0),
        "height": int(clip["height"] or 0),
        "speaker_count": int(clip["speaker_count"] or 0),
        "engine": str(clip["engine"] or "cloud"),
        "bytes": int(clip["bytes"] or 0),
        "created_at": str(clip["created_at"]),
        # '' is "never" in the row; None is "never" in JSON.
        "expires_at": str(clip["expires_at"] or "") or None,
        "available": available,
        # Relative when public_base_url is empty, exactly like clip and logo
        # urls: the SPA resolves it against the API origin it already talks to.
        "url": (
            f"{settings.public_base_url}/v1/clips/{clip['id']}/file"
            if available
            else None
        ),
    }


def _clip_summary(settings: Settings, clip: dict) -> ClipSummaryOut:
    return ClipSummaryOut(**_clip_fields(settings, clip))


def _clip_detail(settings: Settings, clip: dict) -> ClipDetailOut:
    """One clip with its transcript — the half that is permanent."""
    words: list[ClipWordOut] = []
    for word in clip.get("words") or []:
        if isinstance(word, dict):
            try:
                words.append(ClipWordOut.model_validate(word))
            except ValidationError:
                continue  # a word we can't read is not worth a 500
    return ClipDetailOut(**_clip_fields(settings, clip), words=words)


def _format_size(size: int) -> str:
    """A human size for a refusal — GB past a gigabyte, MB below it."""
    if size >= 1_000_000_000:
        value, unit = size / 1_000_000_000, "GB"
    else:
        value, unit = size / 1_000_000, "MB"
    return f"{f'{value:.2f}'.rstrip('0').rstrip('.')} {unit}"


def _parse_clip_metadata(raw: object) -> ClipUploadRequest:
    """The JSON metadata part of a clip upload, as a validated model.

    An absent part is allowed and means "no card": the FILE is the thing being
    saved, and refusing to store somebody's clip because its title was missing
    would lose the clip to protect a string.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ClipUploadRequest()
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=_CLIP_BAD_METADATA)
    if len(raw.encode("utf-8")) > _CLIP_METADATA_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "That clip's metadata is larger than the "
                f"{_format_size(_CLIP_METADATA_MAX_BYTES)} limit."
            ),
        )
    try:
        return ClipUploadRequest.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(status_code=400, detail=_CLIP_BAD_METADATA) from None


def _clip_download_name(clip: dict, extension: str) -> str:
    """A friendly filename for a downloaded clip.

    Built from the clip's INDEX, never from its title: a title is text the
    transcript produced, and a filename assembled out of it is a header we
    would be letting content write.
    """
    return f"clip-{int(clip.get('clip_index') or 0) + 1:02d}.{extension}"


def _clip_file_response(settings: Settings, clip: dict) -> Response:
    """Serve a library clip's file, or 404 honestly once it has expired."""
    file_path = str(clip.get("file_path") or "")
    if not file_path:
        raise HTTPException(status_code=404, detail=_CLIP_EXPIRED)
    storage = get_storage(settings)
    path = storage.library_clip_file(file_path)
    if path is not None:
        media_type = clip_media_type(path.name)
        return FileResponse(
            path,
            media_type=media_type,
            filename=_clip_download_name(clip, path.suffix.lstrip(".") or "mp4"),
            # One account's private video: a shared cache must never keep a
            # copy, and the browser must revalidate rather than serve one whose
            # retention window has since closed.
            headers={"Cache-Control": "private, max-age=0"},
        )
    # Nothing local to serve: the backend hands out its own (presigned) link.
    url = storage.library_clip_url(file_path)
    if url:
        return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail=_CLIP_EXPIRED)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()
    # Fail crash-stranded jobs on boot, handing their quota reservations back.
    # The hourly beat sweep runs the same reconciliation, so a box whose API
    # process never restarts is covered too (worker.reconcile_stalled).
    reconcile_stalled()
    # Expired sessions are dead weight — clear them on every boot.
    db.purge_expired_sessions()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    # A box that cannot enforce what it sells must not boot (settings.validate
    # explains each refusal); this runs before a single route is registered.
    settings.validate()
    # The interactive docs are a dev convenience: they enumerate every route
    # and schema. A configured CC_API_TOKEN is the "this box is public" signal,
    # so /docs, /redoc and /openapi.json switch off with it.
    public_docs = not settings.api_token
    app = FastAPI(
        title="ClipCatalyst Cloud API",
        version=API_VERSION,
        lifespan=_lifespan,
        docs_url="/docs" if public_docs else None,
        redoc_url="/redoc" if public_docs else None,
        openapi_url="/openapi.json" if public_docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(_build_router())
    return app


def _build_router():  # noqa: ANN202 - APIRouter
    from fastapi import APIRouter

    router = APIRouter(prefix="/v1")

    @router.post("/auth/register", response_model=AuthResponse, status_code=201)
    def register(body: RegisterRequest, request: Request, response: Response) -> AuthResponse:
        _no_store(response)
        _rate_limit(request, "register")
        email = auth.normalize_email(body.email)
        if not auth.is_valid_email(email):
            raise HTTPException(
                status_code=400,
                detail="That doesn't look like a valid email address.",
            )
        user = db.create_user(
            uuid.uuid4().hex,
            email=email,
            password_hash=auth.hash_password(body.password),
        )
        if user is None:
            # 409 is a user-exists oracle; accepted tradeoff for a product
            # signup form (ACCOUNTS.md) — the login ERROR path stays generic.
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists — sign in instead.",
            )
        return _signed_in(user)

    @router.post("/auth/login", response_model=AuthResponse)
    def login(body: LoginRequest, request: Request, response: Response) -> AuthResponse:
        _no_store(response)
        _rate_limit(request, "login")
        user = db.get_user_by_email(auth.normalize_email(body.email))
        stored = user["password_hash"] if user is not None else ""
        # A PASSWORD-LESS account — one created by Sign in with Google, whose
        # row carries `password_hash = ''` (LIBRARY.md Part 1) — is refused
        # here, and refused as a decision rather than as a side effect of
        # comparing against an empty hash. `auth.verify_password` says no to
        # an empty stored value too; this says no before it is ever asked.
        password_login = bool(stored)
        # One generic 401 for unknown email, password-less account and wrong
        # password alike, and scrypt runs in every one of those cases (the
        # dummy hash stands in where there is no real one), so neither the
        # body nor the timing tells a stranger which case they hit.
        if not password_login:
            stored = auth.dummy_password_hash()
        if not auth.verify_password(body.password, stored) or user is None or not password_login:
            raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
        return _signed_in(user)

    @router.post("/auth/google", response_model=AuthResponse)
    def auth_google(
        body: GoogleAuthRequest, request: Request, response: Response
    ) -> AuthResponse:
        """Sign in with a Google ID token (LIBRARY.md Part 1).

        Identity only: Google says who this is, and the session that comes
        out is the same session `/v1/auth/login` mints — same TTL, same
        cookie-less bearer, same `no-store`.
        """
        _no_store(response)
        # Its own window, like every other credential route, and for a
        # sharper reason than most: a forged token costs nothing to send and
        # costs us an RS256 verify (and, on an unknown `kid`, a fetch) to
        # refuse.
        _rate_limit(request, "google")
        settings = get_settings()
        if not settings.google_client_id:
            # 503, not 501/400: a deployment state, exactly like billing off.
            raise HTTPException(status_code=503, detail=_GOOGLE_SIGN_IN_OFF)
        try:
            identity = googleid.verify_id_token(body.id_token, settings.google_client_id)
        except googleid.KeysUnavailable as error:
            # We could not check, which is not the same as "you are refused".
            logger.warning("google sign-in unavailable: %s", error)
            raise HTTPException(
                status_code=503, detail=_GOOGLE_KEYS_UNAVAILABLE
            ) from None
        except googleid.InvalidIdToken as error:
            logger.info("google sign-in refused: %s", error)
            raise HTTPException(
                status_code=401, detail=_GOOGLE_SIGN_IN_FAILED
            ) from None
        return _signed_in(_google_account(identity))

    @router.post("/auth/email/start", response_model=EmailCodeStartResponse)
    def email_code_start(
        body: EmailCodeStartRequest, request: Request, response: Response
    ) -> EmailCodeStartResponse:
        """Mail a 6-digit sign-in code to an address (EMAILAUTH.md).

        The response is the same 200 `{sent: true}` whether or not the address
        has an account — and it is the same because this route NEVER LOOKS.
        The account is resolved at verify time, so there is no branch here to
        take, nothing to time, and nothing an attacker can measure: uniformity
        is a property of the control flow rather than a pair of responses
        somebody remembered to keep in step.

        Two independent limits guard it, and each stops an attack the other
        does not (auth.enforce_email_code_limit says which is which).
        """
        _no_store(response)
        # Per CLIENT: one machine cannot farm codes. Counted first and for
        # every caller, including the ones about to be refused below, so a
        # flood of malformed requests is metered too.
        _rate_limit(request, "email-start")
        settings = get_settings()
        if not mailer.is_configured(settings):
            # 503, exactly like Google sign-in with no client id: a deployment
            # state, and never a pretence that mail went out.
            raise HTTPException(status_code=503, detail=_EMAIL_CODE_OFF)
        email = auth.normalize_email(body.email)
        if not auth.is_valid_email(email):
            # About the STRING, not about any account — an address that cannot
            # receive mail cannot be sent a code, and saying so reveals
            # nothing about who has an account here.
            raise HTTPException(
                status_code=400,
                detail="That doesn't look like a valid email address.",
            )
        # Per MAILBOX: nobody can be mail-bombed by a stranger typing their
        # address over and over, however many machines it comes from — and
        # however many ways they spell it, since one inbox answers to every
        # `+tag`, and to dotted and trailing-dot variants (auth.mailbox_key).
        # That folding is for the COUNTER only. `email` below stays the
        # identity form, so `alex+work@` and `alex@` remain separate accounts
        # with separate codes; the two must not be collapsed into one notion.
        auth.enforce_email_code_limit(email)

        code = auth.new_login_code()
        # Stored BEFORE the send, and only ever as a hash. If it were stored
        # after, a fast mail provider could put the code in somebody's hand
        # before the row it verifies against exists.
        db.put_login_code(
            email,
            code_hash=auth.hash_login_code(email, code),
            expires_at=auth.login_code_expires_at(),
        )
        try:
            mailer.send_login_code(settings, email, code)
        except mailer.MailError as error:
            # Nothing was sent, so nothing may remain valid: drop the staged
            # row before answering. Leaving it would mean a code exists that
            # nobody can ever receive — an account with a live credential in
            # limbo — and it would count against a later, real request.
            db.delete_login_code(email)
            logger.warning("could not send a sign-in code: %s", error)
            raise HTTPException(
                status_code=503, detail=_EMAIL_CODE_SEND_FAILED
            ) from None
        return EmailCodeStartResponse()

    @router.post("/auth/email/verify", response_model=AuthResponse)
    def email_code_verify(
        body: EmailCodeVerifyRequest, request: Request, response: Response
    ) -> AuthResponse:
        """Trade a 6-digit code for a session — the same session login mints.

        Every refusal below is the SAME 401 with the same body. A code that is
        wrong, one that expired, one whose guesses are spent and one that was
        never requested are indistinguishable from outside, because telling
        them apart tells a stranger which addresses have codes in flight.
        """
        _no_store(response)
        # Per client again: this is the route a guesser would grind, and the
        # attempt cap below only ever protects ONE code — the limiter is what
        # keeps somebody from spending five guesses on code after code.
        _rate_limit(request, "email-verify")
        email = auth.normalize_email(body.email)
        # A code that was never requested and one whose TTL has run out are the
        # same None here: db.get_login_code checks the expiry in SQL, the way
        # sessions are checked, so the deadline is enforced by this read rather
        # than by whether the hourly sweep has got around to the row.
        row = db.get_login_code(email)
        if row is None:
            raise HTTPException(status_code=401, detail=_EMAIL_CODE_FAILED)
        if not auth.login_code_matches(email, body.code, str(row["code_hash"])):
            # A wrong guess costs one of the five this code will ever accept;
            # db.count_login_code_attempt deletes the row at the cap, in the
            # same transaction, so two simultaneous guesses cannot walk past it.
            attempts = db.count_login_code_attempt(
                email, max_attempts=auth.LOGIN_CODE_MAX_ATTEMPTS
            )
            logger.info(
                "sign-in code refused (%d of %d attempts spent)",
                attempts,
                auth.LOGIN_CODE_MAX_ATTEMPTS,
            )
            raise HTTPException(status_code=401, detail=_EMAIL_CODE_FAILED)
        # Right code — spend it. The DELETE names the hash it expects, so this
        # is where single use is decided: if another request already consumed
        # the row, this one loses and is refused like any other bad code
        # rather than minting a second session from one secret.
        if not db.consume_login_code(email, str(row["code_hash"])):
            raise HTTPException(status_code=401, detail=_EMAIL_CODE_FAILED)
        return _signed_in(_email_code_account(email))

    @router.post("/auth/logout", response_model=LogoutResponse)
    def logout(
        response: Response,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> LogoutResponse:
        _no_store(response)
        auth.require_session(authorization)  # 401 unless the session is live
        raw = auth.bearer_session_token(authorization)
        assert raw is not None  # require_session guarantees a session bearer
        db.revoke_session(auth.hash_session_token(raw))
        return LogoutResponse()

    @router.get("/me", response_model=MeResponse)
    def me(
        response: Response, user: dict = Depends(auth.require_session)
    ) -> MeResponse:
        _no_store(response)
        # `plan`/`plan_status` are the stored subscription facts; quota and
        # entitlements come from the EFFECTIVE plan (canceled → free), read
        # from the same table enforcement uses, so this can never disagree
        # with what the server actually allows.
        entitlements = PLANS[effective_plan(user)]
        month = _current_month()
        return MeResponse(
            email=user["email"],
            plan=user["plan"],
            plan_status=user["plan_status"],
            # Password, Google, or both — read off the row, so the account
            # page can say how someone signs in and never offer "change
            # password" on an account that has none.
            auth_methods=_auth_methods(user),
            quota=QuotaOut(
                limit=entitlements.clips_per_month,
                used=db.get_usage(user["id"], month),
                month=month,
            ),
            entitlements=EntitlementsOut(
                max_height=entitlements.max_height,
                watermark_required=entitlements.watermark_required,
                clips_per_month=entitlements.clips_per_month,
                brand_kit=entitlements.brand_kit,
                # How long saved clips are KEPT — the account page's retention
                # line, read from the same plan the reaper enforces.
                retention_days=entitlements.retention_days,
            ),
            # The stored kit, always — a kit outlives a downgrade on the row
            # (nothing deletes a creator's logo because they changed plan) and
            # `entitlements.brand_kit` above says whether a render will use it.
            brand=_brand_out(get_settings(), user),
        )

    def _require_brand_kit(user: dict) -> None:
        """403 unless the EFFECTIVE plan carries a brand kit.

        Read from the plan table enforcement uses, not from the request or the
        stored row, so a lapsed subscription stops being able to change what
        renders the moment it lapses.
        """
        if not PLANS[effective_plan(user)].brand_kit:
            raise HTTPException(status_code=403, detail=_BRAND_KIT_FORBIDDEN)

    @router.put("/me/brand", response_model=BrandKitOut)
    async def put_brand(
        request: Request,
        response: Response,
        user: dict = Depends(auth.require_session),
    ) -> BrandKitOut:
        """Replace this account's brand kit (multipart or JSON).

        Nothing the client sends is trusted beyond the bytes themselves: the
        content type is SNIFFED from those bytes, the size is measured after
        decoding, the colour is re-validated with the same rule as the
        TypeScript, and the file lands under a name built from the account id
        and a whitelisted extension.
        """
        _no_store(response)
        settings = get_settings()
        _require_brand_kit(user)
        data, raw_color, show_logo = await _read_brand_body(request)

        color = ""
        if raw_color:
            color = brandkit.normalize_hex(raw_color) or ""
            if not color:
                raise HTTPException(status_code=400, detail=_BRAND_BAD_COLOR)

        if data is None:
            _remove_brand_logos(settings, user["id"])
            stored = ""
        else:
            stored = _store_brand_logo(settings, user["id"], data)

        db.update_user(
            user["id"],
            brand_logo_path=stored,
            brand_caption_color=color,
            brand_show_logo=int(show_logo),
        )
        return _brand_out(settings, db.get_user_by_id(user["id"]) or user)

    @router.delete("/me/brand", response_model=BrandKitOut)
    def delete_brand(
        response: Response, user: dict = Depends(auth.require_session)
    ) -> BrandKitOut:
        """Clear the stored kit — logo file included.

        Deliberately NOT gated on the plan: taking your own logo off our
        servers must not depend on what you are paying today, or a downgrade
        would strand it there with no way to remove it.
        """
        _no_store(response)
        settings = get_settings()
        _remove_brand_logos(settings, user["id"])
        db.update_user(
            user["id"],
            brand_logo_path="",
            brand_caption_color="",
            brand_show_logo=1,
        )
        return BrandKitOut()

    @router.get("/me/brand/logo")
    def get_brand_logo(user: dict = Depends(auth.require_session)) -> FileResponse:
        """The account's own logo, for the panel's preview on a new device."""
        settings = get_settings()
        path = _brand_logo_file(settings, user)
        if path is None:
            raise HTTPException(status_code=404, detail="No brand logo stored.")
        media_type = brandkit.LOGO_MEDIA_TYPES.get(path.suffix.lstrip("."))
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                # One account's private asset: a shared cache must not keep it,
                # and the browser must revalidate rather than serve a logo the
                # owner has since replaced.
                "Cache-Control": "private, max-age=0",
                # An uploaded SVG is markup we serve from our own origin. The
                # session bearer means it cannot be navigated to with
                # credentials, and these two headers close the rest: no
                # sniffing into something executable, no subresources or
                # scripts if it is ever opened directly.
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    # --------------------------------------------------------------------- #
    # The clip library (LIBRARY.md Part 2). Session-only and owner-scoped
    # throughout: somebody else's clip is a 404, never a 403 — the same
    # convention the job routes use, so a clip id tells a stranger nothing.
    # --------------------------------------------------------------------- #

    def _owned_clip(clip_id: str, user: dict) -> dict:
        clip = db.get_clip(clip_id)
        if clip is None or str(clip["user_id"]) != str(user["id"]):
            raise HTTPException(status_code=404, detail=_CLIP_UNKNOWN)
        return clip

    @router.get("/clips", response_model=ClipListResponse)
    def list_library(
        response: Response,
        limit: int = Query(default=20, ge=1, le=50),
        before: str = Query(default="", max_length=128),
        user: dict = Depends(auth.require_session),
    ) -> ClipListResponse:
        """This account's clips, newest first.

        The cursor is opaque to the client: it hands back whatever
        `next_before` said. Internally it is the last item's `created_at` plus
        its id, because one render writes several clips in the same
        millisecond and a page boundary inside such a group would otherwise
        skip the rest of it.
        """
        _no_store(response)
        settings = get_settings()
        cursor, _, cursor_id = before.partition("|")
        clips = db.list_clips(
            str(user["id"]), limit=limit, before=cursor, before_id=cursor_id
        )
        # Only when the page came back full: a short page IS the end, and
        # handing out a cursor for it would cost the client a wasted round trip.
        next_before = None
        if len(clips) == limit:
            last = clips[-1]
            next_before = f"{last['created_at']}|{last['id']}"
        return ClipListResponse(
            clips=[_clip_summary(settings, clip) for clip in clips],
            next_before=next_before,
        )

    @router.post("/clips/upload", response_model=ClipDetailOut, status_code=201)
    async def upload_library_clip(
        request: Request,
        response: Response,
        user: dict = Depends(auth.require_session),
    ) -> ClipDetailOut:
        """Save a clip the BROWSER rendered (LIBRARY.md Part 2).

        Deliberately an upload rather than a side effect: the site promises the
        video never leaves the device, so this only ever runs because somebody
        clicked "Save to library".

        It costs no monthly quota — nothing was rendered on our hardware, and
        billing somebody for their own laptop's work would be a lie — which is
        exactly why it needs its own ceiling: `CC_LIBRARY_MAX_BYTES` per
        account, so a free plan cannot be turned into a disk. The container is
        SNIFFED from the bytes, the size is counted as it streams, and `engine`
        is forced to 'browser' whatever the metadata claims.
        """
        _no_store(response)
        settings = get_settings()
        content_type = (
            (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        )
        if content_type != "multipart/form-data":
            raise HTTPException(status_code=415, detail=_CLIP_UNSUPPORTED_BODY)
        declared = request.headers.get("content-length")
        if declared is None:
            raise HTTPException(
                status_code=411, detail="Content-Length header is required."
            )
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(
                status_code=411, detail="Content-Length header is invalid."
            ) from None
        oversize = (
            f"That clip is larger than the {_format_size(settings.max_clip_bytes)} "
            "limit for a saved clip."
        )
        # The envelope (boundaries + the metadata part) rides on top of the
        # file, so the body limit is the file limit plus slack; what actually
        # counts is the byte count below, measured on the file itself.
        if length > settings.max_clip_bytes + _CLIP_BODY_SLACK_BYTES:
            raise HTTPException(status_code=413, detail=oversize)

        # Checked BEFORE a byte is read as well as after the file is measured:
        # an account already at its ceiling deserves to be told so, not after
        # uploading 200 MB.
        stored_bytes = db.library_bytes(str(user["id"]))
        full = (
            f"Your library is full — the limit is "
            f"{_format_size(settings.library_max_bytes)} of stored clips, and "
            f"{_format_size(stored_bytes)} is in use. Delete a clip to make "
            "room, or let one expire."
        )
        if stored_bytes >= settings.library_max_bytes:
            raise HTTPException(status_code=402, detail=full)

        settings.ensure_dirs()
        clip_id = uuid.uuid4().hex
        # Staged in tmp first so the ceiling, the size and the container are
        # all decided before anything lands under the library root.
        staging = settings.tmp_dir / f"library-{clip_id}.part"
        form = await request.form()
        try:
            raw_metadata: object = form.get("metadata")
            if isinstance(raw_metadata, UploadFile):
                raw_metadata = (
                    await raw_metadata.read(_CLIP_METADATA_MAX_BYTES + 1)
                ).decode("utf-8", "replace")
            metadata = _parse_clip_metadata(raw_metadata)

            field = form.get("file")
            if not isinstance(field, UploadFile):
                raise HTTPException(status_code=400, detail=_CLIP_UNSUPPORTED_BODY)
            head = b""
            size = 0
            with staging.open("wb") as fh:
                while True:
                    chunk = await field.read(_CLIP_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:64]
                    size += len(chunk)
                    if size > settings.max_clip_bytes:
                        raise HTTPException(status_code=413, detail=oversize)
                    fh.write(chunk)

            extension = _sniff_clip_type(head)
            if extension is None:
                raise HTTPException(status_code=400, detail=_CLIP_NOT_A_VIDEO)
            if stored_bytes + size > settings.library_max_bytes:
                raise HTTPException(status_code=402, detail=full)

            file_path = get_storage(settings).put_library_clip(
                str(user["id"]), f"{clip_id}.{extension}", staging
            )
        finally:
            # Whatever happened — a refusal, a crash, or the copy landing —
            # the staging file has served its purpose and never outlives the
            # request that made it.
            staging.unlink(missing_ok=True)
            await form.close()

        clip = db.create_clip(
            clip_id,
            user_id=str(user["id"]),
            # No job: this clip was never on our hardware. The column stays ''
            # so nothing ever tries to resolve it against a jobs row.
            job_id="",
            clip_index=metadata.clip_index,
            title=metadata.title,
            score=metadata.score,
            hooks=list(metadata.hooks),
            reason=metadata.reason,
            tip=metadata.tip,
            start=metadata.start,
            end=metadata.end,
            duration=metadata.duration,
            width=metadata.width,
            height=metadata.height,
            speaker_count=metadata.speaker_count,
            words=[word.model_dump() for word in metadata.words],
            # Forced, never read from the payload: `engine` says where the
            # pixels came from, and only the server knows that.
            engine="browser",
            file_path=file_path,
            # The bytes we actually wrote, not a number the client sent — this
            # one feeds the storage ceiling.
            size_bytes=size,
            retention_days=PLANS[effective_plan(user)].retention_days,
        )
        return _clip_detail(settings, clip)

    @router.get("/clips/{clip_id}", response_model=ClipDetailOut)
    def get_library_clip(
        clip_id: str,
        response: Response,
        user: dict = Depends(auth.require_session),
    ) -> ClipDetailOut:
        _no_store(response)
        return _clip_detail(get_settings(), _owned_clip(clip_id, user))

    @router.delete("/clips/{clip_id}", response_model=ClipDeletedResponse)
    def delete_library_clip(
        clip_id: str,
        response: Response,
        user: dict = Depends(auth.require_session),
    ) -> ClipDeletedResponse:
        """Remove a clip: the row AND the file.

        The only thing that deletes a library row — retention deletes files and
        keeps rows. The file goes FIRST: the row is the only pointer to it, so
        dropping the row on a failed unlink would strand the video on our disk
        with nothing left that knows it is there. A failure leaves both in
        place, so trying again finishes the job.
        """
        _no_store(response)
        settings = get_settings()
        clip = _owned_clip(clip_id, user)
        file_path = str(clip.get("file_path") or "")
        if file_path:
            try:
                get_storage(settings).delete_library_clip(file_path)
            except Exception:
                logger.exception("clip %s: could not delete its file", clip_id)
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "That clip couldn't be deleted just now — please try "
                        "again in a moment."
                    ),
                ) from None
        db.delete_clip(clip_id)
        return ClipDeletedResponse()

    @router.get("/clips/{clip_id}/file")
    def get_library_clip_file(
        clip_id: str, user: dict = Depends(auth.require_session)
    ) -> Response:
        """The saved video itself — owner only, 404 once it has expired."""
        return _clip_file_response(get_settings(), _owned_clip(clip_id, user))

    def _require_gateway() -> billing.Gateway:
        """The configured billing gateway, or an honest 503.

        503 (not 501/400) because this is a deployment state, not a client
        mistake: billing is off, or stripe mode is missing its secret key.
        """
        settings = get_settings()
        gateway = billing.get_gateway(settings)
        if gateway is None:
            raise HTTPException(status_code=503, detail=_BILLING_OFF)
        if settings.billing == "stripe" and not settings.stripe_secret_key:
            raise HTTPException(
                status_code=503,
                detail="Stripe is not fully configured (CC_STRIPE_SECRET_KEY is unset).",
            )
        return gateway

    @router.post("/billing/checkout", response_model=CheckoutResponse)
    def billing_checkout(
        body: CheckoutRequest,
        request: Request,
        response: Response,
        user: dict = Depends(auth.require_session),
    ) -> CheckoutResponse:
        _no_store(response)
        # Each checkout mints a Stripe Checkout Session (a real API call in
        # stripe mode, and a customer record on first use), so it gets the same
        # per-client window as the credential routes.
        _rate_limit(request, "checkout")
        settings = get_settings()
        gateway = _require_gateway()
        plan = body.plan.strip().lower()
        # The client names a PLAN; the server maps it to a price id. Nothing
        # in this request can change the account's plan — only the verified
        # webhook that follows a completed checkout does that.
        if plan not in PLANS or plan == "free":
            raise HTTPException(
                status_code=400,
                detail="Pick a paid plan to upgrade to: starter, pro, or enterprise.",
            )
        if settings.billing == "stripe" and not billing.price_id_for(settings, plan):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"The {plan} plan has no Stripe price configured "
                    f"(CC_STRIPE_PRICE_{plan.upper()} is unset)."
                ),
            )
        return CheckoutResponse(url=gateway.create_checkout(user, plan))

    @router.post("/billing/portal", response_model=PortalResponse)
    def billing_portal(user: dict = Depends(auth.require_session)) -> PortalResponse:
        gateway = _require_gateway()
        if not user["stripe_customer_id"]:
            raise HTTPException(
                status_code=400,
                detail="No billing profile yet — upgrade to a paid plan first.",
            )
        return PortalResponse(url=gateway.create_portal(user))

    @router.post("/billing/webhook", response_model=WebhookAckResponse)
    async def billing_webhook(request: Request) -> WebhookAckResponse:
        # Server-to-server, no session/founder auth: authenticity comes from
        # the Stripe-Signature over the RAW body, verified in EVERY mode where
        # billing is on (fake included) — there is no code path that applies
        # an unverified event.
        settings = get_settings()
        if settings.billing == "off":
            raise HTTPException(status_code=503, detail=_BILLING_OFF)
        if not settings.stripe_webhook_secret:
            raise HTTPException(
                status_code=503,
                detail="Webhooks are not configured (CC_STRIPE_WEBHOOK_SECRET is unset).",
            )
        signature = request.headers.get("Stripe-Signature")
        if not signature:
            raise HTTPException(
                status_code=400, detail="Missing Stripe-Signature header."
            )
        payload = await request.body()
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, settings.stripe_webhook_secret
            )
        except stripe.SignatureVerificationError:
            raise HTTPException(
                status_code=400, detail="Invalid Stripe-Signature."
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Malformed webhook payload."
            ) from None
        # Unknown event types return 200 too — acknowledged, ignored — so
        # Stripe never retries events this server doesn't act on. to_dict()
        # flattens the StripeObject into the plain nested dicts billing reads.
        billing.apply_event(event.to_dict(), settings)
        return WebhookAckResponse()

    @router.post("/jobs", response_model=CreateJobResponse, status_code=201)
    def create_job(
        body: CreateJobRequest, actor: auth.Actor = Depends(auth.require_actor)
    ) -> CreateJobResponse:
        settings = get_settings()
        if body.size_bytes > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File is too large — the limit is "
                    f"{settings.max_upload_bytes} bytes."
                ),
            )
        # Entitlements only ever TIGHTEN the request, and only server-side
        # facts feed them: a session's height clamps to its effective plan's
        # ceiling and watermark_required plans keep the watermark. Founder/dev
        # jobs keep the pre-accounts behaviour — full height, watermarked, as
        # cloud renders always were. (Every plan max_height is itself a valid
        # height literal, so min() lands on a renderable value.)
        height = body.height
        watermark = True
        if actor.user is not None:
            entitlements = PLANS[effective_plan(actor.user)]
            height = min(body.height, entitlements.max_height)
            watermark = entitlements.watermark_required
        job_id = uuid.uuid4().hex
        db.create_job(
            job_id,
            filename=body.filename,
            size_bytes=body.size_bytes,
            target_length=body.target_length,
            count=body.count,
            height=height,
            # Session-created jobs are owned; founder/dev jobs stay anonymous.
            user_id=actor.user_id,
        )
        target = get_storage(settings).upload_target(job_id)
        return CreateJobResponse(
            job_id=job_id,
            upload=UploadTargetOut(**target),
            height=height,
            watermark=watermark,
        )

    @router.put("/uploads/{job_id}", response_model=UploadAckResponse)
    async def upload_source(
        job_id: str,
        request: Request,
        actor: auth.Actor = Depends(auth.require_actor),
    ) -> UploadAckResponse:
        settings = get_settings()
        if settings.storage != "local":
            raise HTTPException(
                status_code=404,
                detail="Direct uploads are disabled — use the presigned S3 URL.",
            )
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        # Ownership before any state leaks: a foreign caller must not even
        # learn whether the job is still accepting an upload.
        _require_job_access(job, actor)
        if job["status"] != "awaiting_upload":
            raise HTTPException(
                status_code=409,
                detail=f"Job is {job['status']} — it is no longer accepting an upload.",
            )

        content_length = request.headers.get("content-length")
        if content_length is None:
            raise HTTPException(
                status_code=411, detail="Content-Length header is required."
            )
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=411, detail="Content-Length header is invalid."
            ) from None
        if declared > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload is too large — the limit is "
                    f"{settings.max_upload_bytes} bytes."
                ),
            )

        settings.ensure_dirs()
        storage = get_storage(settings)
        dest = storage.source_path(job_id)
        part = dest.with_suffix(dest.suffix + ".part")
        received = 0
        try:
            with part.open("wb") as fh:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Upload is too large — the limit is "
                                f"{settings.max_upload_bytes} bytes."
                            ),
                        )
                    fh.write(chunk)
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        db.update_job(job_id, size_bytes=received, detail="Upload received")
        return UploadAckResponse(ok=True)

    @router.post("/jobs/{job_id}/start", response_model=StartJobResponse, status_code=202)
    def start_job(
        job_id: str, actor: auth.Actor = Depends(auth.require_actor)
    ) -> StartJobResponse:
        settings = get_settings()
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        _require_job_access(job, actor)
        if not get_storage(settings).source_exists(job_id):
            raise HTTPException(
                status_code=409, detail="Upload the video before starting the job."
            )

        # The quota rides on the job's OWNER (usage is billed to them however
        # the start arrives); anonymous founder/dev jobs have no owner and no
        # quota. A deleted owner leaves the job unmetered, as before.
        owner_id = str(job.get("user_id") or "")
        owner = db.get_user_by_id(owner_id) if owner_id else None

        # Atomically claim the job so concurrent / duplicate starts can't both
        # enqueue it — only the caller that wins the awaiting_upload→queued move
        # gets to dispatch the pipeline task. The claim comes FIRST so a start
        # that loses it (409) never touches the owner's quota.
        started = db.transition_status(
            job_id,
            expect="awaiting_upload",
            to="queued",
            stage="",
            progress=0.0,
            detail="Queued",
            error=None,
        )
        if not started:
            raise HTTPException(status_code=409, detail="Job already started.")

        # Monthly quota: RESERVED here, not merely read. The counter moves for
        # the whole job before anything is queued, so jobs that are already in
        # flight (on a real broker /start returns long before they render)
        # count against the ceiling instead of every one of them reading the
        # same pre-render number. The worker settles the reservation against
        # what actually rendered, and returns all of it if the job fails.
        if owner is not None:
            count = int(job["count"])
            plan_name = effective_plan(owner)
            limit = PLANS[plan_name].clips_per_month
            month = _current_month()
            if not db.reserve_usage(job_id, owner_id, month, count, limit=limit):
                # Nothing was spent, so give the claim back: a 402 must leave
                # the job startable once the month rolls over or the plan grows.
                db.transition_status(
                    job_id,
                    expect="queued",
                    to="awaiting_upload",
                    detail="Upload received",
                )
                raise HTTPException(
                    status_code=402,
                    detail=(
                        f"Monthly clip limit reached — the {plan_name} "
                        f"plan includes {limit} clips per month, "
                        f"{db.get_usage(owner_id, month)} are used for {month}, "
                        f"and this job would render {count} more. The "
                        f"counter resets in {_next_month(month)}."
                    ),
                )

        try:
            # In eager mode (CC_QUEUE=eager) this runs the whole pipeline inline.
            process_job.delay(job_id)
        except Exception:
            # The task never reached the broker, so nothing will ever settle
            # this reservation — hand the quota back here. The job stays
            # claimed (a phantom double-render is worse than a stranded row,
            # which reconcile_stalled fails on the next boot).
            db.settle_usage(job_id, 0)
            raise
        return StartJobResponse(job_id=job_id, status="queued")

    @router.get("/jobs/{job_id}", response_model=JobStatusResponse)
    def get_job_status(
        job_id: str, actor: auth.Actor = Depends(auth.optional_actor)
    ) -> JobStatusResponse:
        # Status polling stays credential-free for anonymous jobs; an OWNED
        # job is visible only to its owner's session (or the founder token).
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        _require_job_access(job, actor)
        return JobStatusResponse(
            job_id=job["id"],
            status=job["status"],
            stage=job["stage"] or "",
            progress=float(job["progress"] or 0.0),
            detail=job["detail"] or "",
            error=job["error"],
            clips=[ClipOut(**clip) for clip in job["clips"]],
        )

    @router.get("/files/{job_id}/{name}")
    def get_file(
        job_id: str, name: str, actor: auth.Actor = Depends(auth.require_actor)
    ) -> Response:
        settings = get_settings()
        clips_dir = settings.clips_dir
        # Guard traversal: job_id must be a bare 32-hex uuid and name a plain
        # safe filename. This rejects "..", "%2E%2E", and any decoded slash
        # before we ever touch the filesystem.
        if (
            not _JOB_ID_RE.match(job_id)
            or name in (".", "..")
            or not _SAFE_NAME_RE.match(name)
        ):
            raise HTTPException(status_code=404, detail="Not found.")
        # Ownership rides on the job row while there is one.
        job = db.get_job(job_id)
        if job is not None:
            _require_job_access(job, actor, detail="Not found.")
            # Defence in depth: the resolved path must stay inside clips_dir.
            resolved = (clips_dir / job_id / name).resolve()
            if resolved.is_relative_to(clips_dir.resolve()) and resolved.is_file():
                media_type = "video/mp4" if name.endswith(".mp4") else None
                # A rendered clip is one account's private video: shared caches
                # must never keep a copy, and the browser must revalidate
                # rather than serve it after the job is reaped or the session
                # changes hands.
                return FileResponse(
                    resolved,
                    media_type=media_type,
                    filename=name,
                    headers={"Cache-Control": "private, max-age=0"},
                )
        # The job's own copy is gone — reaped at CC_JOB_TTL_HOURS, row and all.
        # The LIBRARY keeps its own copy for the owner's whole retention
        # window, so a link the account already holds keeps working long after
        # the job that made it stopped existing. Scoped to the caller's own
        # account, so this can only ever find their clip (db.get_clip_by_file),
        # and the founder token — which owns no library — finds nothing.
        clip = db.get_clip_by_file(actor.user_id, f"{actor.user_id}/{job_id}/{name}")
        if clip is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return _clip_file_response(settings, clip)

    @router.get("/healthz", response_model=HealthzResponse)
    def healthz() -> HealthzResponse:
        settings = get_settings()
        return HealthzResponse(
            ok=True,
            version=API_VERSION,
            queue=settings.queue,
            storage=settings.storage,
            transcriber=settings.transcriber,
        )

    return router


app = create_app()
