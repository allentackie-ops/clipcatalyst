"""FastAPI app: accounts/auth, job creation, uploads, start, status, files."""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import auth, billing, db
from .models import (
    AuthResponse,
    AuthUserOut,
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
from .settings import get_settings
from .storage import get_storage
from .worker import process_job

API_VERSION = "0.1.0"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOGIN_FAILED = "Incorrect email or password."
_BILLING_OFF = (
    "Billing isn't enabled on this server yet — plan upgrades will activate "
    "once Stripe is configured."
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    db.init_db()
    # Fail crash-stranded jobs on boot: anything still queued/processing whose
    # last update predates a full render window plus slack must be dead.
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.render_timeout_s + 300
    )
    db.reconcile_stalled(cutoff.isoformat(timespec="milliseconds"))
    # Expired sessions are dead weight — clear them on every boot.
    db.purge_expired_sessions()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="ClipCatalyst Cloud API", version=API_VERSION, lifespan=_lifespan)
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
    def register(body: RegisterRequest, request: Request) -> AuthResponse:
        auth.enforce_rate_limit(_client_ip(request), "register")
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
    def login(body: LoginRequest, request: Request) -> AuthResponse:
        auth.enforce_rate_limit(_client_ip(request), "login")
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
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> LogoutResponse:
        auth.require_session(authorization)  # 401 unless the session is live
        raw = auth.bearer_session_token(authorization)
        assert raw is not None  # require_session guarantees a session bearer
        db.revoke_session(auth.hash_session_token(raw))
        return LogoutResponse()

    @router.get("/me", response_model=MeResponse)
    def me(user: dict = Depends(auth.require_session)) -> MeResponse:
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
            ),
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
        body: CheckoutRequest, user: dict = Depends(auth.require_session)
    ) -> CheckoutResponse:
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

        # Monthly quota, checked BEFORE the job is claimed so a 402 leaves it
        # startable once the month rolls over or the plan grows. The quota
        # rides on the job's OWNER (usage is billed to them however the start
        # arrives); anonymous founder/dev jobs have no owner and no quota.
        owner_id = str(job.get("user_id") or "")
        if owner_id:
            owner = db.get_user_by_id(owner_id)
            if owner is not None:
                plan_name = effective_plan(owner)
                limit = PLANS[plan_name].clips_per_month
                if limit is not None:
                    month = _current_month()
                    used = db.get_usage(owner_id, month)
                    if used + int(job["count"]) > limit:
                        raise HTTPException(
                            status_code=402,
                            detail=(
                                f"Monthly clip limit reached — the {plan_name} "
                                f"plan includes {limit} clips per month, "
                                f"{used} are used for {month}, and this job "
                                f"would render {int(job['count'])} more. The "
                                f"counter resets in {_next_month(month)}."
                            ),
                        )

        # Atomically claim the job so concurrent / duplicate starts can't both
        # enqueue it — only the caller that wins the awaiting_upload→queued move
        # gets to dispatch the pipeline task.
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
        # In eager mode (CC_QUEUE=eager) this runs the whole pipeline inline.
        process_job.delay(job_id)
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
        return FileResponse(resolved, media_type=media_type, filename=name)

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
