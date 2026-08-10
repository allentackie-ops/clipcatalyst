"""FastAPI app: job creation, local uploads, start, status, file serving."""

from __future__ import annotations

import hmac
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import db
from .models import (
    ClipOut,
    CreateJobRequest,
    CreateJobResponse,
    HealthzResponse,
    JobStatusResponse,
    StartJobResponse,
    UploadAckResponse,
    UploadTargetOut,
)
from .settings import get_settings
from .storage import get_storage
from .worker import process_job

API_VERSION = "0.1.0"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def require_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """Gate mutating routes behind a bearer token when one is configured.

    ``CC_API_TOKEN=""`` keeps the API open (dev default, current behaviour).
    When a token is set, callers must send exactly
    ``Authorization: Bearer <token>``; compared in constant time.
    """
    token = get_settings().api_token
    if not token:
        return None
    expected = f"Bearer {token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")
    return None


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

    @router.post(
        "/jobs",
        response_model=CreateJobResponse,
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    def create_job(body: CreateJobRequest) -> CreateJobResponse:
        settings = get_settings()
        if body.size_bytes > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File is too large — the limit is "
                    f"{settings.max_upload_bytes} bytes."
                ),
            )
        job_id = uuid.uuid4().hex
        db.create_job(
            job_id,
            filename=body.filename,
            size_bytes=body.size_bytes,
            target_length=body.target_length,
            count=body.count,
            height=body.height,
        )
        target = get_storage(settings).upload_target(job_id)
        return CreateJobResponse(job_id=job_id, upload=UploadTargetOut(**target))

    @router.put(
        "/uploads/{job_id}",
        response_model=UploadAckResponse,
        dependencies=[Depends(require_token)],
    )
    async def upload_source(job_id: str, request: Request) -> UploadAckResponse:
        settings = get_settings()
        if settings.storage != "local":
            raise HTTPException(
                status_code=404,
                detail="Direct uploads are disabled — use the presigned S3 URL.",
            )
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
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

    @router.post(
        "/jobs/{job_id}/start",
        response_model=StartJobResponse,
        status_code=202,
        dependencies=[Depends(require_token)],
    )
    def start_job(job_id: str) -> StartJobResponse:
        settings = get_settings()
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        if not get_storage(settings).source_exists(job_id):
            raise HTTPException(
                status_code=409, detail="Upload the video before starting the job."
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
    def get_job_status(job_id: str) -> JobStatusResponse:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job.")
        return JobStatusResponse(
            job_id=job["id"],
            status=job["status"],
            stage=job["stage"] or "",
            progress=float(job["progress"] or 0.0),
            detail=job["detail"] or "",
            error=job["error"],
            clips=[ClipOut(**clip) for clip in job["clips"]],
        )

    @router.get("/files/{job_id}/{name}", dependencies=[Depends(require_token)])
    def get_file(job_id: str, name: str) -> FileResponse:
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
