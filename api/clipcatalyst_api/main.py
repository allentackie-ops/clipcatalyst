"""FastAPI app: accounts/auth, job creation, uploads, start, status, files."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError

# The form parser builds Starlette's UploadFile; FastAPI's is a SUBCLASS used
# for signature-declared params, so an isinstance check against it silently
# misses every part of a hand-parsed form (and quietly cleared the logo).
from starlette.datastructures import UploadFile

from . import auth, billing, brandkit, db
from .models import (
    AuthResponse,
    AuthUserOut,
    BrandKitOut,
    BrandKitRequest,
    CheckoutRequest,
    CheckoutResponse,
    ClipOut,
    CreateJobRequest,
    CreateJobResponse,
    EntitlementsOut,
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
from .storage import get_storage
from .worker import process_job, reconcile_stalled

API_VERSION = "0.1.0"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOGIN_FAILED = "Incorrect email or password."
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
        # One generic 401 for unknown email and wrong password alike, and
        # scrypt runs either way (dummy hash for unknown emails), so neither
        # the body nor the timing is a user-exists oracle.
        stored = user["password_hash"] if user is not None else auth.dummy_password_hash()
        if not auth.verify_password(body.password, stored) or user is None:
            raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
        return _signed_in(user)

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
    ) -> FileResponse:
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
        # Ownership rides on the job row; a rendered file with no row (the
        # reaper is mid-sweep) is treated as already gone.
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Not found.")
        _require_job_access(job, actor, detail="Not found.")
        # Defence in depth: the resolved path must stay inside clips_dir.
        resolved = (clips_dir / job_id / name).resolve()
        if not resolved.is_relative_to(clips_dir.resolve()) or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Not found.")
        media_type = "video/mp4" if name.endswith(".mp4") else None
        # A rendered clip is one account's private video: shared caches must
        # never keep a copy, and the browser must revalidate rather than serve
        # it after the job is reaped or the session changes hands.
        return FileResponse(
            resolved,
            media_type=media_type,
            filename=name,
            headers={"Cache-Control": "private, max-age=0"},
        )

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
