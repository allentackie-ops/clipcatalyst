"""Media probing: duration, dimensions, audio presence.

Prefers ffprobe (`settings.ffprobe_bin`) with JSON output. When ffprobe is not
installed (e.g. the sandbox ships only the static imageio-ffmpeg binary), falls
back to parsing the stderr banner of ``ffmpeg -hide_banner -i <path>`` for the
``Duration:`` and ``Stream ... Video/Audio`` lines.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..settings import Settings
from .types import MediaInfo, PipelineError

_UNREADABLE_MESSAGE = (
    "Couldn't read this video. The file may be corrupt, DRM-protected, or use "
    "an unsupported format — try re-exporting it as a standard MP4 "
    "(H.264 video + AAC audio)."
)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #[^\n]*?: Video:[^\n]*?(\d{2,5})x(\d{2,5})")
_AUDIO_RE = re.compile(r"Stream #[^\n]*?: Audio:")


def probe_media(path: str | Path, settings: Settings) -> MediaInfo:
    """Probe a media file, raising PipelineError with a friendly message."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise PipelineError(_UNREADABLE_MESSAGE)

    info = _probe_with_ffprobe(path, settings)
    if info is None:
        info = _probe_with_ffmpeg(path, settings)
    if info is None or info.duration <= 0:
        raise PipelineError(_UNREADABLE_MESSAGE)
    return info


def probe_image_size(path: str | Path, settings: Settings) -> tuple[int, int] | None:
    """Pixel size of a still image, or None when it cannot be read.

    Deliberately total where ``probe_media`` raises: its only caller is the
    brand-kit logo overlay, which must degrade to no overlay rather than fail
    somebody's render (BRANDKIT.md — "a brand kit must never fail a render").
    A still has no duration, so it can never go through ``probe_media``, whose
    contract is a playable clip.

    ffprobe first; when the box ships only the static ffmpeg binary (no
    ffprobe on PATH), the same stderr banner ``_probe_with_ffmpeg`` parses
    carries the dimensions. An unreadable file, an unsupported codec (an SVG
    on a build with no SVG decoder), or a missing binary all answer None.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
    except OSError:
        return None

    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            streams = json.loads(proc.stdout or "{}").get("streams", [])
            if streams:
                width = int(streams[0].get("width") or 0)
                height = int(streams[0].get("height") or 0)
                if width > 0 and height > 0:
                    return width, height
            return None
    except (OSError, subprocess.TimeoutExpired):
        pass  # ffprobe missing/unusable — the ffmpeg banner below still knows
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

    try:
        proc = subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VIDEO_RE.search(proc.stderr or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _probe_with_ffprobe(path: Path, settings: Settings) -> MediaInfo | None:
    """Run ffprobe; None when the binary itself is unavailable."""
    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None  # ffprobe missing/unusable → let the ffmpeg fallback try
    if proc.returncode != 0:
        raise PipelineError(_UNREADABLE_MESSAGE)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise PipelineError(_UNREADABLE_MESSAGE) from None

    duration = 0.0
    try:
        duration = float(data.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    width = height = 0
    has_audio = False
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and width == 0:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if duration <= 0:
                try:
                    duration = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif codec_type == "audio":
            has_audio = True
    return MediaInfo(duration=duration, width=width, height=height, has_audio=has_audio)


def _probe_with_ffmpeg(path: Path, settings: Settings) -> MediaInfo | None:
    """Parse `ffmpeg -hide_banner -i path` stderr (exits non-zero by design)."""
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-i", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipelineError(
            "ffmpeg is not available on this machine, so the video could not "
            "be probed. Check CC_FFMPEG_BIN."
        ) from exc
    stderr = proc.stderr or ""

    m = _DURATION_RE.search(stderr)
    if not m:
        raise PipelineError(_UNREADABLE_MESSAGE)
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    width = height = 0
    vm = _VIDEO_RE.search(stderr)
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
    has_audio = _AUDIO_RE.search(stderr) is not None
    return MediaInfo(duration=duration, width=width, height=height, has_audio=has_audio)
