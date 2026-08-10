"""Time-varying crop tests — verified by *rendering*, not by reading strings.

An ffmpeg expression can be perfectly plausible and still not pan: a mis-escaped
comma splits the filtergraph, `t` may not be what you think after an input-side
seek, and `crop` only re-evaluates `x` per frame if you gave it one. So the core
tests here encode a real file from a hard left-red / right-blue source and then
decode the result with OpenCV: if the dominant colour walks from red to blue,
the crop window really moved.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from clipcatalyst_api.pipeline.croptrack import CropKeyframe, CropTrack
from clipcatalyst_api.pipeline.probe import probe_media
from clipcatalyst_api.pipeline.render import (
    build_crop_filter,
    escape_filter_expr,
    render_clip,
)
from clipcatalyst_api.pipeline.types import ClipPlan, RenderOptions
from clipcatalyst_api.settings import get_settings

SOURCE_W, SOURCE_H = 640, 360
SOURCE_SECONDS = 6
OUT_HEIGHT = 480  # 270x480 — small enough to decode every frame in a test

# The 9:16 window cut from 640x360 is 202 px wide, so it fits entirely inside
# one colour half (320 px) whenever its centre is at 0.2 or 0.8 of the width.
CROP_W = int(SOURCE_H * 9 / 16)


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def split_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 6 s 640x360 clip: left half pure red, right half pure blue."""
    get_settings.cache_clear()
    ffmpeg = get_settings().ffmpeg_bin
    get_settings.cache_clear()
    path = tmp_path_factory.mktemp("split") / "split.mp4"
    half = f"{SOURCE_W // 2}x{SOURCE_H}"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=red:s={half}:r=15:d={SOURCE_SECONDS}",
            "-f", "lavfi", "-i", f"color=blue:s={half}:r=15:d={SOURCE_SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_SECONDS}",
            "-filter_complex", "[0:v][1:v]hstack=inputs=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


def make_plan(start: float, end: float) -> ClipPlan:
    """A wordless, watermark-free plan so nothing is burned over the colours."""
    return ClipPlan(
        id="crop-1", start=start, end=end, score=80, title="Crop test",
        hooks=[], reason="", tip="", words=[],
    )


def redness(frame: np.ndarray) -> float:
    """mean(R) - mean(B) over the middle band: >0 red, <0 blue, ~0 split."""
    band = frame[frame.shape[0] // 4 : frame.shape[0] * 3 // 4]
    return float(band[:, :, 2].mean()) - float(band[:, :, 0].mean())


def sample_redness(path: Path) -> list[tuple[float, float]]:
    """(timestamp, redness) for every decoded frame of the rendered clip."""
    capture = cv2.VideoCapture(str(path))
    assert capture.isOpened(), f"OpenCV could not open {path}"
    fps = capture.get(cv2.CAP_PROP_FPS) or 15.0
    out: list[tuple[float, float]] = []
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        out.append((index / fps, redness(frame)))
        index += 1
    capture.release()
    assert out, "no frames decoded from the rendered clip"
    return out


def unescaped_commas(text: str) -> int:
    """Commas the filtergraph parser would treat as filter separators."""
    count = 0
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == ",":
            count += 1
        i += 1
    return count


# --- The rendered proof ----------------------------------------------------


def test_moving_track_pans_red_to_blue(split_source: Path, settings, tmp_path: Path) -> None:
    """A 0.2 -> 0.8 pan must actually walk the encoded frames red -> blue."""
    plan = make_plan(0.5, 4.5)
    duration = plan.end - plan.start
    track = CropTrack(
        keyframes=[CropKeyframe(t=0.0, cx=0.2), CropKeyframe(t=duration, cx=0.8)],
        is_static=False,
        coverage=1.0,
    )
    out = tmp_path / "pan.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings, track=track,
    )

    info = probe_media(out, settings)
    assert (info.width, info.height) == (270, OUT_HEIGHT)

    series = sample_redness(out)
    assert series[0][1] > 60, "clip should open on the red half"
    assert series[-1][1] < -60, "clip should end on the blue half"

    # Halfway the window straddles the colour boundary — a gradual pan, not a cut.
    middle = min(series, key=lambda s: abs(s[0] - duration / 2))[1]
    assert abs(middle) < 40, f"midpoint should be split red/blue, got {middle}"

    # Monotone travel: redness never meaningfully climbs back.
    values = [v for _, v in series]
    assert all(b - a < 12 for a, b in zip(values, values[1:])), "pan reversed"


def test_pan_keyframes_use_clip_relative_time(
    split_source: Path, settings, tmp_path: Path
) -> None:
    """Keyframe `t` is seconds from clip start, not from the source start.

    The clip starts 1.5 s into the source, so a step keyframed at t=2.0 must
    land 2.0 s into the *output*. If the expression read raw source timestamps
    it would fire at 0.5 s instead.
    """
    plan = make_plan(1.5, 5.5)
    track = CropTrack(
        keyframes=[
            CropKeyframe(t=0.0, cx=0.2),
            CropKeyframe(t=2.0, cx=0.2),
            CropKeyframe(t=2.1, cx=0.8),
            CropKeyframe(t=4.0, cx=0.8),
        ],
        is_static=False,
        coverage=1.0,
    )
    out = tmp_path / "step.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings, track=track,
    )

    series = sample_redness(out)
    crossings = [t for (t, v), (_, nv) in zip(series, series[1:]) if v > 0 >= nv]
    assert len(crossings) == 1, f"expected one red->blue switch, got {crossings}"
    assert crossings[0] == pytest.approx(2.05, abs=0.35)


