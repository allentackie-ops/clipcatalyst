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


class GoogleAuthRequest(BaseModel):
    """``POST /v1/auth/google`` — the ID token from Google Identity Services.

    A JWT, and nothing about it is believed until it has been verified
    against Google's key set (googleid.py). The max_length is a parser guard,
    not a credential check: real ID tokens are around a kilobyte, and every
    substantive refusal is the same 401.
    """

    id_token: str = Field(min_length=1, max_length=8192)


class EmailCodeStartRequest(BaseModel):
    """``POST /v1/auth/email/start`` — send a 6-digit code to this address.

    Only the address, deliberately: nothing about the caller decides what is
    sent or where, and the route never looks up whether an account exists, so
    there is nothing here to enumerate with.
    """

    email: str = Field(min_length=3, max_length=254)


class EmailCodeStartResponse(BaseModel):
    """The ONE body ``start`` ever returns on success.

    Byte-identical for an address with an account and one without — that
    identity is the anti-enumeration property, so this model has no field that
    could ever differ between the two.
    """

    sent: bool = True


class EmailCodeVerifyRequest(BaseModel):
    """``POST /v1/auth/email/verify`` — trade a code for a session.

    `code` is bounded but not shaped: the length is a parser guard, and every
    substantive refusal — wrong, expired, exhausted, never requested — must
    collapse into the same 401, so a 6-character rule here (which would answer
    422 for a 7-character guess) would be a distinguishable answer the spec
    does not allow.
    """

    email: str = Field(min_length=1, max_length=254)
    code: str = Field(min_length=1, max_length=64)


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
    # Whether cloud renders may carry the account's own logo and caption
    # colour. Its own promise, never inferred from watermark_required — the
    # panel shows the upsell off THIS field.
    brand_kit: bool = False
    # How long a saved clip's FILE is kept; None = forever (LIBRARY.md Part 2).
    # The account page says this out loud, and says it from here rather than
    # from a table in the frontend, so the promise on screen is the one the
    # reaper actually enforces.
    retention_days: int | None = None


class BrandKitRequest(BaseModel):
    """The JSON shape of ``PUT /v1/me/brand`` (multipart is the other form).

    A PUT replaces the whole kit, so an omitted field means "not part of the
    kit any more", not "leave it alone" — the panel holds the complete kit
    locally and syncs it whole. ``logo`` is a ``data:`` URL (what the browser
    already stores); its bytes are decoded, size-checked and SNIFFED
    server-side, and the declared media type in the URL is ignored.
    """

    logo: str | None = None
    caption_color: str | None = None
    show_logo: bool = True


class BrandKitOut(BaseModel):
    """A stored kit as the client sees it — the logo as a URL, never bytes."""

    logo_url: str | None = None
    caption_color: str | None = None
    show_logo: bool = True


class MeResponse(BaseModel):
    """The single account source the frontend trusts (plan, quota, limits)."""

    email: str
    plan: str
    plan_status: str
    # How this account can be signed into: any of "password", "google" and
    # "email" (LIBRARY.md Part 1, EMAILAUTH.md). The account page reads it to
    # say so out loud, and to never offer "change password" on a password-less
    # account. "email" is present whenever the server has a mailer, since a
    # code is sent to an address and every account has one.
    auth_methods: list[str] = Field(default_factory=list)
    quota: QuotaOut
    entitlements: EntitlementsOut
    brand: BrandKitOut = Field(default_factory=BrandKitOut)


# --------------------------------------------------------------------------- #
# The clip library (LIBRARY.md Part 2). Two lifetimes in one row: the metadata
# is permanent, the file expires — so `available` and `url` describe the FILE,
# and everything else survives it.
# --------------------------------------------------------------------------- #


class ClipWordOut(BaseModel):
    """One transcript word, re-based to the clip's start. Mirrors
    ``TranscriptWord`` in lib/studio/types.ts."""

    text: str
    start: float
    end: float
    speaker: int | None = None


class ClipSummaryOut(BaseModel):
    """A library clip as the grid sees it — everything but the transcript."""

    id: str
    job_id: str = ""  # '' for a browser clip: nothing was rendered on our box
    clip_index: int = 0
    title: str = ""
    score: int = Field(default=0, ge=0, le=100)
    hooks: list[str] = Field(default_factory=list)
    reason: str = ""
    tip: str = ""
    start: float = 0.0
    end: float = 0.0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    speaker_count: int = 0
    engine: str = "cloud"  # "cloud" | "browser"
    bytes: int = 0
    created_at: str
    # When the FILE goes; None = never (an unlimited plan). The row itself has
    # no expiry — the card keeps showing title, score and hooks afterwards.
    expires_at: str | None = None
    # Is the video still here? Everything above is here regardless.
    available: bool = False
    # Only when available — a card with a url that 404s is the broken player
    # LIBRARY.md's non-negotiables rule out.
    url: str | None = None


class ClipDetailOut(ClipSummaryOut):
    """One clip in full, transcript included (``GET /v1/clips/{id}``)."""

    words: list[ClipWordOut] = Field(default_factory=list)


class ClipListResponse(BaseModel):
    clips: list[ClipSummaryOut] = Field(default_factory=list)
    # Feed back as `?before=` for the next page; None = the end of the library.
    # Opaque on purpose: it is the last item's created_at plus its id, because
    # clips from one render share a timestamp and a cursor that could not tell
    # them apart would drop the rest of the group at a page boundary.
    next_before: str | None = None


