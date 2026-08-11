"""Shared pipeline dataclasses.

Field names deliberately mirror the browser pipeline's contract in
lib/studio/types.ts so the two engines stay conceptually identical.
All times are seconds relative to the start of the source video.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Word:
    text: str  # includes leading space where the ASR provides one
    start: float
    end: float
    speaker: int | None = None  # diarized speaker index (0 = most speech); None = unknown


@dataclass(frozen=True)
class Transcript:
    words: list[Word]
    text: str


@dataclass(frozen=True)
class Sentence:
    text: str
    start: float
    end: float
    words: list[Word]


@dataclass(frozen=True)
class SilenceSpan:
    start: float
    end: float


@dataclass(frozen=True)
class AudioFeatures:
    """Per-hop loudness features; rms normalized so p95 maps to 1."""

    rms: list[float]
    hop_seconds: float
    silences: list[SilenceSpan]


@dataclass(frozen=True)
class HighlightOptions:
    target_length: int  # 15 | 30 | 60
    count: int  # 1..3


@dataclass(frozen=True)
class ClipPlan:
    id: str
    start: float
    end: float
    score: int  # 0..100
    title: str
    hooks: list[str]  # up to 5, strongest first
    reason: str
    tip: str
    words: list[Word]  # words inside [start, end], re-based to clip start


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class RenderOptions:
    height: int = 1920  # output height; width derived for 9:16
    watermark: bool = True
    crf: int = 21
    preset: str = "veryfast"
    # Brand kit (BRANDKIT.md). Both are resolved from the owner's LIVE plan in
    # worker.py, never from the job row or the client. `logo_path` is a local
    # file overlaid bottom-right in place of our mark — our mark wins when
    # `watermark` is set, so the two are never drawn together. `caption_color`
    # is "#rrggbb" and replaces the default violet for unassigned words in
    # clips with fewer than two speakers; diarized colours are untouched.
    logo_path: str | None = None
    caption_color: str | None = None


@dataclass
class RenderedClip:
    plan: ClipPlan
    path: str  # local path of the rendered file
    width: int
    height: int


class PipelineError(RuntimeError):
    """User-presentable pipeline failure."""


class RenderError(PipelineError):
    pass


class TranscribeError(PipelineError):
    pass


@dataclass
class StageProgress:
    stage: str  # probe | transcribe | analyze | render
    progress: float  # 0..1, or -1 for indeterminate
    detail: str = ""
    clip_index: int | None = None
    clip_count: int | None = None


ProgressFn = "callable[[StageProgress], None]"  # documentation alias


def rebase_words(words: list[Word], start: float) -> list[Word]:
    """Words inside a window, re-based so captions start at 0."""
    return [
        Word(
            text=w.text,
            start=max(0.0, w.start - start),
            end=max(0.0, w.end - start),
            speaker=w.speaker,
        )
        for w in words
    ]