def test_static_track_holds_an_off_center_window(
    split_source: Path, settings, tmp_path: Path
) -> None:
    """A static track parks the window on one side for the whole clip."""
    plan = make_plan(0.5, 3.0)
    track = CropTrack(
        keyframes=[CropKeyframe(t=0.0, cx=0.8)], is_static=True, coverage=1.0
    )
    out = tmp_path / "static.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings, track=track,
    )

    values = [v for _, v in sample_redness(out)]
    assert max(values) < -60, "every frame should be blue"


def test_no_track_keeps_the_centered_crop(
    split_source: Path, settings, tmp_path: Path
) -> None:
    """Without a track the window straddles the split, exactly as it always did."""
    plan = make_plan(0.5, 3.0)
    out = tmp_path / "centered.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings,
    )

    values = [v for _, v in sample_redness(out)]
    assert all(abs(v) < 40 for v in values), "centered crop should stay half/half"


@pytest.mark.parametrize(
    ("cx", "expect_red"), [(-0.4, True), (1.4, False)],
)
def test_out_of_range_center_is_clamped_into_the_frame(
    split_source: Path, settings, tmp_path: Path, cx: float, expect_red: bool
) -> None:
    """x is clamped to [0, iw-ow]: a nonsense centre renders an edge window."""
    plan = make_plan(0.5, 2.5)
    track = CropTrack(
        keyframes=[CropKeyframe(t=0.0, cx=cx)], is_static=True, coverage=1.0
    )
    out = tmp_path / f"clamp-{cx}.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings, track=track,
    )

    values = [v for _, v in sample_redness(out)]
    if expect_red:
        assert min(values) > 60, "clamped to x=0 -> the far-left (red) window"
    else:
        assert max(values) < -60, "clamped to x=iw-ow -> the far-right (blue) window"


def test_max_keyframe_track_renders(split_source: Path, settings, tmp_path: Path) -> None:
    """48 keyframes (the builder's hard cap) still make a filtergraph ffmpeg takes."""
    plan = make_plan(0.5, 4.5)
    duration = plan.end - plan.start
    keyframes = [
        CropKeyframe(t=duration * i / 47, cx=0.2 + 0.6 * i / 47) for i in range(48)
    ]
    out = tmp_path / "many.mp4"

    render_clip(
        split_source, plan, out,
        RenderOptions(height=OUT_HEIGHT, watermark=False), settings,
        track=CropTrack(keyframes=keyframes, is_static=False, coverage=1.0),
    )

    series = sample_redness(out)
    assert series[0][1] > 60 and series[-1][1] < -60


# --- Expression shape (cheap guards around the rendered proof) -------------


def test_no_track_filter_is_byte_for_byte_the_old_one() -> None:
    assert build_crop_filter(None) == "crop=min(iw\\,ih*9/16):ih"
    assert build_crop_filter(CropTrack(keyframes=[])) == "crop=min(iw\\,ih*9/16):ih"


def test_static_filter_is_a_constant_x() -> None:
    track = CropTrack(keyframes=[CropKeyframe(t=0.0, cx=0.8)], is_static=True)
    crop = build_crop_filter(track)
    assert crop == "crop=min(iw\\,ih*9/16):ih:clip((0.8)*iw-ow/2\\,0\\,iw-ow):0"
    assert "if(" not in crop  # no per-frame branching for a shot that never moves


def test_moving_filter_is_piecewise_linear_and_bounded() -> None:
    track = CropTrack(
        keyframes=[
            CropKeyframe(t=0.0, cx=0.3),
            CropKeyframe(t=1.0, cx=0.5),
            CropKeyframe(t=2.0, cx=0.7),
        ],
        is_static=False,
    )
    crop = build_crop_filter(track)
    assert crop.count("if(lt(t\\,") == 2  # one test per interior boundary
    assert crop.count("lerp(") == 2
    assert crop.count("clip(") == 3  # two fractions + the final x clamp
    assert "iw-ow" in crop  # clamped to the legal window range


def test_expression_never_leaks_a_filtergraph_separator() -> None:
    """Every comma in the crop filter is escaped, so it stays one filter."""
    track = CropTrack(
        keyframes=[CropKeyframe(t=i / 2, cx=0.2 + i / 40) for i in range(20)],
        is_static=False,
    )
    assert unescaped_commas(build_crop_filter(track)) == 0
    assert unescaped_commas(build_crop_filter(None)) == 0
    assert escape_filter_expr("if(lt(t,1),a,b)") == "if(lt(t\\,1)\\,a\\,b)"
    assert escape_filter_expr("min(iw;ih)[x]'") == "min(iw\\;ih)\\[x\\]\\'"


def test_duplicate_timestamps_do_not_divide_by_zero() -> None:
    track = CropTrack(
        keyframes=[
            CropKeyframe(t=0.0, cx=0.3),
            CropKeyframe(t=0.0, cx=0.4),
            CropKeyframe(t=1.0, cx=0.7),
        ],
        is_static=False,
    )
    crop = build_crop_filter(track)
    assert "/0)" not in crop and unescaped_commas(crop) == 0


def test_track_is_optional_and_keyword_compatible(settings) -> None:
    """`track` is an add-on: existing positional callers are untouched."""
    import inspect

    params = list(inspect.signature(render_clip).parameters)
    assert params[:6] == [
        "src_path", "plan", "out_path", "opts", "settings", "on_progress",
    ]
    assert params[6] == "track"
    assert inspect.signature(render_clip).parameters["track"].default is None


def test_replaced_settings_do_not_disturb_the_crop(settings) -> None:
    """Sanity: the crop filter depends only on the track, not on settings."""
    quiet = replace(settings, face_tracking="off")
    assert quiet.face_tracking == "off"
    assert build_crop_filter(None) == "crop=min(iw\\,ih*9/16):ih"
