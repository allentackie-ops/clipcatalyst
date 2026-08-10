"""Transcription backends + audio loudness features.

`get_transcriber(settings)` returns either the real faster-whisper backend
(imported lazily so the API container never needs torch/ctranslate2) or the
JSON-file `FakeTranscriber` used by tests and the sandbox.

`extract_audio_features` decodes mono 16 kHz f32 PCM through ffmpeg and ports
the browser's `computeAudioFeatures` (lib/studio/audio.ts) exactly: RMS per
0.05 s hop normalized so the 95th percentile maps to 1, silence spans where
normalized RMS stays below 0.08 for >= 0.35 s, nearby spans merged across
gaps of <= 0.1 s.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Callable, Protocol

from ..settings import Settings
from .types import AudioFeatures, SilenceSpan, TranscribeError, Transcript, Word

TARGET_SAMPLE_RATE = 16000
HOP_SECONDS = 0.05
SILENCE_THRESHOLD = 0.08
SILENCE_MIN_SECONDS = 0.35
SILENCE_MERGE_GAP_SECONDS = 0.1

ProgressFn = Callable[[float], None]


class Transcriber(Protocol):
    def transcribe(
        self, path: str | Path, on_progress: ProgressFn | None = None
    ) -> Transcript: ...


def get_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == "fake":
        return FakeTranscriber(settings.fake_transcript_path)
    if settings.transcriber == "faster-whisper":
        return FasterWhisperTranscriber(settings)
    raise TranscribeError(
        f"Unknown transcriber {settings.transcriber!r} — "
        "set CC_TRANSCRIBER to 'faster-whisper' or 'fake'."
    )


class FasterWhisperTranscriber:
    """Word-timestamped transcription via faster-whisper (lazy import)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None

    def _load_model(self):  # noqa: ANN202 - WhisperModel imported lazily
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy: lazy on purpose

            s = self._settings
            self._model = WhisperModel(
                s.whisper_model, device=s.whisper_device, compute_type=s.whisper_compute
            )
        return self._model

    def transcribe(
        self, path: str | Path, on_progress: ProgressFn | None = None
    ) -> Transcript:
        model = self._load_model()
        words: list[Word] = []
        # faster-whisper returns a lazy generator, so ctranslate2/PyAV decode
        # errors surface while iterating `segments`, not from transcribe().
        # Keep the whole consume inside the try so decode-time failures still
        # raise the friendly TranscribeError instead of a raw crash.
        try:
            segments, info = model.transcribe(
                str(path), word_timestamps=True, vad_filter=True
            )
            total = float(getattr(info, "duration", 0.0) or 0.0)
            for segment in segments:  # generator: decode + progress per segment
                for w in segment.words or []:
                    # faster-whisper word text carries its leading space — keep it.
                    words.append(
                        Word(text=w.word, start=float(w.start), end=float(w.end))
                    )
                if on_progress is not None and total > 0:
                    on_progress(min(1.0, float(segment.end) / total))
        except Exception as exc:  # ctranslate2/PyAV raise plain RuntimeErrors
            raise TranscribeError(
                "Transcription failed — the file's audio could not be decoded."
            ) from exc

        if on_progress is not None:
            on_progress(1.0)
        text = "".join(w.text for w in words).strip()
        return Transcript(words=words, text=text)


