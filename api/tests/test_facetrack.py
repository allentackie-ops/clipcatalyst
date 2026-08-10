"""Face-detection tests against real, generated video.

Haar is picky, which is the point: rather than mock it, these tests draw a
crude but genuinely detectable face (light oval, brow shadow band, two dark
eye ellipses, a mouth), encode it with ffmpeg, and run the whole
decode-scale-detect path. A moving face must produce samples whose `cx`
climbs; anything the detector can't see must degrade to `[]` rather than raise,
because a bad detection day may never fail a render.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from clipcatalyst_api.pipeline.croptrack import CropTrackOptions, build_crop_track
from clipcatalyst_api.pipeline.facetrack import (
    detect_faces,
    face_tracking_enabled,
)
from clipcatalyst_api.pipeline.types import MediaInfo, PipelineError
from clipcatalyst_api.settings import get_settings

SOURCE_W, SOURCE_H = 640, 360
SOURCE_FPS = 15
MOVING_SECONDS = 8.0
FACE_WIDTH = 150  # ~23% of frame width — comfortably above the 8% minimum

# A phone-shaped portrait clip: displayed 360x640, stored as landscape pixels.
PORTRAIT_W, PORTRAIT_H = 360, 640
PORTRAIT_SECONDS = 4.0
PORTRAIT_FACE_WIDTH = 110  # ~31% of the displayed width
PORTRAIT_FACE_CX = 0.3  # fraction of the DISPLAYED width


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def draw_face(frame: np.ndarray, cx: int, cy: int, width: int) -> None:
    """The minimum a Haar frontal-face cascade will accept as a face.

    The cascade keys off brightness relationships, not detail: a bright oval,
    a darker band where the brows are, and two dark eyes below it. Proportions
    matter more than realism — these were tuned until detection is reliable
    after the 640 -> 320 downscale the pipeline does.
    """
    height = int(width * 1.3)
    cv2.ellipse(frame, (cx, cy), (width // 2, height // 2), 0, 0, 360, (225, 225, 225), -1)
    brow = cy - int(height * 0.08)
    cv2.rectangle(
        frame,
        (cx - int(width * 0.45), brow - int(height * 0.05)),
        (cx + int(width * 0.45), brow + int(height * 0.05)),
        (100, 100, 100),
        -1,
    )
    eye_y = cy - int(height * 0.02)
    eye_dx = int(width * 0.17)
    radius = (int(width * 0.11), int(height * 0.088))
    cv2.ellipse(frame, (cx - eye_dx, eye_y), radius, 0, 0, 360, (20, 20, 20), -1)
    cv2.ellipse(frame, (cx + eye_dx, eye_y), radius, 0, 0, 360, (20, 20, 20), -1)
    cv2.ellipse(
        frame,
        (cx, cy + int(height * 0.22)),
        (int(width * 0.20), int(height * 0.05)),
        0, 0, 360, (55, 55, 55), -1,
    )


def encode(path: Path, frames, ffmpeg: str, size: tuple[int, int] | None = None) -> Path:
    """Encode BGR frames to H.264 — the detector sees compressed video, as in life."""
    width, height = size or (SOURCE_W, SOURCE_H)
    proc = subprocess.Popen(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-r", str(SOURCE_FPS), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(path),
        ],
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for frame in frames:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0, "test fixture failed to encode"
    return path


def blank() -> np.ndarray:
    return np.full((SOURCE_H, SOURCE_W, 3), 90, np.uint8)


@pytest.fixture(scope="module")
def ffmpeg_bin() -> str:
    get_settings.cache_clear()
    binary = get_settings().ffmpeg_bin
    get_settings.cache_clear()
    return binary


@pytest.fixture(scope="module")
def moving_face_video(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """One face sliding left to right, 0.2 -> 0.8 of the frame width."""
    count = int(MOVING_SECONDS * SOURCE_FPS)

    def frames():
        for i in range(count):
            progress = i / (count - 1)
            frame = blank()
            draw_face(frame, int((0.2 + 0.6 * progress) * SOURCE_W), SOURCE_H // 2, FACE_WIDTH)
            yield frame

    path = tmp_path_factory.mktemp("faces") / "moving.mp4"
    return encode(path, frames(), ffmpeg_bin)


@pytest.fixture(scope="module")
def two_face_video(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """Two stationary faces — a podcast two-shot."""
    count = int(4.0 * SOURCE_FPS)

    def frames():
        for _ in range(count):
            frame = blank()
            draw_face(frame, int(0.27 * SOURCE_W), SOURCE_H // 2, 130)
            draw_face(frame, int(0.73 * SOURCE_W), SOURCE_H // 2, 130)
            yield frame

    path = tmp_path_factory.mktemp("faces") / "two.mp4"
    return encode(path, frames(), ffmpeg_bin)


@pytest.fixture(scope="module")
def rotated_portrait_video(
    tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str
) -> Path:
    """What a phone actually uploads: landscape pixels plus a rotation tag.

    Phones record the sensor's landscape frame and describe the quarter turn in
    the container's display matrix. ffmpeg applies that turn on decode, before
    any filter runs, so what reaches the detector is an upright 360x640 frame —
    while ffprobe still reports the coded 640x360. The stored picture here is
    therefore the portrait frame rotated a quarter turn, exactly the shape that
    sizing the pipe from coded dimensions would wrongly squash it back into.
    """
    upright = np.full((PORTRAIT_H, PORTRAIT_W, 3), 90, np.uint8)
    draw_face(
        upright, int(PORTRAIT_FACE_CX * PORTRAIT_W), PORTRAIT_H // 2, PORTRAIT_FACE_WIDTH
    )
    coded = np.ascontiguousarray(np.rot90(upright))  # ffmpeg turns it back
    count = int(PORTRAIT_SECONDS * SOURCE_FPS)

    directory = tmp_path_factory.mktemp("faces")
    flat = encode(
        directory / "coded.mp4",
        (coded for _ in range(count)),
        ffmpeg_bin,
        size=(PORTRAIT_H, PORTRAIT_W),
    )
    path = directory / "rotated.mp4"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-display_rotation", "-90", "-i", str(flat), "-c", "copy", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def faceless_video(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """Busy b-roll with nothing face-shaped in it."""
    path = tmp_path_factory.mktemp("faces") / "noface.mp4"
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={SOURCE_W}x{SOURCE_H}:rate=15:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
    return path


# --- Detection -------------------------------------------------------------


def test_moving_face_samples_travel_left_to_right(moving_face_video: Path, settings) -> None:
    samples = detect_faces(moving_face_video, 0.0, MOVING_SECONDS, settings)

    expected = MOVING_SECONDS * 2  # ~2 Hz
    assert len(samples) >= expected * 0.5, f"only {len(samples)} detections"

    assert samples[0].cx < 0.35, "should start on the left third"
    assert samples[-1].cx > 0.65, "should end on the right third"

    # The subject only ever moves right, so the samples must too (with a little
    # slack for Haar's box wobble between frames).
    for earlier, later in zip(samples, samples[1:]):
        assert later.cx > earlier.cx - 0.05, "cx went backwards"
    assert samples[-1].cx - samples[0].cx > 0.4, "barely moved"


def test_sample_fields_are_normalized_to_source_width(
    moving_face_video: Path, settings
) -> None:
    samples = detect_faces(moving_face_video, 0.0, MOVING_SECONDS, settings)

    assert all(0.0 <= s.cx <= 1.0 for s in samples)
    assert all(s.score == 1.0 for s in samples)  # Haar offers no confidence
    # The drawn face is 150/640 of the width; Haar's box is a bit tighter/looser.
    assert all(0.1 < s.size < 0.4 for s in samples)
    assert all(0.0 <= s.t <= MOVING_SECONDS for s in samples)
    assert [s.t for s in samples] == sorted(s.t for s in samples)


def test_sampling_is_about_two_hertz(moving_face_video: Path, settings) -> None:
    samples = detect_faces(moving_face_video, 0.0, MOVING_SECONDS, settings)
    stamps = sorted({s.t for s in samples})
    gaps = [round(b - a, 3) for a, b in zip(stamps, stamps[1:])]
    assert gaps, "expected several sampled frames"
    assert all(abs(g % 0.5) < 1e-6 for g in gaps), f"off-grid samples: {gaps}"
    assert min(gaps) == pytest.approx(0.5)


def test_window_is_honoured_and_times_are_clip_relative(
    moving_face_video: Path, settings
) -> None:
    """Only [start, end] is scanned, and `t` counts from `start`."""
    samples = detect_faces(moving_face_video, 4.0, 8.0, settings)

    assert samples, "expected detections in the second half"
    assert max(s.t for s in samples) <= 4.0 + 1e-6
    assert min(s.t for s in samples) >= 0.0
    # Four seconds in, the face has already crossed the middle.
    assert samples[0].cx > 0.45


def test_every_face_in_a_frame_is_reported(two_face_video: Path, settings) -> None:
    """Subject choice belongs to croptrack — detection hands over both faces."""
    samples = detect_faces(two_face_video, 0.0, 4.0, settings)
    assert samples

    by_time: dict[float, list] = {}
    for sample in samples:
        by_time.setdefault(sample.t, []).append(sample)
    assert any(len(group) == 2 for group in by_time.values()), "faces were collapsed"

    both = next(group for group in by_time.values() if len(group) == 2)
    left, right = sorted(both, key=lambda s: s.cx)
    assert left.cx < 0.4 < 0.6 < right.cx


def test_rotated_portrait_source_is_detected(
    rotated_portrait_video: Path, settings
) -> None:
    """A rotation tag must not cost us every detection in the clip.

    ffmpeg autorotates before the filtergraph, so the pipe has to be sized for
    the displayed 360x640 frame. Sizing it from the coded 640x360 force-scales
    9:16 into 16:9, stretches the face to twice its width, and Haar finds
    nothing at all — a silent zero, which is why this asserts on the hit count.
    """
    samples = detect_faces(rotated_portrait_video, 0.0, PORTRAIT_SECONDS, settings)

    assert len(samples) >= PORTRAIT_SECONDS, f"only {len(samples)} detections"
    # cx is a fraction of the DISPLAYED width (360), never the coded one (640).
    assert all(abs(s.cx - PORTRAIT_FACE_CX) < 0.06 for s in samples), [
        s.cx for s in samples
    ]
    assert all(0.2 < s.size < 0.45 for s in samples), [s.size for s in samples]


def test_rotation_is_respected_when_the_caller_supplies_the_probe(
    rotated_portrait_video: Path, settings
) -> None:
    """The threaded MediaInfo carries coded dimensions — rotation still applies."""
    coded = MediaInfo(
        duration=PORTRAIT_SECONDS, width=PORTRAIT_H, height=PORTRAIT_W, has_audio=False
    )
    samples = detect_faces(
        rotated_portrait_video, 0.0, PORTRAIT_SECONDS, settings, info=coded
    )

    assert samples
    assert all(abs(s.cx - PORTRAIT_FACE_CX) < 0.06 for s in samples)


def test_a_known_media_info_replaces_the_probe(
    moving_face_video: Path, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker probed the source once; a 5-clip job must not probe 5 more times."""
    from clipcatalyst_api.pipeline import probe as probe_module

    def explode(*args, **kwargs):
        raise AssertionError("detect_faces re-probed an already-probed source")

    monkeypatch.setattr(probe_module, "probe_media", explode)
    known = MediaInfo(
        duration=MOVING_SECONDS, width=SOURCE_W, height=SOURCE_H, has_audio=False
    )
    samples = detect_faces(
        moving_face_video, 0.0, MOVING_SECONDS, settings, info=known
    )

    assert samples
    assert samples[0].cx < 0.35 < 0.65 < samples[-1].cx


