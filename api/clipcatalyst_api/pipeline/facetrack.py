"""Face detection for the cloud renderer (the `reframe` stage's eyes).

Frames are pulled from ffmpeg at ~2 Hz, already scaled to 320 px wide, and
handed to OpenCV's bundled `haarcascade_frontalface_default.xml`. 2 Hz is
plenty: `croptrack` smooths and rate-limits the camera anyway, so denser
sampling would only cost CPU. 320 px keeps Haar cheap — detection cost scales
with pixels, and a face worth cropping to is far bigger than the 8 % minimum.

Frames are read one at a time off the pipe, never buffered whole: a 60 s 4K
clip is ~100 frames of 320x180, but the *source* frames it decodes from would
be gigabytes.

Everything here is best-effort. A clip with no detectable face (animation,
b-roll, a back of a head) returns `[]`, which `build_crop_track` turns into a
centered track. Only ffmpeg failing to *launch* raises — a bad detection day
must never fail a render.

Horizontal values are normalized to the *displayed* source width (0..1),
matching `croptrack.py` and the `iw` the renderer's crop filter sees; uniform
scaling means x/320 on the detect frame is already the same fraction on the
source.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ..settings import Settings
from .croptrack import FaceSample
from .types import MediaInfo, PipelineError

log = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]

#: Detection frames per second of clip.
SAMPLE_FPS = 2.0
#: Width every frame is scaled to before detection.
DETECT_WIDTH = 320
#: Smallest face we bother with, as a fraction of frame width.
MIN_FACE_FRACTION = 0.08
#: Haar knobs (see cv2.CascadeClassifier.detectMultiScale).
SCALE_FACTOR = 1.1
MIN_NEIGHBORS = 4
CASCADE_NAME = "haarcascade_frontalface_default.xml"

_OFF_VALUES = {"", "0", "off", "false", "no", "none", "disabled"}

#: How ffmpeg reports a display matrix, in both the banner and ffprobe's JSON.
_ROTATION_RE = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")
#: The older QuickTime spelling of the same thing: a stream-level `rotate` tag.
_ROTATE_TAG_RE = re.compile(r"^\s*rotate\s*:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE)

_NO_FFMPEG = (
    "ffmpeg is not available, so the clip could not be scanned for faces. "
    "Check CC_FFMPEG_BIN."
)


def face_tracking_enabled(settings: Settings) -> bool:
    """Whether `CC_FACE_TRACKING` allows detection (default: yes)."""
    value = getattr(settings, "face_tracking", "on")
    return str(value).strip().lower() not in _OFF_VALUES


def detect_faces(
    video_path: str | Path,
    start: float,
    end: float,
    settings: Settings,
    on_progress: ProgressFn | None = None,
    info: MediaInfo | None = None,
) -> list[FaceSample]:
    """Sample `[start, end]` at ~2 Hz and return every face found.

    One `FaceSample` per detected face per frame — multi-face frames are kept
    intact so `build_crop_track` can do the subject picking. `t` is relative to
    `start`. Returns `[]` (never raises) when tracking is off, OpenCV is
    missing, the cascade won't load, ffmpeg exits non-zero, or simply nothing
    was found. Raises `PipelineError` only if ffmpeg cannot be executed at all.

    Pass `info` when the caller already probed this file (the worker holds one
    for the whole job) — otherwise every clip pays for its own probe.
    """
    duration = float(end) - float(start)
    if duration <= 0:
        return []
    if not face_tracking_enabled(settings):
        return []
    if shutil.which(settings.ffmpeg_bin) is None:
        # Checked up front because the probe below would otherwise swallow it
        # as "unreadable file" — a missing decoder is a real misconfiguration.
        raise PipelineError(_NO_FFMPEG)

    detector = _load_detector()
    if detector is None:
        return []
    cv2, np, cascade = detector

    size = _detect_size(video_path, settings, info)
    if size is None:
        return []
    width, height = size
    frame_bytes = width * height * 3
    min_side = max(8, int(width * MIN_FACE_FRACTION))
    # A couple of frames of slack: fps filtering can emit one extra at the tail.
    max_frames = int(duration * SAMPLE_FPS) + 4

    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        f"{float(start):.3f}",
        "-to",
        f"{float(end):.3f}",
        "-i",
        str(video_path),
        "-an",
        "-sn",
        "-vf",
        f"fps={SAMPLE_FPS:g},scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError as exc:  # the one genuine "cannot even try" case
        raise PipelineError(_NO_FFMPEG) from exc

    samples: list[FaceSample] = []
    assert proc.stdout is not None
    try:
        for index in range(max_frames):
            buffer = _read_exactly(proc.stdout, frame_bytes)
            if buffer is None:
                break
            t = index / SAMPLE_FPS
            frame = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 3)
            for face in _detect_frame(cv2, cascade, frame, min_side):
                x, _y, w, _h = face
                samples.append(
                    FaceSample(
                        t=round(t, 3),
                        cx=(float(x) + float(w) / 2) / width,
                        size=float(w) / width,
                        score=1.0,
                    )
                )
            if on_progress is not None:
                on_progress(min(1.0, (index + 1) / max(1.0, duration * SAMPLE_FPS)))
    except Exception:  # decoding/detection hiccup: keep what we have
        log.warning("face detection stopped early", exc_info=True)
    finally:
        _shutdown(proc)

    if proc.returncode not in (0, None) and not samples:
        # Unreadable input, bad seek, etc. Centered crop is a fine answer.
        log.info("face detection found nothing (ffmpeg exit %s)", proc.returncode)
        return []
    if on_progress is not None:
        on_progress(1.0)
    return samples


def _load_detector():
    """`(cv2, numpy, CascadeClassifier)` or None when anything is unusable."""
    try:
        import cv2  # noqa: PLC0415 — optional dep, and the import is not cheap
        import numpy as np  # noqa: PLC0415
    except Exception:
        log.info("opencv is not installed; face tracking disabled")
        return None
    try:
        path = str(Path(cv2.data.haarcascades) / CASCADE_NAME)
        cascade = cv2.CascadeClassifier(path)
        if cascade.empty():
            log.warning("haar cascade missing at %s", path)
            return None
    except Exception:
        # opencv-python-headless 5.x dropped the bundled cascades entirely,
        # which is why requirements.txt pins <5.
        log.warning("could not load the haar cascade", exc_info=True)
        return None
    return cv2, np, cascade


def _detect_frame(cv2, cascade, frame, min_side: int):
    """Faces as (x, y, w, h) in detect-frame pixels; [] on any failure."""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        found = cascade.detectMultiScale(
            gray,
            scaleFactor=SCALE_FACTOR,
            minNeighbors=MIN_NEIGHBORS,
            minSize=(min_side, min_side),
        )
    except Exception:
        log.debug("detection failed on one frame", exc_info=True)
        return []
    if found is None or len(found) == 0:
        return []
    return [tuple(int(v) for v in face) for face in found]


def _detect_size(
    video_path: str | Path, settings: Settings, info: MediaInfo | None = None
) -> tuple[int, int] | None:
    """`(width, height)` of the frames ffmpeg will put on the pipe, or None.

    `scale=320:-1` reads better but leaves the reader guessing at the row
    count, and a wrong guess silently shears every frame into nonsense the
    detector might still fire on. So we pass an explicit height — and it has to
    be the height of the frame ffmpeg *emits*, not the one stored in the file.

    Those differ constantly. ffmpeg autorotates from the display matrix on the
    way in, before the filtergraph, and every phone shooting portrait stores a
    landscape stream plus a quarter-turn matrix. Sizing from those coded
    dimensions force-scales an upright 9:16 frame into a 16:9 box, which
    stretches every face to twice its width — past what Haar will accept, so
    detection quietly drops to zero on the most common source there is.

    Give up on tracking (None) rather than guess: a source we can't measure is
    one the render will report on anyway.
    """
    source = _source_size(video_path, settings, info)
    if source is None:
        return None
    width, height = source
    if _is_sideways(_display_rotation(video_path, settings)):
        width, height = height, width
    return DETECT_WIDTH, max(2, round(DETECT_WIDTH * height / width))


def _source_size(
    video_path: str | Path, settings: Settings, info: MediaInfo | None
) -> tuple[int, int] | None:
    """Coded frame size — from the caller's probe when it already has one."""
    if info is not None and info.width > 0 and info.height > 0:
        return info.width, info.height
    try:
        from .probe import probe_media  # noqa: PLC0415 — avoids an import cycle

        probed = probe_media(video_path, settings)
    except Exception:
        log.info("could not probe %s for face tracking", video_path)
        return None
    if probed.width <= 0 or probed.height <= 0:
        return None
    return probed.width, probed.height


