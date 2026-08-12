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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded, Terminated

from . import connections, db, publish
from .brandkit import logo_abs_path, normalize_hex
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
from .storage import Storage, clip_media_type, get_storage

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

# Slack on top of one render window before a queued/processing row is treated
# as abandoned by reconcile_stalled.
STALL_SLACK_SECONDS = 300

# Celery's own control-flow signals: the task is being torn down, which is not
# the same thing as one stage of it failing. BOTH subclass Exception, so every
# best-effort `except Exception` below would otherwise swallow them and carry
# on working inside a task that is already dying — the soft time limit fires
# ~60 s before the HARD kill, so a per-clip handler that "recovers" from it
# spends that grace on the next clip and gets SIGKILLed mid-render, leaving the
# row stuck in `processing` still holding its owner's quota. Re-raised, they
# reach process_job, which records the honest timeout and releases the
# reservation while there is still time to write it.
_CONTROL_SIGNALS = (SoftTimeLimitExceeded, Terminated)


class _LostClaim(Exception):
    """This run no longer owns the job row, so it must write NOTHING.

    Raised at the two points where ownership is decided: the queued→processing
    claim that opens a run, and the processing→done transition that closes it.
    Losing either means somebody else has already written a terminal status and
    settled the reservation — the stall reconciler failing an abandoned-looking
    row, the reaper deleting an expired one, or a second delivery of the same
    task. Whatever this run produced is not billed to anybody, so publishing it
    would be giving clips away; whatever quota it thought it held is already
    back in the owner's month, so settling would take it a second time.

    Handled in process_job, which stops the run without a status write and
    without a settlement — deliberately NOT a PipelineError, which would land
    in _fail and write the very terminal state we must not write.

    ``staged`` carries the library files this run had already copied out of the
    job's directory (see _stage_library_file). They belong to nobody — no row
    was ever written for them, because rows are written only after the
    processing→done transition is WON — so process_job removes them.
    """

    def __init__(self, job_id: str, staged: tuple[str, ...] = ()) -> None:
        super().__init__(job_id)
        self.staged = staged


@dataclasses.dataclass(frozen=True)
class _StagedClip:
    """One rendered clip on its way into the library.

    Held between the render loop (which copies the file into the library root
    while the render is still on disk) and _publish_library (which writes the
    row, and only once this run has WON the right to publish anything).
    """

    plan: ClipPlan
    clip: dict  # the ClipOut dict the job row publishes
    file_path: str  # what the library row will point at
    size_bytes: int


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
    # Whether this run owns the job. A duplicate delivery that loses the claim
    # owns nothing on disk — see the `finally`.
    owns_job = True
    try:
        _run(job_id, settings, storage)
    except _LostClaim as lost:
        # Ordered FIRST: every handler below writes a terminal status, which is
        # precisely what a run that lost its row may not do. Stop cleanly and
        # drop any output this run produced (see _discard_output).
        owns_job = False
        logger.warning(
            "job %s: the row moved out from under this run; discarding it", job_id
        )
        _discard_output(settings, storage, job_id, lost.staged)
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
        #
        # Unless this run lost the claim. Celery delivers at least once, so a
        # worker restart or an expired visibility timeout can start a second
        # run of a job another worker is already rendering. That duplicate owns
        # nothing: deleting the source here pulls the file out from under the
        # owning run mid-ffmpeg, which fails a perfectly good job and tells the
        # user to re-export a video that was never the problem. The owning run
        # removes the source on its own exit.
        if owns_job:
            _remove_source(storage, settings, job_id)


