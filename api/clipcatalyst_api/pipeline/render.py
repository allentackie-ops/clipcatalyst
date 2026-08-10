"""ffmpeg clip renderer: trim, 9:16 crop/scale, burn ASS captions, x264+aac.

Uses input-side ``-ss``/``-to`` so decoding starts at the clip and output
timestamps are re-based to zero — which is exactly the timeline the (already
re-based) caption events in the generated .ass expect. Progress is parsed from
``-progress pipe:1`` (out_time_us over the clip duration).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from ..settings import Settings
from .captions import build_ass
from .types import ClipPlan, RenderedClip, RenderError, RenderOptions

_STDERR_TAIL_LINES = 15

ProgressFn = Callable[[float], None]


def escape_subtitles_path(path: str) -> str:
    """Escape a filename for use as the `subtitles=` value inside -vf.

    Two escaping levels apply (see ffmpeg's "filtergraph escaping" docs): the
    filter-option parser (\\ : ') and then the filtergraph parser
    (\\ ' [ ] , ;).
    """
    level1 = "".join("\\" + c if c in "\\:'" else c for c in path)
    return "".join("\\" + c if c in "\\'[],;" else c for c in level1)


def render_clip(
    src_path: str | Path,
    plan: ClipPlan,
    out_path: str | Path,
    opts: RenderOptions,
    settings: Settings,
    on_progress: ProgressFn | None = None,
) -> RenderedClip:
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
            "crop=min(iw\\,ih*9/16):ih,"
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
