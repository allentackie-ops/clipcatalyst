"""ffmpeg clip renderer: trim, 9:16 crop/scale, burn ASS captions, x264+aac.

Uses input-side ``-ss``/``-to`` so decoding starts at the clip and output
timestamps are re-based to zero — which is exactly the timeline the (already
re-based) caption events in the generated .ass expect, and the timeline a
``CropTrack``'s keyframes live on. Progress is parsed from ``-progress pipe:1``
(out_time_us over the clip duration).

The crop window is the same 9:16 slice it always was; the only new thing is
*where* it sits. With a track, ``crop``'s ``x`` becomes an expression in ``t``
that ffmpeg re-evaluates every frame — a piecewise-linear replay of
``crop_center_at`` inside the filtergraph, which is what lets a single encode
pan without us decoding frames ourselves.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from ..settings import Settings
from .captions import build_ass
from .croptrack import CropKeyframe, CropTrack
from .types import ClipPlan, RenderedClip, RenderError, RenderOptions

_STDERR_TAIL_LINES = 15

#: Crop width: the full height at 9:16, or the whole frame if it is narrower.
#: Already filtergraph-escaped (the comma belongs to `min`, not the graph).
_CROP_W = "min(iw\\,ih*9/16)"

ProgressFn = Callable[[float], None]


def escape_subtitles_path(path: str) -> str:
    """Escape a filename for use as the `subtitles=` value inside -vf.

    Two escaping levels apply (see ffmpeg's "filtergraph escaping" docs): the
    filter-option parser (\\ : ') and then the filtergraph parser
    (\\ ' [ ] , ;).
    """
    level1 = "".join("\\" + c if c in "\\:'" else c for c in path)
    return "".join("\\" + c if c in "\\'[],;" else c for c in level1)


def escape_filter_expr(expr: str) -> str:
    """Escape an ffmpeg expression for embedding in a filtergraph argument.

    Only the filtergraph level applies (an expression has no option-parser
    metacharacters of its own), but its commas would otherwise read as filter
    separators — `if(lt(t,1),a,b)` would split the graph three ways.
    """
    return "".join("\\" + c if c in "\\'[],;" else c for c in expr)


def _num(value: float) -> str:
    """Compact fixed-point literal — ffmpeg's parser has no use for 1e-05."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _pan_expression(keyframes: Sequence[CropKeyframe]) -> str:
    """Piecewise-linear crop center over `t`, normalized 0..1 of source width.

    Built right-to-left so the nesting reads as a chain of "before this
    keyframe?" tests, with the last keyframe's value as the final else. The
    interpolation fraction is clipped to 0..1 so the ends hold flat instead of
    extrapolating off the frame. The builder caps keyframes at 48, which keeps
    this a few KB.
    """
    expr = _num(keyframes[-1].cx)
    for i in range(len(keyframes) - 2, -1, -1):
        a, b = keyframes[i], keyframes[i + 1]
        span = float(b.t) - float(a.t)
        if span <= 0:  # duplicate timestamps: nothing to interpolate across
            continue
        fraction = f"clip((t-{_num(a.t)})/{_num(span)},0,1)"
        segment = f"lerp({_num(a.cx)},{_num(b.cx)},{fraction})"
        expr = f"if(lt(t,{_num(b.t)}),{segment},{expr})"
    return expr


def build_crop_filter(track: CropTrack | None) -> str:
    """The `crop=` filter for one clip: centered, fixed, or panning.

    `x` is always clamped to `[0, iw-ow]` in the expression itself — the last
    guard before ffmpeg would refuse a window hanging off the frame edge.
    """
    if track is None or not track.keyframes:
        return f"crop={_CROP_W}:ih"  # centered, exactly as before tracking
    keyframes = list(track.keyframes)
    if track.is_static or len(keyframes) == 1:
        center = _num(keyframes[0].cx)  # one fixed window, no per-frame branching
    else:
        center = _pan_expression(keyframes)
    x = f"clip(({center})*iw-ow/2,0,iw-ow)"
    return f"crop={_CROP_W}:ih:{escape_filter_expr(x)}:0"


def render_clip(
    src_path: str | Path,
    plan: ClipPlan,
    out_path: str | Path,
    opts: RenderOptions,
    settings: Settings,
    on_progress: ProgressFn | None = None,
    track: CropTrack | None = None,
) -> RenderedClip:
    """Render one clip. `track` reframes on the speaker; None stays centered."""
    height = opts.height
    width = round(height * 9 / 16 / 2) * 2  # nearest even 9:16 width
    clip_duration = max(0.1, plan.end - plan.start)

    settings.ensure_dirs()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    ass_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".ass",
        prefix=f"cc-{plan.id}-",
        dir=settings.tmp_dir,
        delete=False,
    )
    stderr_file = tempfile.TemporaryFile(dir=settings.tmp_dir)
    try:
        with ass_file:
            ass_file.write(build_ass(plan, height, opts.watermark))

        vf = (
            f"{build_crop_filter(track)},"
            f"scale={width}:{height},"
            f"subtitles={escape_subtitles_path(ass_file.name)}"
        )
        cmd = [
            settings.ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{plan.start:.3f}",
            "-to",
            f"{plan.end:.3f}",
            "-i",
            str(src_path),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            opts.preset,
            "-crf",
            str(opts.crf),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(out_path),
        ]

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True
            )
        except OSError as exc:
            raise RenderError(
                "ffmpeg is not available, so the clip could not be rendered. "
                "Check CC_FFMPEG_BIN."
            ) from exc

        assert proc.stdout is not None
        for line in proc.stdout:  # -progress key=value stream
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and on_progress is not None:
                try:
                    done = int(value) / 1_000_000 / clip_duration
                except ValueError:
                    continue
                on_progress(min(1.0, max(0.0, done)))
            elif key == "progress" and value == "end" and on_progress is not None:
                on_progress(1.0)
        returncode = proc.wait()

        if returncode != 0:
            stderr_file.seek(0)
            tail = stderr_file.read().decode("utf-8", "replace").strip().splitlines()
            detail = "\n".join(tail[-_STDERR_TAIL_LINES:])
            raise RenderError(
                f"Rendering failed (ffmpeg exit {returncode}).\n{detail}"
            )
    finally:
        stderr_file.close()
        Path(ass_file.name).unlink(missing_ok=True)

    return RenderedClip(plan=plan, path=str(out_path), width=width, height=height)
