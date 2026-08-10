"""Pydantic request/response schemas for the HTTP API (v1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    target_length: Literal[15, 30, 60] = 30
    count: int = Field(default=2, ge=1, le=3)
    height: Literal[960, 1280, 1920] = 1920


class UploadTargetOut(BaseModel):
    mode: Literal["put"] = "put"
    url: str


class CreateJobResponse(BaseModel):
    job_id: str
    upload: UploadTargetOut


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