def test_progress_is_reported_monotonically(moving_face_video: Path, settings) -> None:
    progress: list[float] = []
    detect_faces(moving_face_video, 0.0, MOVING_SECONDS, settings, on_progress=progress.append)

    assert progress
    assert progress == sorted(progress)
    assert all(0.0 <= p <= 1.0 for p in progress)
    assert progress[-1] == pytest.approx(1.0)


# --- Degrading gracefully --------------------------------------------------


def test_video_without_faces_returns_no_samples(faceless_video: Path, settings) -> None:
    assert detect_faces(faceless_video, 0.0, 4.0, settings) == []


def test_face_tracking_off_skips_detection(moving_face_video: Path, settings) -> None:
    assert detect_faces(moving_face_video, 0.0, MOVING_SECONDS, replace(settings, face_tracking="off")) == []


@pytest.mark.parametrize("value", ["off", "0", "false", "no", ""])
def test_off_switch_accepts_the_usual_spellings(settings, value: str) -> None:
    assert face_tracking_enabled(replace(settings, face_tracking=value)) is False


@pytest.mark.parametrize("value", ["on", "1", "true", "ON"])
def test_on_is_the_default_and_the_obvious_spellings(settings, value: str) -> None:
    assert settings.face_tracking == "on"
    assert face_tracking_enabled(replace(settings, face_tracking=value)) is True