class ClipUploadRequest(BaseModel):
    """The JSON metadata part of ``POST /v1/clips/upload``.

    A browser clip's card, as the Studio already holds it. Nothing here is
    trusted for anything but display: `engine` is forced to "browser" by the
    route, the expiry comes from the account's plan, and the byte count comes
    from the file we actually stored — never from this payload.
    """

    title: str = Field(default="", max_length=300)
    score: int = Field(default=0, ge=0, le=100)
    hooks: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(default="", max_length=1000)
    tip: str = Field(default="", max_length=1000)
    start: float = Field(default=0.0, ge=0)
    end: float = Field(default=0.0, ge=0)
    duration: float = Field(default=0.0, ge=0)
    width: int = Field(default=0, ge=0, le=8192)
    height: int = Field(default=0, ge=0, le=8192)
    speaker_count: int = Field(default=0, ge=0, le=64)
    clip_index: int = Field(default=0, ge=0, le=1000)
    # A 60 s clip is ~200 words; the ceiling is a parser guard, not a limit
    # anybody should ever meet.
    words: list[ClipWordOut] = Field(default_factory=list, max_length=5000)


class ClipDeletedResponse(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
# Connected publishing accounts (PUBLISH.md Part 2).
#
# Read these as the enforcement of one rule: there is NO field on any model
# below that a token could be put in. "Never returned by an endpoint" is a
# property of the schema here, not a habit of the routes.
# --------------------------------------------------------------------------- #


class ConnectionOut(BaseModel):
    """One connected channel, as the account page sees it.

    `account_name` is what a creator recognises (their channel's title);
    `account_id` is the provider's own id for it, carried so the UI can tell
    two channels apart when somebody has connected more than one.
    """

    id: str
    platform: str  # 'youtube' …
    account_name: str = ""
    account_id: str = ""
    scopes: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class PlatformOut(BaseModel):
    """A platform the product knows about, and whether it can be connected.

    Sent for every platform, connected or not, because PUBLISH.md's UI rule is
    that nothing may offer a platform which cannot currently complete a post —
    and the honest answer to "can it?" is a server-side fact (is there a token
    key, a client id, a public URL, an approved review) rather than something
    the frontend should be holding its own copy of.
    """

    platform: str
    label: str
    connectable: bool
    # Why not, in words a creator reads. '' when it can be connected.
    reason: str = ""
    # The standing caveat about posting here — for YouTube, that uploads land
    # as private until Google's review completes. Shown either way.
    note: str = ""
    # Is there an adapter in this build that can actually complete a post
    # (PUBLISH.md Part 3)? Connectable and publishable are different questions:
    # a platform could be connected for reading long before it can be posted
    # to, and the Post button follows THIS one.
    publishable: bool = False
    # The visibilities a post here may be given, in the order to show them.
    # A single entry means there is no choice — which is the honest state while
    # Google's review is outstanding, and the reason this is a list rather than
    # a boolean: a UI that renders the list cannot offer an option the server
    # would silently override.
    privacy_choices: list[str] = Field(default_factory=list)
    # Set when the platform allows no choice at all, and to the value it
    # forces. '' when the user's pick stands.
    forced_privacy: str = ""


class ConnectionListResponse(BaseModel):
    connections: list[ConnectionOut] = Field(default_factory=list)
    platforms: list[PlatformOut] = Field(default_factory=list)


class ConnectionStartResponse(BaseModel):
    """Where to send the browser. The state and PKCE challenge are inside it."""

    authorize_url: str


class ConnectionDeletedResponse(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
# Posting a clip to a connected channel (PUBLISH.md Part 3).
# --------------------------------------------------------------------------- #


class PublishClipRequest(BaseModel):
    """``POST /v1/clips/{id}/publish`` — post this clip to a connected channel.

    `title` and `description` are what the sheet's two fields hold; both may be
    empty, and what an empty one means is the adapter's rule (the clip's own
    title, and its top hook) rather than a client-side default, so the same
    post is produced whoever is calling.

    `privacy` is a REQUEST. What actually happens is the platform's capability
    resolved against it — forced to `private` while Google's review is
    outstanding — and the response says which it was.
    """

    platform: str = Field(default="youtube", min_length=1, max_length=32)
    title: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=5000)
    privacy: Literal["public", "unlisted", "private"] = "private"


class PublishJobOut(BaseModel):
    """One publish attempt, as the sheet polling it sees it.

    The same shape as a render job's status for the same reason: it is the same
    kind of thing to a browser — a status, a fraction, a line of prose, and an
    error written to be read. There is no field here a token could go in; the
    row it is built from has none either.
    """

    id: str
    clip_id: str
    platform: str
    status: str  # queued | uploading | done | failed
    progress: float = 0.0
    detail: str = ""
    error: str | None = None
    # What was asked for, and what the platform's capability made of it.
    title: str = ""
    privacy: str = "private"
    # Only once it has landed — the link the UI offers when the post is done.
    video_id: str | None = None
    video_url: str | None = None
    # The standing caveat for this platform (uploads land private, …), carried
    # on the job so the sheet can keep saying it while the upload runs.
    note: str = ""
    created_at: str
    updated_at: str


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