def _fail(job_id: str, message: str) -> None:
    """Record a terminal failed status and release the quota, together.

    A job that fails rendered nothing the owner asked for, so the whole
    reservation /start took goes back. Status write and release are one
    transaction (db.finalize_job), so a process that dies mid-way — the soft
    time limit fires ~60 s before the hard kill — leaves either both or
    neither, never a terminal row holding quota nobody will hand back.

    The guard is `LIVE_STATUSES`, so this fails only a row that is still
    genuinely queued or processing. A False return means the job is already
    terminal: the reconciler failed and refunded it, or it finished. Either
    way the reservation has been settled by its rightful owner and there is
    nothing here to overwrite — which is also why a Celery retry and a failure
    path arriving after a partial success are both no-ops.
    """
    try:
        if not db.finalize_job(
            job_id, expect=db.LIVE_STATUSES, to="failed", rendered=0, error=message
        ):
            logger.warning(
                "job %s: already terminal; leaving its status and quota alone", job_id
            )
    except Exception:
        logger.exception("job %s: could not record failed status", job_id)


def _discard_output(
    settings: Settings, storage: Storage, job_id: str, staged: tuple[str, ...] = ()
) -> None:
    """Bin clips rendered by a run that turned out not to own its job row.

    ``GET /v1/files/{job_id}/{name}`` serves whatever sits under the job's
    clips directory — it authorizes on the row, not on clips_json — so output
    left behind by a lost run is downloadable against a row that has already
    been failed and REFUNDED. That is the free-clips hole, so the files go.

    Only for a row that is failed or gone. A row still `processing` or already
    `done` belongs to somebody else — a duplicate delivery mid-render, or the
    run that legitimately finished — and their clips are not ours to delete.

    ``staged`` names the library copies THIS run made, and only those: the
    rmtree above cannot reach them (the library has its own root), so they are
    removed by name. Each one is checked against the library first, because
    "the job row is gone" is also what a job reaped at 48 h looks like — and
    that job's clips may well still be sitting in somebody's library, saved
    from a run that finished perfectly a month ago. A file a live row still
    points at is never deleted here.
    """
    try:
        job = db.get_job(job_id)
        if job is not None and job["status"] != "failed":
            return
        shutil.rmtree(settings.clips_dir / job_id, ignore_errors=True)
        for file_path in staged:
            if db.clip_file_referenced(file_path):
                continue
            try:
                storage.delete_library_clip(file_path)
            except Exception:
                logger.warning(
                    "job %s: could not remove staged library file %s",
                    job_id,
                    file_path,
                )
    except Exception:
        logger.warning("job %s: could not discard the lost run's output", job_id)


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

    # --- claim -------------------------------------------------------------
    # The row decides who may run this job, and this is a transition the worker
    # can LOSE. It used to be a blind `update_job(status="processing")`, which
    # could not lose: a job the reconciler had already failed and refunded got
    # flipped straight back into the pipeline and rendered for free.
    #
    # The trigger is not an attacker, it is a queue backlog. reconcile_stalled
    # fails anything still queued after render_timeout_s + slack, and a busy box
    # leaves jobs queued for exactly that long — so every backlogged job was
    # delivered unbilled, and its owner's month kept resetting to zero.
    #
    # Winning the claim is what makes the reservation ours to settle later. The
    # source download (S3) happens after it, so a run with no claim does no work
    # at all.
    if not db.transition_status(
        job_id,
        expect="queued",
        to="processing",
        stage="probe",
        progress=0.0,
        detail="Reading the video",
        error=None,
    ):
        raise _LostClaim(job_id)

    src = storage.source_path(job_id)

    # --- probe -----------------------------------------------------------
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
        except _CONTROL_SIGNALS:
            raise  # see _CONTROL_SIGNALS: the task is dying, not the tracker
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
    logo_path, caption_color = _brand_kit_for(job, settings)
    opts = RenderOptions(
        height=_height_for(job),
        watermark=_watermark_for(job),
        logo_path=logo_path,
        caption_color=caption_color,
    )
    clips: list[dict] = []
    # Library copies made by this run, in render order. They are FILES only
    # until the finalize below succeeds — a file nothing points at is invisible
    # to every route, while a row is a clip somebody owns. See _publish_library.
    staged: list[_StagedClip] = []
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
            # Copied out of the job's world WHILE the render is still on disk,
            # because a moment later the `finally` below unlinks it. The
            # destination is the library root, which no job sweep touches.
            library_path, library_size = _stage_library_file(
                storage, settings, job, name, rendered.path
            )
        except _CONTROL_SIGNALS:
            # see _CONTROL_SIGNALS: swallowing this here reported an unusual
            # codec instead of the timeout, and then burned the remaining
            # grace rendering the NEXT clip straight into the hard kill.
            raise
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

        clip = _clip_out(
            plan, index, storage.clip_url(job_id, name), rendered.width, rendered.height
        )
        clips.append(clip)
        if library_path:
            staged.append(
                _StagedClip(
                    plan=plan,
                    clip=clip,
                    file_path=library_path,
                    size_bytes=library_size,
                )
            )

    if not clips:
        raise PipelineError(_ALL_RENDERS_FAILED_ERROR)

    done_detail = f"Rendered {len(clips)} clip{'s' if len(clips) != 1 else ''}"
    if failed_count:
        done_detail += f" ({failed_count} failed)"

    # Publishing the clips, going terminal, and settling the quota are ONE
    # transaction guarded on the status this run claimed.
    #
    # Quota bookkeeping: /start already RESERVED this job's whole `count`
    # against the owner's month, so completion only settles the difference —
    # the owner keeps the clips that ACTUALLY rendered and gets the rest back
    # (a failed clip costs nothing). This is the same table /v1/me and the
    # start-time reservation read, so the account page can never disagree with
    # enforcement. Anonymous jobs reserved nothing, so this is a no-op for them.
    #
    # Losing the guard is the reverse race: the reconciler failed this row
    # mid-run and already handed the reservation back. Writing `done` then
    # would publish clips against a refunded reservation, and settling would
    # bill a month that has already been returned — so we do neither, and
    # clips_json is never written either, because it moves in this same
    # statement rather than just before it.
    if not db.finalize_job(
        job_id,
        expect="processing",
        to="done",
        rendered=len(clips),
        clips_json=db.encode_clips(clips),
        stage="render",
        progress=1.0,
        detail=done_detail,
        error=None,
    ):
        raise _LostClaim(job_id, tuple(item.file_path for item in staged))

    # The library rows land HERE — after the transition that decided this run
    # owns the job, and long (CC_JOB_TTL_HOURS) before anything can reap it.
    #
    # Not one statement earlier: a run that loses the guard above has rendered
    # clips nobody was billed for, and stocking somebody's permanent library
    # with them is the free-clips hole in its most durable form. Losing the
    # guard raises instead, and the staged FILES — which no row points at, so
    # no route can reach — are removed by _discard_output.
    _publish_library(job, staged)


