"""Transcribe/audio-feature tests.

Covers the two hardening changes in ``pipeline/transcribe.py``:

1. ``FasterWhisperTranscriber`` wraps the *lazy* segment generator, so
   ctranslate2/PyAV decode errors that only surface while iterating raise the
   friendly ``TranscribeError`` instead of crashing.
2. ``extract_audio_features`` streams PCM from ffmpeg in hop-aligned blocks
   instead of buffering the whole decoded signal, and must produce output
   identical to the old fully-buffered approach.
"""

from __future__ import annotations

import math
import subprocess
from array import array
from dataclasses import replace
from pathlib import Path

import pytest

from clipcatalyst_api.pipeline.transcribe import (
    HOP_SECONDS,
    SILENCE_MERGE_GAP_SECONDS,
    SILENCE_MIN_SECONDS,
    SILENCE_THRESHOLD,
    TARGET_SAMPLE_RATE,
    FasterWhisperTranscriber,
    extract_audio_features,
)
from clipcatalyst_api.pipeline.types import (
    AudioFeatures,
    SilenceSpan,
    TranscribeError,
    Transcript,
)
from clipcatalyst_api.settings import get_settings


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def ffmpeg_bin() -> str:
    get_settings.cache_clear()
    binary = get_settings().ffmpeg_bin
    get_settings.cache_clear()
    return binary