def _is_sideways(rotation: float) -> bool:
    """True when the display matrix turns the picture a quarter turn."""
    try:
        return round(abs(float(rotation))) % 180 == 90
    except (TypeError, ValueError):
        return False


def _display_rotation(video_path: str | Path, settings: Settings) -> float:
    """Degrees ffmpeg will rotate this file by on decode; 0 when we can't tell.

    Memoized on the file's identity: a job scans the same source once per clip,
    and its display matrix cannot change underneath us.
    """
    path = Path(video_path)
    try:
        stat = path.stat()
        fingerprint: tuple[int, int] | None = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        fingerprint = None  # unreadable; the size lookup will have said so too
    return _rotation_of(str(path), fingerprint, settings.ffprobe_bin, settings.ffmpeg_bin)


@lru_cache(maxsize=32)
def _rotation_of(
    path: str,
    fingerprint: tuple[int, int] | None,
    ffprobe_bin: str,
    ffmpeg_bin: str,
) -> float:
    """ffprobe's answer, else the `ffmpeg -i` banner's, else "no rotation"."""
    for reader, binary in (
        (_rotation_from_ffprobe, ffprobe_bin),
        (_rotation_from_ffmpeg, ffmpeg_bin),
    ):
        try:
            degrees = reader(binary, path)
        except Exception:  # missing binary, timeout, unparseable output
            log.debug("could not read rotation via %s", binary, exc_info=True)
            continue
        if degrees is not None:
            return degrees
    return 0.0