def _stage_library_file(
    storage: Storage, settings: Settings, job: dict, name: str, rendered_path: str
) -> tuple[str, int]:
    """Copy one rendered clip into the library root; ('', 0) when it can't.

    Best-effort by design: the clip itself has already rendered and is being
    delivered on the job, so a library copy that fails must not fail it. A clip
    with no copy simply gets no library row — better than a row that reads as
    expired the day it was made.

    Anonymous founder/dev jobs (no user_id) have no account to own a library
    entry, so they never make one.
    """
    owner_id = str(job.get("user_id") or "")
    if not owner_id:
        return "", 0
    try:
        settings.ensure_dirs()
        size_bytes = Path(rendered_path).stat().st_size
        # <user>/<job>/<name>: it keeps one job's clips together, and it is
        # what `GET /v1/files/{job_id}/{name}` rebuilds to keep an old link
        # working after the job row itself has been reaped.
        stored = storage.put_library_clip(owner_id, f"{job['id']}/{name}", rendered_path)
    except Exception:
        logger.warning(
            "job %s: could not save %s to the library", job.get("id"), name, exc_info=True
        )
        return "", 0
    return stored, size_bytes


def _publish_library(job: dict, staged: list[_StagedClip]) -> None:
    """Write one library row per clip this run rendered and staged.

    ``expires_at`` is computed from the owner's plan RIGHT NOW (db.create_clip
    turns retention_days into a deadline), which is the same rule the render
    itself follows for height, watermark and brand kit: entitlements are read
    at render time, never off the job row.

    Best-effort, per clip: the job is already `done` and its clips are already
    published, so a library write that fails must not turn a finished render
    into a failure. The clip stays downloadable from the job either way.
    """
    owner = _owner_of(job)
    if owner is None:
        return
    retention_days = PLANS[effective_plan(owner)].retention_days
    for item in staged:
        clip = item.clip
        try:
            db.create_clip(
                uuid.uuid4().hex,
                user_id=str(owner["id"]),
                job_id=str(job["id"]),
                clip_index=int(clip["index"]),
                title=str(clip["title"]),
                score=int(clip["score"]),
                hooks=list(clip["hooks"]),
                reason=str(clip["reason"]),
                tip=str(clip["tip"]),
                start=float(clip["start"]),
                end=float(clip["end"]),
                duration=float(clip["duration"]),
                width=int(clip["width"]),
                height=int(clip["height"]),
                speaker_count=int(clip["speaker_count"]),
                # The transcript outlives the video, as decided — these are the
                # clip's own words, already re-based to its start by plan_clips.
                words=[dataclasses.asdict(word) for word in item.plan.words],
                engine="cloud",
                file_path=item.file_path,
                size_bytes=item.size_bytes,
                retention_days=retention_days,
            )
        except Exception:
            logger.exception(
                "job %s: could not write the library row for clip %s",
                job.get("id"),
                clip.get("id"),
            )