@pytest.fixture(scope="module")
def sine_with_gaps(ffmpeg_bin: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 5 s 440 Hz sine muted over two windows, so silence spans exist.

    Gaps at t=[1.0, 1.6] (0.6 s) and t=[3.0, 3.9] (0.9 s) — both comfortably
    above the 0.35 s silence-sustain threshold.
    """
    path = tmp_path_factory.mktemp("audio") / "sine_gaps.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-af",
            "volume=enable='between(t,1,1.6)+between(t,3,3.9)':volume=0",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


def _decode_full_pcm(ffmpeg_bin: str, path: Path) -> "array[float]":
    """The OLD buffered decode: read the entire f32le stream into memory."""
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-ac",
            "1",
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    pcm = array("f")
    usable = len(proc.stdout) - (len(proc.stdout) % 4)
    pcm.frombytes(proc.stdout[:usable])
    return pcm


def _reference_features(pcm: "array[float]", sample_rate: int) -> AudioFeatures:
    """Independent from-scratch port of lib/studio/audio.ts computeAudioFeatures.

    Deliberately does NOT call the production module, so it is a genuine oracle
    for the streaming extractor rather than a comparison of shared code.
    """
    hop_size = max(1, round(sample_rate * HOP_SECONDS))
    hop_count = math.ceil(len(pcm) / hop_size)
    rms: list[float] = [0.0] * hop_count
    for i in range(hop_count):
        start = i * hop_size
        stop = min(start + hop_size, len(pcm))
        ss = 0.0
        for j in range(start, stop):
            s = pcm[j]
            ss += s * s
        rms[i] = math.sqrt(ss / max(1, stop - start))

    if hop_count > 0:
        ordered = sorted(rms)
        p95 = ordered[math.floor(0.95 * (len(ordered) - 1))]
        peak = ordered[-1]
        scale = p95 if p95 > 0 else (peak if peak > 0 else 1.0)
        rms = [min(1.0, max(0.0, v / scale)) for v in rms]

    total_seconds = len(pcm) / sample_rate
    raw_spans: list[list[float]] = []
    run_start = -1
    for i in range(hop_count + 1):
        quiet = i < hop_count and rms[i] < SILENCE_THRESHOLD
        if quiet and run_start == -1:
            run_start = i
        elif not quiet and run_start != -1:
            raw_spans.append([run_start * HOP_SECONDS, min(i * HOP_SECONDS, total_seconds)])
            run_start = -1

    merged: list[list[float]] = []
    for span in raw_spans:
        if merged and span[0] - merged[-1][1] <= SILENCE_MERGE_GAP_SECONDS:
            merged[-1][1] = span[1]
        else:
            merged.append(list(span))
    silences = [
        SilenceSpan(start=s, end=e) for s, e in merged if e - s >= SILENCE_MIN_SECONDS
    ]
    return AudioFeatures(rms=rms, hop_seconds=HOP_SECONDS, silences=silences)


# --------------------------------------------------------------------------- #
# extract_audio_features: streaming output == old buffered output              #
# --------------------------------------------------------------------------- #


def test_streaming_features_match_buffered(sine_with_gaps: Path, ffmpeg_bin: str, settings) -> None:
    streamed = extract_audio_features(sine_with_gaps, settings)
    reference = _reference_features(
        _decode_full_pcm(ffmpeg_bin, sine_with_gaps), TARGET_SAMPLE_RATE
    )

    # Same number of hops, identical hop cadence.
    assert streamed.hop_seconds == reference.hop_seconds == HOP_SECONDS
    assert len(streamed.rms) == len(reference.rms)

    # Bit-identical RMS: the streaming accumulation preserves per-sample order,
    # so the floating-point result must match the buffered path exactly.
    assert streamed.rms == reference.rms

    # A few concrete RMS spot-checks against the hop layout (0.05 s hops):
    #   hop 10 = 0.5 s → loud sine        hop 24 = 1.2 s → inside gap 1 (muted)
    #   hop 40 = 2.0 s → between gaps      hop 70 = 3.5 s → inside gap 2 (muted)
    assert streamed.rms[10] == pytest.approx(1.0, abs=0.2)  # loud, normalized ~1
    assert streamed.rms[40] > SILENCE_THRESHOLD  # loud, between the two gaps
    assert streamed.rms[24] < SILENCE_THRESHOLD  # muted (gap 1)
    assert streamed.rms[70] < SILENCE_THRESHOLD  # muted (gap 2)

    # Identical silence spans, and the two engineered gaps are present.
    assert streamed.silences == reference.silences
    assert len(streamed.silences) == 2
    first, second = streamed.silences
    assert first.start == pytest.approx(1.0, abs=0.2)
    assert first.end == pytest.approx(1.6, abs=0.2)
    assert second.start == pytest.approx(3.0, abs=0.2)
    assert second.end == pytest.approx(3.9, abs=0.2)


def test_streaming_features_full_signal_no_leftover(sine_with_gaps: Path, ffmpeg_bin: str, settings) -> None:
    """The last (partial) hop is accounted for: total_seconds ≈ file length."""
    streamed = extract_audio_features(sine_with_gaps, settings)
    # 5 s at 0.05 s hops → ~100 hops (resampler flush may add a couple).
    assert 98 <= len(streamed.rms) <= 103


def test_extract_features_bad_audio_raises_transcribe_error(settings, tmp_path: Path) -> None:
    garbage = tmp_path / "not-audio.bin"
    garbage.write_bytes(b"definitely not a media file" * 200)
    with pytest.raises(TranscribeError):
        extract_audio_features(garbage, settings)


def test_extract_features_missing_ffmpeg_raises(settings, tmp_path: Path) -> None:
    bad = tmp_path / "audio.wav"
    bad.write_bytes(b"\x00")
    # Settings is a frozen dataclass — copy it with a bogus ffmpeg path so the
    # Popen raises OSError, which must surface as a friendly TranscribeError.
    broken = replace(settings, ffmpeg_bin=str(tmp_path / "no-such-ffmpeg"))
    with pytest.raises(TranscribeError):
        extract_audio_features(bad, broken)


# --------------------------------------------------------------------------- #
# FasterWhisperTranscriber: lazy-generator decode errors → TranscribeError     #
# --------------------------------------------------------------------------- #


class _FakeWord:
    def __init__(self, word: str, start: float, end: float) -> None:
        self.word = word
        self.start = start
        self.end = end


class _FakeSegment:
    def __init__(self, words: list[_FakeWord], end: float) -> None:
        self.words = words
        self.end = end


class _FakeInfo:
    duration = 10.0


class _RaisingModel:
    """model.transcribe() succeeds but the returned generator raises on iterate.

    Mirrors faster-whisper: transcribe() is cheap and returns a lazy generator;
    ctranslate2/PyAV decode failures only surface once you consume it.
    """

    def transcribe(self, path: str, **kwargs: object):
        def gen():
            yield _FakeSegment([_FakeWord(" hello", 0.0, 0.4)], end=0.4)
            raise RuntimeError("ctranslate2: could not decode audio frame")

        return gen(), _FakeInfo()


class _HappyModel:
    def transcribe(self, path: str, **kwargs: object):
        def gen():
            yield _FakeSegment(
                [_FakeWord(" hello", 0.0, 0.4), _FakeWord(" world", 0.5, 0.9)], end=0.9
            )
            yield _FakeSegment([_FakeWord(" again", 1.0, 1.4)], end=1.4)

        return gen(), _FakeInfo()


def _transcriber_with_model(settings, model) -> FasterWhisperTranscriber:
    t = FasterWhisperTranscriber(settings)
    t._load_model = lambda: model  # type: ignore[method-assign]
    return t


def test_decode_error_during_iteration_raises_transcribe_error(settings) -> None:
    t = _transcriber_with_model(settings, _RaisingModel())
    with pytest.raises(TranscribeError) as excinfo:
        t.transcribe("whatever.mp4")
    assert "decoded" in str(excinfo.value)
    # The underlying ctranslate2 error is chained, not swallowed.
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_happy_iteration_still_produces_words(settings) -> None:
    progress: list[float] = []
    t = _transcriber_with_model(settings, _HappyModel())
    result = t.transcribe("whatever.mp4", progress.append)
    assert isinstance(result, Transcript)
    assert result.text == "hello world again"
    assert [w.text for w in result.words] == [" hello", " world", " again"]
    assert progress, "expected progress callbacks"
    assert progress[-1] == pytest.approx(1.0)
    assert progress == sorted(progress)  # monotonic non-decreasing