def _rotation_from_ffprobe(binary: str, path: str) -> float | None:
    """Display-matrix rotation from ffprobe JSON; None when it can't answer."""
    proc = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-print_format",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    streams = json.loads(proc.stdout or "{}").get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    for side_data in stream.get("side_data_list") or []:
        rotation = side_data.get("rotation")
        if rotation is not None:
            return float(rotation)
    tag = (stream.get("tags") or {}).get("rotate")
    return float(tag) if tag is not None else 0.0


def _rotation_from_ffmpeg(binary: str, path: str) -> float | None:
    """Same, parsed from the `ffmpeg -i` banner — used when ffprobe is absent."""
    proc = subprocess.run(
        [binary, "-hide_banner", "-i", path],  # exits non-zero: no output file
        capture_output=True,
        text=True,
        timeout=60,
    )
    stderr = proc.stderr or ""
    if "Video:" not in stderr:
        return None  # nothing decodable was reported — don't invent an answer
    match = _ROTATION_RE.search(stderr) or _ROTATE_TAG_RE.search(stderr)
    return float(match.group(1)) if match else 0.0


def _read_exactly(stream, count: int) -> bytes | None:
    """Exactly `count` bytes, or None at a clean/short end of stream."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None  # EOF (a trailing partial frame is not usable)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _shutdown(proc: subprocess.Popen) -> None:
    """Close the pipe and reap ffmpeg, killing it if it ignores the hint."""
    if proc.stdout is not None:
        try:
            proc.stdout.close()  # EPIPE tells ffmpeg to stop early
        except Exception:
            pass
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
