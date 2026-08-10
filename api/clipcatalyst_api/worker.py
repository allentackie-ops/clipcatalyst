"""The Celery task that runs the whole pipeline for one job.

probe → transcribe → diarize → analyze → reframe → render, mirroring the
browser Studio's stages.
Progress is persisted on the job row (throttled) so `GET /v1/jobs/{id}` can
stream honest progress. Every failure lands in status=failed with a friendly,
user-presentable error string.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

from celery.exceptions import SoftTimeLimitExceeded

from . import db
from .pipeline.croptrack import CropTrack, CropTrackOptions, build_crop_track
from .pipeline.diarize import assign_speakers, build_speech_segments
from .pipeline.facetrack import detect_faces
from .pipeline.highlights import plan_clips
from .pipeline.probe import probe_media
from .pipeline.render import render_clip
from .pipeline.speaker_embed import diarization_enabled, segment_embeddings
from .pipeline.transcribe import extract_audio_features, get_transcriber
from .pipeline.types import (
    ClipPlan,
    HighlightOptions,
    PipelineError,
    RenderOptions,
    Transcript,
)
from .plans import PLANS, Plan, effective_plan
from .queue_app import celery_app
from .settings import Settings, get_settings
from .storage import Storage, get_storage

logger = logging.getLogger(__name__)

PROGRESS_WRITE_INTERVAL_SECONDS = 0.5

_GENERIC_ERROR = (
    "Something went wrong while processing this video. Please try again — "
    "if it keeps failing, re-export the file as a standard MP4."
)
_NO_SPEECH_ERROR = (
    "We couldn't find enough speech in this video to build clips. "
    "ClipCatalyst needs clear spoken dialogue — try a talking video."
)
_ALL_RENDERS_FAILED_ERROR = (
    "Rendering failed for every planned clip. The video may use an unusual "
    "format — try re-exporting it as a standard MP4 (H.264 + AAC)."
)
_TIMEOUT_ERROR = (
    "This video took too long to process and was stopped. Try a shorter clip "
    "count or a shorter source video."
)

# Every clip is cut to 9:16; the source aspect decides how wide that window is.
_TARGET_ASPECT = 9 / 16
_DEFAULT_SOURCE_ASPECT = 16 / 9


class _Throttle:
    """Rate-limits progress writes to at most one per interval."""

    def __init__(self, interval: float = PROGRESS_WRITE_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._last = 0.0

    def ready(self) -> bool:
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            return True
        return False


@celery_app.task(bind=True, name="clipcatalyst.process_job")
def process_job(self, job_id: str) -> None:
    settings = get_settings()
    storage = get_storage(settings)
    try:
        _run(job_id, settings, storage)
    except SoftTimeLimitExceeded:
        # Fired ~60 s before the hard kill: record a terminal failure while we
        # still can, then re-raise so Celery marks the task FAILURE too.
        logger.warning("job %s exceeded the soft time limit", job_id)
        _fail(job_id, _TIMEOUT_ERROR)
        raise
    except PipelineError as exc:
        # Pipeline errors are written to be shown to users — map them through.
        logger.warning("job %s failed: %s", job_id, exc)
        _fail(job_id, str(exc))
    except Exception:
        logger.exception("job %s crashed unexpectedly", job_id)
        _fail(job_id, _GENERIC_ERROR)
    finally:
        # On every exit that isn't a hard-kill, drop the uploaded source: it is
        # never needed again once the job is terminal (done or failed).
        _remove_source(storage, settings, job_id)


def _fail(job_id: str, message: str) -> None:
    """Record a terminal failed status; never let bookkeeping raise on us.

    A job that fails rendered nothing the owner asked for, so the whole quota
    reservation /start took goes back — released BEFORE the status write, so a
    process that dies between the two (the soft time limit fires ~60 s before
    the hard kill) leaves a non-terminal row that boot-time reconciliation
    still sweeps, rather than a terminal one holding quota nobody will ever
    return. Settling is once-only, so this is safe on a Celery retry and after
    a partial success that already settled.
    """
    try:
        db.settle_usage(job_id, 0)
    except Exception:
        logger.exception("job %s: could not release the quota reservation", job_id)
    try:
        db.update_job(job_id, status="failed", error=message)
    except Exception:
        logger.exception("job %s: could not record failed status", job_id)


def _remove_source(storage: Storage, settings: Settings, job_id: str) -> None:
    """Delete the uploaded source from disk (and via the backend if it can).

    Prefers a backend ``delete_source`` hook when present; otherwise removes the
    known on-disk locations (LocalStorage's uploads dir and the S3 tmp staging
    copy). Never raises — cleanup failures must not mask the real outcome.
    """
    delete = getattr(storage, "delete_source", None)
    if callable(delete):
        try:
            delete(job_id)
            return
        except Exception:
            logger.warning("job %s: delete_source failed; falling back", job_id)
    for path in (
        settings.uploads_dir / f"{job_id}.src",
        settings.tmp_dir / f"{job_id}.src",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("job %s: could not remove source %s", job_id, path)


def _run(job_id: str, settings: Settings, storage: Storage) -> None:
    db.init_db()
    job = db.get_job(job_id)
    if job is None:
        raise PipelineError("This job no longer exists.")

    settings.ensure_dirs()
    src = storage.source_path(job_id)

    # --- probe -----------------------------------------------------------
    db.update_job(
        job_id,
        status="processing",
        stage="probe",
        progress=0.0,
        detail="Reading the video",
        error=None,
    )
    info = probe_media(src, settings)

    # --- transcribe ------------------------------------------------------
    db.update_job(
        job_id, stage="transcribe", progress=0.0, detail="Analyzing the audio"
    )
    features = extract_audio_features(src, settings)

    throttle = _Throttle()

    def on_transcribe_progress(p: float) -> None:
        if throttle.ready():
            db.update_job(
                job_id,
                stage="transcribe",
                progress=round(min(1.0, max(0.0, p)), 4),
                detail="Transcribing speech",
            )

    transcript = get_transcriber(settings).transcribe(src, on_transcribe_progress)
    if not transcript.words:
        raise PipelineError(_NO_SPEECH_ERROR)
    db.update_job(job_id, stage="transcribe", progress=1.0, detail="Transcript ready")

    # --- diarize (best-effort: any failure keeps words unassigned) -------
    # CC_DIARIZATION=off skips the stage entirely (no stage row is written,
    # so progress reads probe → transcribe → analyze exactly as before).
    if diarization_enabled(settings):
        db.update_job(
            job_id, stage="diarize", progress=0.0, detail="Listening for speakers"
        )
        transcript, speaker_count = _diarize_transcript(job_id, src, transcript, settings)
        db.update_job(
            job_id,
            stage="diarize",
            progress=1.0,
            detail=f"Found {speaker_count} speakers" if speaker_count > 1 else "One speaker",
        )

    # --- analyze ---------------------------------------------------------
    db.update_job(
        job_id, stage="analyze", progress=0.0, detail="Finding the best moments"
    )
    options = HighlightOptions(
        target_length=int(job["target_length"]), count=int(job["count"])
    )
    plans = plan_clips(transcript, features, options)
    if not plans:
        raise PipelineError(_NO_SPEECH_ERROR)
    db.update_job(
        job_id,
        stage="analyze",
        progress=1.0,
        detail=f"Planned {len(plans)} clip{'s' if len(plans) != 1 else ''}",
    )

    total = len(plans)

    # --- reframe (best-effort: every failure degrades to a centered crop) --
    # Detection runs for all clips before rendering starts so the stage the UI
    # shows advances once instead of alternating reframe → render → reframe.
    # `info` rides along so detection reuses the probe above instead of
    # spawning one of its own per clip.
    source_aspect = (
        info.width / info.height
        if info.width > 0 and info.height > 0
        else _DEFAULT_SOURCE_ASPECT
    )
    tracks: list[CropTrack | None] = []

    for index, plan in enumerate(plans):
        base = index / total
        detail = f"Finding the speaker in clip {index + 1} of {total}"
        db.update_job(job_id, stage="reframe", progress=round(base, 4), detail=detail)

        reframe_throttle = _Throttle()

        def on_detect_progress(p: float, base: float = base, detail: str = detail) -> None:
            if reframe_throttle.ready():
                overall = base + min(1.0, max(0.0, p)) / total
                db.update_job(
                    job_id, stage="reframe", progress=round(overall, 4), detail=detail
                )

        try:
            samples = detect_faces(
                src, plan.start, plan.end, settings, on_detect_progress, info
            )
            tracks.append(
                build_crop_track(
                    samples,
                    CropTrackOptions(
                        duration=max(0.1, plan.end - plan.start),
                        target_aspect=_TARGET_ASPECT,
                        source_aspect=source_aspect,
                    ),
                )
            )
        except Exception:
            # A clip that can't be tracked still renders — centered, as before.
            logger.warning(
                "job %s: reframe for clip %d/%d failed; using a centered crop",
                job_id,
                index + 1,
                total,
                exc_info=True,
            )
            tracks.append(None)

    tracked = sum(1 for t in tracks if t is not None and t.coverage > 0)
    db.update_job(
        job_id,
        stage="reframe",
        progress=1.0,
        detail=f"Tracked the speaker in {tracked} of {total} clip{'s' if total != 1 else ''}",
    )

    # --- render (sequential, per-clip failure isolation) -----------------
    opts = RenderOptions(height=_height_for(job), watermark=_watermark_for(job))
    clips: list[dict] = []
    failed_count = 0

    for index, plan in enumerate(plans):
        base = index / total
        detail = f"Rendering clip {index + 1} of {total}"
        db.update_job(job_id, stage="render", progress=round(base, 4), detail=detail)

        name = f"clip-{index + 1:02d}.mp4"
        out_path = settings.tmp_dir / f"{job_id}-{name}"
        render_throttle = _Throttle()

        def on_render_progress(p: float, base: float = base, detail: str = detail) -> None:
            if render_throttle.ready():
                overall = base + min(1.0, max(0.0, p)) / total
                db.update_job(
                    job_id, stage="render", progress=round(overall, 4), detail=detail
                )

        try:
            rendered = render_clip(
                src, plan, out_path, opts, settings, on_render_progress, tracks[index]
            )
            storage.put_clip(job_id, rendered.path, name)
        except PipelineError as exc:
            failed_count += 1
            logger.warning("job %s: clip %d/%d failed: %s", job_id, index + 1, total, exc)
            continue
        except Exception:
            failed_count += 1
            logger.exception("job %s: clip %d/%d crashed", job_id, index + 1, total)
            continue
        finally:
            out_path.unlink(missing_ok=True)

        clips.append(
            _clip_out(plan, index, storage.clip_url(job_id, name), rendered.width, rendered.height)
        )

    if not clips:
        raise PipelineError(_ALL_RENDERS_FAILED_ERROR)

    db.set_clips(job_id, clips)
    done_detail = f"Rendered {len(clips)} clip{'s' if len(clips) != 1 else ''}"
    if failed_count:
        done_detail += f" ({failed_count} failed)"
    db.update_job(
        job_id,
        status="done",
        stage="render",
        progress=1.0,
        detail=done_detail,
        error=None,
    )
    # Quota bookkeeping: /start already RESERVED this job's whole `count`
    # against the owner's month, so completion only settles the difference —
    # the owner keeps the clips that ACTUALLY rendered and gets the rest back
    # (a failed clip costs nothing). This is the same table /v1/me and the
    # start-time reservation read, so the account page can never disagree with
    # enforcement. Anonymous jobs reserved nothing, so this is a no-op for
    # them, and settling twice (a Celery retry) is a no-op too.
    db.settle_usage(job_id, len(clips))


def _diarize_transcript(
    job_id: str, src, transcript: Transcript, settings: Settings
) -> tuple[Transcript, int]:
    """Stamp `word.speaker` onto the transcript's words; NEVER raises.

    Diarization is best-effort by contract (SPEAKERS.md): any failure — ffmpeg
    dying, numpy missing, a numeric surprise — degrades to unassigned words,
    which the caption builder renders exactly like today's single-voice violet.
    Returns the (possibly rebuilt) transcript and the speaker count. When only
    one speaker is heard the transcript is returned untouched, keeping the
    single-voice render byte-identical to the pre-diarization output.
    """
    try:
        segments = build_speech_segments(transcript.words)
        embeddings = segment_embeddings(src, segments, settings)
        result = assign_speakers(transcript.words, segments, embeddings)
        if result.speaker_count <= 1:
            return transcript, 1
        # Word is frozen; plans re-window these words and carry speaker along.
        words = [
            dataclasses.replace(w, speaker=s) if s is not None else w
            for w, s in zip(transcript.words, result.word_speakers)
        ]
        return dataclasses.replace(transcript, words=words), result.speaker_count
    except Exception:
        logger.warning(
            "job %s: diarization failed; captions stay single-color",
            job_id,
            exc_info=True,
        )
        return transcript, 1


def _owner_entitlements(job: dict) -> Plan | None:
    """The job owner's EFFECTIVE plan entitlements, as of render time.

    None for the cases with no live account behind the job: anonymous
    founder/dev jobs (user_id ""), and owned jobs whose user row is gone.
    Reading the plan here rather than trusting the job row is the point —
    an upgrade that lands between create and start is honoured, and a plan
    that lapsed in that window cannot keep spending the entitlements it had
    when the row was written. Nothing the client sent is read.
    """
    owner_id = str(job.get("user_id") or "")
    if not owner_id:
        return None
    user = db.get_user_by_id(owner_id)
    if user is None:
        return None
    return PLANS[effective_plan(user)]


def _watermark_for(job: dict) -> bool:
    """Whether this job's renders carry the watermark. Server-side facts only.

    Anonymous founder/dev jobs keep the cloud default — watermarked, exactly
    today's output. Owned jobs follow the owner's effective plan (free and
    lapsed-paid plans require it; live paid plans drop it).
    """
    entitlements = _owner_entitlements(job)
    return True if entitlements is None else entitlements.watermark_required


def _height_for(job: dict) -> int:
    """The render height for this job, re-clamped to the owner's plan NOW.

    ``create_job`` clamps the requested height to the plan the account held at
    create time and stores the result — but the row then outlives the plan. A
    Pro account could stockpile unstarted 4K jobs, downgrade, and drain them:
    the stored 3840 would render 4K on a free account forever. So the ceiling
    is re-derived at render time exactly like the watermark, and the stored
    height only ever tightens (a smaller ask is never raised by an upgrade —
    the user chose it).
    """
    height = int(job["height"])
    entitlements = _owner_entitlements(job)
    if entitlements is None:
        return height
    return min(height, entitlements.max_height)


def _clip_out(plan: ClipPlan, index: int, url: str, width: int, height: int) -> dict:
    return {
        "id": plan.id,
        "index": index,
        "score": int(plan.score),
        "title": plan.title,
        "hooks": list(plan.hooks),
        "reason": plan.reason,
        "tip": plan.tip,
        "start": plan.start,
        "end": plan.end,
        "duration": round(plan.end - plan.start, 3),
        "url": url,
        "width": width,
        "height": height,
        # Distinct diarized speakers heard in this clip; 0 when diarization
        # was off, failed, or heard a single voice (words stay unassigned).
        "speaker_count": len({w.speaker for w in plan.words if w.speaker is not None}),
    }


def reap_expired() -> int:
    """Delete jobs (rows + rendered clips + source) older than the TTL.

    Cutoff is ``now - settings.job_ttl_hours``. For each expired job we remove
    its clips directory, any stray source file on disk, and the DB row. Returns
    the number of jobs reaped. Safe to run repeatedly; unit-testable on its own.
    """
    settings = get_settings()
    db.init_db()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.job_ttl_hours)
    ).isoformat(timespec="milliseconds")
    storage = get_storage(settings)

    reaped = 0
    for job in db.list_jobs_older_than(cutoff):
        job_id = job["id"]
        shutil.rmtree(settings.clips_dir / job_id, ignore_errors=True)
        _remove_source(storage, settings, job_id)
        db.delete_job(job_id)
        reaped += 1
    return reaped


@celery_app.task(name="clipcatalyst.reap_expired")
def reap_expired_task() -> int:
    """Celery entry point for the reaper (scheduled hourly via beat)."""
    count = reap_expired()
    if count:
        logger.info("reaper removed %d expired job(s)", count)
    return count