def test_env_var_turns_tracking_off(
    moving_face_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CC_FACE_TRACKING", "off")
    get_settings.cache_clear()
    try:
        assert detect_faces(moving_face_video, 0.0, MOVING_SECONDS, get_settings()) == []
    finally:
        get_settings.cache_clear()


def test_unreadable_source_returns_no_samples(settings, tmp_path: Path) -> None:
    """Detection problems are not render problems."""
    assert detect_faces(tmp_path / "missing.mp4", 0.0, 5.0, settings) == []

    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a video" * 500)
    assert detect_faces(garbage, 0.0, 5.0, settings) == []


def test_empty_window_returns_no_samples(moving_face_video: Path, settings) -> None:
    assert detect_faces(moving_face_video, 3.0, 3.0, settings) == []
    assert detect_faces(moving_face_video, 5.0, 2.0, settings) == []


def test_missing_ffmpeg_raises(moving_face_video: Path, settings) -> None:
    """The one thing worth raising for: we could not even run the decoder."""
    broken = replace(settings, ffmpeg_bin="/nonexistent/ffmpeg", ffprobe_bin="/nonexistent/ffprobe")
    with pytest.raises(PipelineError):
        detect_faces(moving_face_video, 0.0, 2.0, broken)


# --- Handing off to the shared motion math ---------------------------------


def test_detections_build_a_track_that_pans_right(moving_face_video: Path, settings) -> None:
    samples = detect_faces(moving_face_video, 0.0, MOVING_SECONDS, settings)
    track = build_crop_track(
        samples,
        CropTrackOptions(
            duration=MOVING_SECONDS, target_aspect=9 / 16, source_aspect=SOURCE_W / SOURCE_H
        ),
    )

    assert track.is_static is False, "a subject crossing the frame is not a static shot"
    assert track.coverage > 0.5
    assert len(track.keyframes) <= 48
    assert track.keyframes[-1].cx > track.keyframes[0].cx + 0.2


def test_no_detections_build_a_centered_static_track(faceless_video: Path, settings) -> None:
    samples = detect_faces(faceless_video, 0.0, 4.0, settings)
    track = build_crop_track(
        samples,
        CropTrackOptions(duration=4.0, target_aspect=9 / 16, source_aspect=SOURCE_W / SOURCE_H),
    )

    assert track.is_static is True
    assert track.coverage == 0.0
    assert track.keyframes == [track.keyframes[0]]
    assert track.keyframes[0].cx == pytest.approx(0.5)