def _diarize_transcript(
    job_id: str, src, transcript: Transcript, settings: Settings
) -> tuple[Transcript, int]:
    """Stamp `word.speaker` onto the transcript's words; never fails the job.

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
    except _CONTROL_SIGNALS:
        raise  # see _CONTROL_SIGNALS: not a diarization miss, a dying task
    except Exception:
        logger.warning(
            "job %s: diarization failed; captions stay single-color",
            job_id,
            exc_info=True,
        )
        return transcript, 1


def _owner_of(job: dict) -> dict | None:
    """The account row behind a job, as it stands RIGHT NOW.

    None for the cases with no live account: anonymous founder/dev jobs
    (user_id ""), and owned jobs whose user row is gone.
    """
    owner_id = str(job.get("user_id") or "")
    if not owner_id:
        return None
    return db.get_user_by_id(owner_id)


def _owner_entitlements(job: dict) -> Plan | None:
    """The job owner's EFFECTIVE plan entitlements, as of render time.

    None whenever ``_owner_of`` finds no account. Reading the plan here rather
    than trusting the job row is the point — an upgrade that lands between
    create and start is honoured, and a plan that lapsed in that window cannot
    keep spending the entitlements it had when the row was written. Nothing the
    client sent is read.
    """
    user = _owner_of(job)
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


def _brand_kit_for(job: dict, settings: Settings) -> tuple[str | None, str | None]:
    """This job's brand kit at render time: ``(logo path, caption colour)``.

    Resolved from the owner's LIVE plan, in the same breath as _watermark_for
    and _height_for and for the same reason: a downgrade must take the brand
    kit away on the NEXT RENDER, not at next login. A job row cannot carry a
    kit it was created with.

    Three server-side facts decide it, in the order BRANDKIT.md sets out:

    * no ``brand_kit`` on the effective plan (free, lapsed, anonymous) → no
      kit at all. It is its own entitlement, never read off the watermark.
    * ``watermark_required`` → OUR mark is drawn, so neither the logo nor the
      colour applies; a free account's stored kit is the upsell, not a render.
    * otherwise the colour applies whenever there is one, and the logo also
      needs ``show_logo`` and a file that is really on disk — a row pointing
      at a logo that has been removed renders a clean corner, not an error.

    A colour that no longer parses degrades to None (today's violet), and the
    stored path is resolved through ``logo_abs_path``, which refuses anything
    outside the brand directory.
    """
    owner = _owner_of(job)
    if owner is None:
        return None, None
    entitlements = PLANS[effective_plan(owner)]
    if not entitlements.brand_kit or entitlements.watermark_required:
        return None, None
    logo: str | None = None
    if int(owner.get("brand_show_logo") or 0):
        path = logo_abs_path(settings, str(owner.get("brand_logo_path") or ""))
        if path is not None and path.is_file():
            logo = str(path)
    return logo, normalize_hex(str(owner.get("brand_caption_color") or ""))


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


# --------------------------------------------------------------------------- #
# Posting a clip to a connected channel (PUBLISH.md Part 3).
#
# The same shape as process_job, one size down: claim the row, do the work,
# write a terminal status you are still entitled to write. It shares the queue
# (an upload is minutes of network, not seconds) and shares nothing else — a
# publish renders nothing, reserves no quota, and touches no job row.
#
# Nothing below names a platform. The adapter is looked up from the row and
# spoken to through `PublishTarget`, which is what makes TikTok and Instagram a
# new module rather than a new branch in here.
# --------------------------------------------------------------------------- #

_PUBLISH_GENERIC_ERROR = (
    "Something went wrong while posting this clip. Please try again — if it "
    "keeps failing, disconnect and reconnect the channel."
)
_PUBLISH_TIMEOUT_ERROR = (
    "This upload took too long and was stopped. Please try posting the clip "
    "again."
)


@celery_app.task(bind=True, name="clipcatalyst.publish_clip")
def publish_clip(self, publish_id: str) -> None:
    settings = get_settings()
    try:
        _publish_run(publish_id, settings)
    except _LostClaim:
        # Ordered first, exactly as in process_job: a run that no longer owns
        # the row may not write a terminal status over whatever does own it.
        logger.warning(
            "publish %s: the row moved out from under this run; stopping", publish_id
        )
    except SoftTimeLimitExceeded:
        logger.warning("publish %s exceeded the soft time limit", publish_id)
        _fail_publish(publish_id, _PUBLISH_TIMEOUT_ERROR)
        raise
    except publish.PublishError as exc:
        # Written to be read by the person who pressed Post.
        logger.warning("publish %s failed: %s", publish_id, exc)
        _fail_publish(publish_id, str(exc))
    except Exception:
        logger.exception("publish %s crashed unexpectedly", publish_id)
        _fail_publish(publish_id, _PUBLISH_GENERIC_ERROR)


def _fail_publish(publish_id: str, message: str) -> None:
    """Record a terminal failure, if this run is still the one entitled to.

    Guarded on LIVE_PUBLISH_STATUSES, so a failure path arriving after the stall
    sweep already failed the row (or after it somehow finished) is a no-op
    rather than an overwrite — the same rule `_fail` follows for jobs.
    """
    try:
        if not db.transition_publish_status(
            publish_id,
            expect=db.LIVE_PUBLISH_STATUSES,
            to="failed",
            error=message,
            detail="Failed",
        ):
            logger.warning(
                "publish %s: already terminal; leaving its status alone", publish_id
            )
    except Exception:
        logger.exception("publish %s: could not record failed status", publish_id)


def _publish_run(publish_id: str, settings: Settings) -> None:
    """One publish, start to finish.

    Everything is re-read HERE rather than trusted from the row: the clip's file
    may have expired since the request, the connection may have been
    disconnected, and the platform's capability may have changed with a
    deployment. A queued publish is a request to post *now*, under the rules
    that hold now.
    """
    db.init_db()
    row = db.get_publish_job(publish_id)
    if row is None:
        raise _LostClaim(publish_id)

    # The claim. queued → uploading, and a run that loses it does nothing at
    # all: a second delivery of the same task must not upload the video twice.
    if not db.transition_publish_status(
        publish_id,
        expect="queued",
        to="uploading",
        progress=0.0,
        detail="Preparing the upload",
        error=None,
    ):
        raise _LostClaim(publish_id)

    target = publish.target_for(str(row["platform"]))
    if target is None:
        raise publish.PublishError(publish.unsupported(str(row["platform"])))

    connection = db.get_connection(str(row["connection_id"]))
    if connection is None or str(connection["user_id"]) != str(row["user_id"]):
        raise publish.PublishError(publish.CONNECTION_GONE)

    clip = db.get_clip(str(row["clip_id"]))
    if clip is None or str(clip["user_id"]) != str(row["user_id"]):
        raise publish.PublishError(publish.CLIP_UNKNOWN)
    file_path = str(clip.get("file_path") or "")
    if not file_path:
        # PUBLISH.md: an expired clip is REFUSED with a clear message rather
        # than turning into a 500 somewhere further down.
        raise publish.PublishError(publish.CLIP_EXPIRED)

    settings.ensure_dirs()
    storage = get_storage(settings)
    # The stored name's own extension, because a clip saved from the browser
    # engine may be WebM rather than MP4 — and what it IS decides what the
    # upload is allowed to say it is.
    media_type = clip_media_type(file_path) or "video/mp4"
    suffix = Path(file_path).suffix or ".mp4"
    dest = settings.tmp_dir / f"publish-{publish_id}{suffix}"
    local = storage.fetch_library_clip(file_path, dest)
    if local is None or not Path(local).is_file():
        # The row says there is a file and the storage backend disagrees: the
        # retention sweep landed between the request and this moment, or the
        # object is gone. Same event as an expired clip, so the same sentence.
        raise publish.PublishError(publish.CLIP_EXPIRED)
    # A copy this run made (S3) rather than the library's own file (local
    # disk). Only the copy is ours to delete, and it is deleted on EVERY exit —
    # a failed upload leaves a whole video behind otherwise, in a tmp directory
    # nothing else sweeps.
    staged = Path(local) if Path(local) == dest else None
    try:
        request = publish.PublishRequest(
            clip=clip,
            file=Path(local),
            size_bytes=Path(local).stat().st_size,
            title=str(row["title"] or ""),
            description=str(row["description"] or ""),
            media_type=media_type,
            # Re-resolved against the CURRENT capability, never taken from the
            # row as gospel: a box whose app is still unverified must force
            # `private` even on a publish queued while somebody was mid-deploy.
            privacy=target.capability(settings).resolve(str(row["privacy"] or "")),
        )

        throttle = _Throttle()

        def on_progress(fraction: float, detail: str) -> None:
            if throttle.ready():
                db.update_publish_job(
                    publish_id,
                    progress=round(min(1.0, max(0.0, fraction)), 4),
                    detail=detail,
                )

        result = target.publish(settings, connection, request, on_progress)
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                logger.warning("publish %s: could not remove %s", publish_id, staged)

    if not db.transition_publish_status(
        publish_id,
        expect="uploading",
        to="done",
        progress=1.0,
        detail=f"Posted to {_platform_label(str(row['platform']))}",
        error=None,
        video_id=result.video_id,
        video_url=result.url,
    ):
        # The stall sweep failed this row while the upload was in flight. The
        # video IS on the channel, so the log is the only place left to say so —
        # and this run must not write `done` over a terminal status it lost.
        logger.warning(
            "publish %s finished as %s but its row is no longer ours",
            publish_id,
            result.video_id,
        )
        raise _LostClaim(publish_id)


def _platform_label(platform: str) -> str:
    """A platform's display name for a progress line ('YouTube', not 'Youtube')."""
    provider = connections.provider_for(platform)
    return provider.label if provider is not None else platform


def reconcile_stalled_publishes() -> int:
    """Fail publishes whose worker died mid-upload. Returns the count.

    The same cutoff as the render reconciler for the same reason: a row nobody
    is working on must not read `uploading` forever, because the sheet watching
    it would spin forever with it.
    """
    settings = get_settings()
    db.init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.render_timeout_s + STALL_SLACK_SECONDS
    )
    return db.reconcile_stalled_publishes(cutoff.isoformat(timespec="milliseconds"))


