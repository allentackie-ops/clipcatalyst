"""Pydantic request/response schemas for the HTTP API (v1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    target_length: Literal[15, 30, 60] = 30
    count: int = Field(default=2, ge=1, le=3)
    # 3840 (4K) is cloud-only — the browser pipeline stays ≤ 1920. A session's
    # request is clamped server-side to its plan's max_height (see create_job).
    height: Literal[960, 1280, 1920, 3840] = 1920


class UploadTargetOut(BaseModel):
    mode: Literal["put"] = "put"
    url: str


class CreateJobResponse(BaseModel):
    job_id: str
    upload: UploadTargetOut
    # What the server DECIDED (post-entitlement), not what was asked: the
    # height after the plan clamp and whether renders will carry a watermark.
    height: int
    watermark: bool


class UploadAckResponse(BaseModel):
    ok: bool = True


class StartJobResponse(BaseModel):
    job_id: str
    status: str = "queued"


class ClipOut(BaseModel):
    """Mirrors the browser Studio's clip card contract."""

    id: str
    index: int
    score: int = Field(ge=0, le=100)
    title: str
    hooks: list[str] = Field(default_factory=list)
    reason: str
    tip: str
    start: float
    end: float
    duration: float
    url: str
    width: int
    height: int
    # Distinct diarized speakers in the clip's words; 0 = diarization off,
    # failed, or a single voice (browser parity: badge shows only at >= 2).
    speaker_count: int = 0


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # awaiting_upload | queued | processing | done | failed
    stage: str  # "" | probe | transcribe | diarize | analyze | reframe | render
    progress: float
    detail: str
    error: str | None = None
    clips: list[ClipOut] = Field(default_factory=list)


class HealthzResponse(BaseModel):
    ok: bool = True
    version: str
    queue: str  # "redis" | "eager"
    storage: str  # "local" | "s3"
    transcriber: str  # "faster-whisper" | "fake"


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # MIN_PASSWORD_LENGTH (auth.py) — enforced here so the 422 names the field.
    password: str = Field(min_length=8, max_length=1024)


class LoginRequest(BaseModel):
    # No min_length beyond non-empty: every credential failure must collapse
    # into the one generic 401, so login never pre-judges password shape.
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class AuthUserOut(BaseModel):
    id: str
    email: str
    plan: str
    plan_status: str


class AuthResponse(BaseModel):
    token: str  # raw `cc_sess_…` bearer — shown once, only its hash is stored
    user: AuthUserOut


class LogoutResponse(BaseModel):
    ok: bool = True


class QuotaOut(BaseModel):
    limit: int | None  # None = unlimited (enterprise)
    used: int
    month: str  # "YYYY-MM" (UTC)


class EntitlementsOut(BaseModel):
    max_height: int
    watermark_required: bool
    clips_per_month: int | None


class MeResponse(BaseModel):
    """The single account source the frontend trusts (plan, quota, limits)."""

    email: str
    plan: str
    plan_status: str
    quota: QuotaOut
    entitlements: EntitlementsOut


class CheckoutRequest(BaseModel):
    # A plan NAME, validated in the route (unknown/free → 400, not 422) and
    # mapped to a server-configured price id — clients never send price ids.
    plan: str = Field(min_length=1, max_length=32)


class CheckoutResponse(BaseModel):
    url: str  # Stripe Checkout URL to redirect the browser to


class PortalResponse(BaseModel):
    url: str  # Stripe billing portal URL


class WebhookAckResponse(BaseModel):
    received: bool = True
