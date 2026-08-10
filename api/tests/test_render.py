"""Render pipeline tests: real ffmpeg render of a generated source video."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipcatalyst_api.pipeline.probe import probe_media
from clipcatalyst_api.pipeline.render import escape_subtitles_path, render_clip
from clipcatalyst_api.pipeline.types import (
    ClipPlan,
    PipelineError,
    RenderError,
    RenderOptions,
    Word,
)
from clipcatalyst_api.settings import get_settings

SOURCE_SECONDS = 20


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 20 s 640x360 test pattern with a sine audio track (x264 + aac)."""
    get_settings.cache_clear()
    ffmpeg = get_settings().ffmpeg_bin
    get_settings.cache_clear()
    path = tmp_path_factory.mktemp("source") / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=30:duration={SOURCE_SECONDS}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={SOURCE_SECONDS}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def make_plan(start: float = 2.0, end: float = 10.0) -> ClipPlan:
    words = [  # re-based to clip start, spanning ~0.2..8 s of the clip
        Word("Hello", 0.2, 0.6),
        Word(" world", 0.7, 1.2),
        Word(" this", 1.3, 1.7),
        Word(" is", 1.8, 2.0),
        Word(" a", 2.1, 2.2),
        Word(" rendered", 2.3, 3.0),
        Word(" karaoke", 3.1, 3.9),
        Word(" caption", 4.0, 4.8),
        Word(" test", 5.2, 5.8),
        Word(" clip", 6.5, 7.9),
    ]
    return ClipPlan(
        id="clip-1",
        start=start,
        end=end,
        score=90,
        title="Render test",
        hooks=["hook"],
        reason="",
        tip="",
        words=words,
    )


def test_probe_source(source_video: Path, settings) -> None:
    info = probe_media(source_video, settings)
    assert info.width == 640
    assert info.height == 360
    assert info.has_audio is True
    assert info.duration == pytest.approx(SOURCE_SECONDS, abs=0.5)


def test_render_portrait_clip_with_captions(source_video: Path, settings, tmp_path: Path) -> None:
    plan = make_plan(start=2.0, end=10.0)
    out = tmp_path / "clip.mp4"
    progress: list[float] = []

    rendered = render_clip(
        source_video,
        plan,
        out,
        RenderOptions(height=960, watermark=True),
        settings,
        on_progress=progress.append,
    )

    assert rendered.path == str(out)
    assert (rendered.width, rendered.height) == (540, 960)
    assert out.stat().st_size > 20_000  # a real encoded video, not a stub

    info = probe_media(out, settings)
    assert (info.width, info.height) == (540, 960)  # portrait 9:16
    assert info.duration == pytest.approx(plan.end - plan.start, abs=0.5)
    assert info.has_audio is True

    assert progress, "expected on_progress callbacks"
    assert progress == sorted(progress)  # monotonic
    assert progress[-1] == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in progress)

    # No leftover .ass temp files.
    assert not list(settings.tmp_dir.glob("*.ass"))


def test_render_without_watermark(source_video: Path, settings, tmp_path: Path) -> None:
    out = tmp_path / "clean.mp4"
    render_clip(
        source_video,
        make_plan(start=1.0, end=4.0),
        out,
        RenderOptions(height=480, watermark=False),
        settings,
    )
    info = probe_media(out, settings)
    assert (info.width, info.height) == (270, 480)
    assert info.duration == pytest.approx(3.0, abs=0.5)


def test_render_failure_raises_with_stderr_tail(settings, tmp_path: Path) -> None:
    with pytest.raises(RenderError) as excinfo:
        render_clip(
            tmp_path / "does-not-exist.mp4",
            make_plan(),
            tmp_path / "out.mp4",
            RenderOptions(height=960),
            settings,
        )
    message = str(excinfo.value)
    assert "ffmpeg exit" in message
    assert "does-not-exist.mp4" in message  # stderr tail included
    assert not list(settings.tmp_dir.glob("*.ass"))  # temp cleaned on failure


def test_probe_unreadable_file(settings, tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"this is not a video at all" * 100)
    with pytest.raises(PipelineError):
        probe_media(garbage, settings)
    with pytest.raises(PipelineError):
        probe_media(tmp_path / "missing.mp4", settings)


def test_subtitles_path_escaping() -> None:
    assert escape_subtitles_path("/tmp/plain.ass") == "/tmp/plain.ass"
    # Colon: option-level escape, then the backslash itself is graph-escaped.
    assert escape_subtitles_path("/tmp/a:b.ass") == "/tmp/a\\\\:b.ass"
    assert escape_subtitles_path("/tmp/a'b.ass") == "/tmp/a\\\\\\'b.ass"
    assert escape_subtitles_path("/tmp/a,b.ass") == "/tmp/a\\,b.ass"