def reap_expired_publishes() -> int:
    """Delete finished publish rows past the job TTL; returns the count.

    A publish row is a receipt for one attempt, not a record of what is on
    somebody's channel — the video lives on YouTube and the clip lives in the
    library, and neither is touched here. Only terminal rows are reaped, so a
    live upload can never be swept out from under itself.
    """
    settings = get_settings()
    db.init_db()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=settings.job_ttl_hours)
    ).isoformat(timespec="milliseconds")
    return db.delete_publish_jobs_older_than(cutoff)


def reconcile_stalled() -> int:
    """Fail — and refund — jobs whose worker died mid-run. Returns the count.

    Anything still queued/processing whose last update predates a full render
    window plus slack has nobody left working on it, so nothing will ever
    settle the quota it reserved at /start. Both callers share this cutoff: the
    API's startup hook and the hourly beat sweep. Running it from the sweep as
    well is what makes the refund independent of restarts — an API process that
    stays up for weeks would otherwise never reconcile anything.

    Failing a row here is now genuinely terminal: db.reconcile_stalled fails and
    refunds in one guarded transaction, and _run cannot claim (or finish) a job
    it no longer owns. Before that, a backlog longer than the stall window meant
    the sweep refunded jobs the worker then went on to render and deliver.
    """
    settings = get_settings()
    db.init_db()
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.render_timeout_s + STALL_SLACK_SECONDS
    )
    return db.reconcile_stalled(cutoff.isoformat(timespec="milliseconds"))