class FakeTranscriber:
    """Reads `{words: [{text, start, end}], text}` JSON from a fixture path."""

    def __init__(self, transcript_path: str) -> None:
        self._path = transcript_path

    def transcribe(
        self, path: str | Path, on_progress: ProgressFn | None = None
    ) -> Transcript:
        if not self._path or not Path(self._path).is_file():
            raise TranscribeError(
                "Fake transcriber selected but CC_FAKE_TRANSCRIPT_PATH does not "
                "point at a transcript JSON file."
            )
        try:
            data = json.loads(Path(self._path).read_text(encoding="utf-8"))
            words = [
                Word(text=str(w["text"]), start=float(w["start"]), end=float(w["end"]))
                for w in data["words"]
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TranscribeError(
                f"Fake transcript at {self._path} is not valid transcript JSON."
            ) from exc
        text = str(data.get("text") or "".join(w.text for w in words).strip())
        if on_progress is not None:
            on_progress(1.0)
        return Transcript(words=words, text=text)


BYTES_PER_SAMPLE = 4  # f32le
# Hops decoded per streamed read. ~256 hops ≈ 12.8 s of 16 kHz mono ≈ 0.8 MB,
# so the whole PCM buffer is never resident — only one block at a time.
_HOPS_PER_BLOCK = 256


def extract_audio_features(path: str | Path, settings: Settings) -> AudioFeatures:
    """Decode mono 16 kHz PCM via ffmpeg and compute loudness features.

    The PCM is streamed from ffmpeg's stdout in hop-aligned blocks and folded
    into per-hop sum-of-squares on the fly, so the full decoded signal
    (~230 MB/hour) is never buffered in memory. Output is identical to feeding
    the whole buffer through :func:`compute_audio_features`.
    """
    cmd = [
        settings.ffmpeg_bin,
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
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # matches old behavior: stderr was unused
        )
    except OSError as exc:
        raise TranscribeError(
            "ffmpeg is not available, so the audio could not be analyzed. "
            "Check CC_FFMPEG_BIN."
        ) from exc

    hop_size = max(1, round(TARGET_SAMPLE_RATE * HOP_SECONDS))
    block_bytes = hop_size * BYTES_PER_SAMPLE * _HOPS_PER_BLOCK

    raw_rms: list[float] = []
    total_samples = 0
    hop_sum_squares = 0.0  # running sum-of-squares for the in-progress hop
    hop_fill = 0  # samples accumulated into the in-progress hop
    leftover = b""  # bytes that don't yet form a whole float32

    stdout = proc.stdout
    assert stdout is not None
    try:
        while True:
            chunk = stdout.read(block_bytes)
            if not chunk:
                break
            if leftover:
                chunk = leftover + chunk
                leftover = b""
            whole = len(chunk) - (len(chunk) % BYTES_PER_SAMPLE)
            if whole != len(chunk):
                leftover = chunk[whole:]
                chunk = chunk[:whole]
            if not chunk:
                continue
            samples = array("f")
            samples.frombytes(chunk)
            if sys.byteorder == "big":  # f32le on the wire, native in memory
                samples.byteswap()
            for s in samples:
                hop_sum_squares += s * s
                hop_fill += 1
                total_samples += 1
                if hop_fill == hop_size:
                    raw_rms.append(math.sqrt(hop_sum_squares / hop_size))
                    hop_sum_squares = 0.0
                    hop_fill = 0
    except Exception:
        proc.kill()
        proc.wait()
        raise
    finally:
        stdout.close()

    # Final partial hop (identical to the buffered code's `to - from` tail).
    if hop_fill > 0:
        raw_rms.append(math.sqrt(hop_sum_squares / hop_fill))

    if proc.wait() != 0:
        raise TranscribeError(
            "Couldn't read this file's audio. The video may use an unsupported "
            "codec or contain no audio track — try re-exporting it as a "
            "standard MP4 (H.264 video + AAC audio)."
        )

    return _finalize_features(raw_rms, total_samples, TARGET_SAMPLE_RATE)


def compute_audio_features(pcm: "array[float]", sample_rate: int) -> AudioFeatures:
    """Straight port of computeAudioFeatures from lib/studio/audio.ts.

    Buffered reference path: computes raw per-hop RMS over the whole signal,
    then defers normalization + silence detection to :func:`_finalize_features`
    (the same code the streaming :func:`extract_audio_features` uses).
    """
    hop_size = max(1, round(sample_rate * HOP_SECONDS))
    hop_count = math.ceil(len(pcm) / hop_size)
    raw_rms: list[float] = [0.0] * hop_count

    for i in range(hop_count):
        start = i * hop_size
        stop = min(start + hop_size, len(pcm))
        sum_squares = 0.0
        for j in range(start, stop):
            s = pcm[j]
            sum_squares += s * s
        raw_rms[i] = math.sqrt(sum_squares / max(1, stop - start))

    return _finalize_features(raw_rms, len(pcm), sample_rate)


def _finalize_features(
    raw_rms: list[float], total_samples: int, sample_rate: int
) -> AudioFeatures:
    """Normalize per-hop RMS (p95 → 1) and derive silence spans.

    Shared tail of both the buffered and streaming feature extractors so their
    output is guaranteed bit-identical. `raw_rms` is one un-normalized RMS per
    0.05 s hop; `total_samples` is the decoded PCM length.
    """
    hop_count = len(raw_rms)
    rms = list(raw_rms)

    # Normalize so the 95th percentile maps to 1; fall back to max, then 1.
    if hop_count > 0:
        ordered = sorted(rms)
        p95 = ordered[math.floor(0.95 * (len(ordered) - 1))]
        peak = ordered[-1]
        scale = p95 if p95 > 0 else (peak if peak > 0 else 1.0)
        rms = [min(1.0, max(0.0, v / scale)) for v in rms]

    total_seconds = total_samples / sample_rate

    # Runs of consecutive below-threshold hops.
    raw_spans: list[list[float]] = []
    run_start = -1
    for i in range(hop_count + 1):
        quiet = i < hop_count and rms[i] < SILENCE_THRESHOLD
        if quiet and run_start == -1:
            run_start = i
        elif not quiet and run_start != -1:
            raw_spans.append(
                [run_start * HOP_SECONDS, min(i * HOP_SECONDS, total_seconds)]
            )
            run_start = -1

    # Merge spans separated by <= 0.1 s gaps, keep spans sustained >= 0.35 s.
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