def reap_expired() -> int:
    """Delete jobs (rows + rendered clips + source) older than the TTL.

    Cutoff is ``now - settings.job_ttl_hours``. For each expired job we release
    any quota it is still holding, then remove its clips directory, any stray
    source file on disk, and the DB row. Returns the number of jobs reaped.
    Safe to run repeatedly; unit-testable on its own.

    This is PIPELINE state and nothing else. A clip saved to somebody's library
    is a separate row with a separate lifetime, and its file lives under
    ``settings.library_dir`` — not under the ``clips_dir / job_id`` tree this
    rmtree walks — so a 48 h sweep of finished jobs cannot take a video out of
    a library that is meant to keep it for 90 days. The library has its own
    reaper (reap_expired_clips), which deletes files by their own deadline and
    keeps every row.
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
        # Settle BEFORE the row goes. The reservation's only settlement token
        # lives on the job row itself (jobs.usage_reserved), so a row deleted
        # while it still holds one bills the owner's month forever with nothing
        # left anywhere to hand it back — reconcile_stalled cannot recover a
        # row that no longer exists. This reaps by created_at regardless of
        # status, so it is the last thing standing between a job stuck queued
        # or processing and a free account losing a month it never rendered.
        # Settlement is once-only and the counter is clamped at zero, so a job
        # that already settled (every completed one) pays nothing extra here.
        db.settle_usage(job_id, 0)
        # Then drop the ROW before the files. A worker finishing inside this
        # window would otherwise win its processing→done guard against a row
        # about to be deleted and write clips after the rmtree, leaving files
        # on disk with nothing left to reap them. With the row gone first, that
        # worker loses its claim instead and discards its own output.
        db.delete_job(job_id)
        shutil.rmtree(settings.clips_dir / job_id, ignore_errors=True)
        _remove_source(storage, settings, job_id)
        reaped += 1
    return reaped


def reap_expired_clips() -> int:
    """Delete the FILE of every library clip past its window; KEEP the row.

    Retention (LIBRARY.md Part 2) is a promise about storage, not about
    memory: at ``expires_at`` the video goes and ``file_path`` becomes '', but
    the title, score, hooks and transcript stay forever. A row is deleted only
    when its owner deletes it. Clips with no deadline ('' — the enterprise
    window) are never selected at all.

    The file is removed BEFORE the row is blanked, and that order is the whole
    error handling: a sweep that dies in between leaves a row still naming a
    file that is already gone, which the next sweep selects again and finishes
    (deleting a missing file is a no-op). Blanking first would lose the only
    pointer to the file and leak it forever. A file that genuinely cannot be
    deleted leaves its row alone, so the next hour tries again instead of
    telling the owner their clip expired while it still sits on our disk.
    """
    settings = get_settings()
    db.init_db()
    storage = get_storage(settings)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    reaped = 0
    for clip in db.list_expired_clips(now):
        try:
            storage.delete_library_clip(clip["file_path"])
        except Exception:
            logger.warning(
                "clip %s: could not delete the expired file %s",
                clip["id"],
                clip["file_path"],
                exc_info=True,
            )
            continue
        if db.clear_clip_file(clip["id"]):
            reaped += 1
    return reaped


def purge_expired_login_codes() -> int:
    """Delete sign-in codes past their TTL; returns the count (EMAILAUTH.md).

    A tidy-up, not an enforcement: an expired code is already refused at
    verify, so a box whose beat never ran is no less safe — only untidier.
    """
    db.init_db()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return db.purge_expired_login_codes(now)


def purge_expired_oauth_states() -> int:
    """Delete abandoned authorizations; returns the count (PUBLISH.md Part 2).

    The same kind of tidy-up: a state past its ten minutes is already refused
    by db.consume_oauth_state, so this only clears the flows people started and
    never finished at the provider's consent screen.
    """
    db.init_db()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return db.purge_expired_oauth_states(now)


@celery_app.task(name="clipcatalyst.reap_expired")
def reap_expired_task() -> int:
    """Celery entry point for the hourly maintenance sweep (run by beat).

    Reconciliation runs FIRST and from here, not only from the API's startup
    hook: the hook fires on a restart, and a box that does not restart inside
    CC_JOB_TTL_HOURS would let the reaper delete a stranded row before any
    refund ran. Reconciling on the same hourly tick means a job abandoned by a
    dead worker gets its quota back within the hour, whatever the API process
    is doing. Returns the reaped count, as before.

    The library's own retention runs on the same tick and is independent in
    both directions: it never touches a job, and the job reaper never touches
    a library file.

    Expired sign-in codes are swept here too (EMAILAUTH.md), and abandoned
    OAuth authorizations with them (PUBLISH.md Part 2). Hygiene only: both
    already stopped working the moment their TTL passed, because neither
    db.get_login_code nor db.consume_oauth_state will return an expired row —
    this is what keeps dead secrets from accumulating in the tables, not what
    makes them dead.

    Publishes (PUBLISH.md Part 3) get both halves of the same treatment: an
    upload whose worker died is failed so the sheet watching it stops spinning,
    and finished rows are reaped on the job TTL. Neither touches the video on
    the channel or the clip in the library.
    """
    stalled = reconcile_stalled()
    if stalled:
        logger.info("reconciled %d stalled job(s)", stalled)
    stalled_publishes = reconcile_stalled_publishes()
    if stalled_publishes:
        logger.info("reconciled %d stalled publish(es)", stalled_publishes)
    count = reap_expired()
    if count:
        logger.info("reaper removed %d expired job(s)", count)
    expired_clips = reap_expired_clips()
    if expired_clips:
        logger.info("retention removed %d expired clip file(s)", expired_clips)
    expired_codes = purge_expired_login_codes()
    if expired_codes:
        logger.info("swept %d expired sign-in code(s)", expired_codes)
    expired_states = purge_expired_oauth_states()
    if expired_states:
        logger.info("swept %d abandoned authorization(s)", expired_states)
    expired_publishes = reap_expired_publishes()
    if expired_publishes:
        logger.info("reaped %d finished publish row(s)", expired_publishes)
    return count
