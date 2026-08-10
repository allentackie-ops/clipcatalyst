"""Voice embeddings for the cloud diarizer (the `diarize` stage's ears).

`segment_embeddings` turns each speech segment (from
`diarize.build_speech_segments`) into a small voice fingerprint the shared
judgement core (`diarize.assign_speakers`) can cluster. The recipe is the one
SPEAKERS.md fixes for both engines:

  1. The segment's mono 16 kHz PCM, capped at the segment's first 4 s.
  2. Frames of 400 samples (25 ms), hop 320 (20 ms), Hamming window.
  3. Magnitude spectrum via 512-point FFT.
  4. 24 triangular mel filters spanning 80-7600 Hz; log energies (floor 1e-10).
  5. DCT-II, keep coefficients c1..c12 (drop c0 -- energy is mic distance,
     not identity).
  6. Skip near-silent frames (frame RMS < 0.008); fewer than 5 voiced frames
     -> EMPTY embedding (the shared core lets that segment inherit a
     neighbour's speaker).
  7. Embedding = [mean(c1..c12), std(c1..c12)] -> 24 dims, L2-normalized.

Exact numeric parity with the browser's JS FFT is NOT required (and float
error makes it impossible); each engine validates its own embeddings against
synthetic voices. Only the clustering layer is bit-exact across engines.

PCM comes from ONE streaming ffmpeg pass (f32le mono 16 kHz, the same decode
`transcribe.extract_audio_features` uses): blocks are read off the pipe and
sliced into the per-segment windows on the fly, so the full decoded signal
(~230 MB/hour) is never resident -- at most 4 s * 4 B * 16 kHz = 256 KB per
segment. The read stops as soon as the last segment's window is filled.

Everything here is best-effort. Diarization being off (`CC_DIARIZATION`),
numpy missing, silent or too-short segments, PCM running out early -- all of
that degrades to empty embeddings, which the pipeline renders as today's
single-voice violet captions. `TranscribeError` is raised only when ffmpeg
itself fails (cannot launch, or exits in error before the needed PCM
arrived); a quality problem must never fail a clip.
"""

from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from ..settings import Settings
from .types import TranscribeError

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
#: Only the first this-many seconds of a segment feed its embedding.
SEGMENT_CAP_S = 4.0
#: 25 ms analysis frames ...
FRAME_SIZE = 400
#: ... every 20 ms.
HOP_SIZE = 320
FFT_SIZE = 512
MEL_BANDS = 24
MEL_LOW_HZ = 80.0
MEL_HIGH_HZ = 7600.0
LOG_FLOOR = 1e-10
#: Frames quieter than this RMS carry no voice worth fingerprinting.
VOICED_RMS = 0.008
#: Fewer voiced frames than this -> the segment is unusable, embed as [].
MIN_VOICED_FRAMES = 5
#: DCT-II coefficients kept: c1..c12 (c0 dropped -- loudness, not identity).
FIRST_COEFF, LAST_COEFF = 1, 12

_BYTES_PER_SAMPLE = 4  # f32le
#: Samples decoded per pipe read (~1 s, 64 KB) -- streaming, never the file.
_BLOCK_SAMPLES = SAMPLE_RATE

_OFF_VALUES = {"", "0", "off", "false", "no", "none", "disabled"}

_NO_FFMPEG = (
    "ffmpeg is not available, so the audio could not be scanned for speakers. "
    "Check CC_FFMPEG_BIN."
)
_DECODE_FAILED = (
    "ffmpeg could not decode this file's audio while scanning for speakers."
)


def diarization_enabled(settings: Settings) -> bool:
    """Whether `CC_DIARIZATION` allows speaker detection (default: yes)."""
    value = getattr(settings, "diarization", "on")
    return str(value).strip().lower() not in _OFF_VALUES


def segment_embeddings(
    path: str | Path, segments: Sequence, settings: Settings
) -> list[list[float]]:
    """One 24-dim voice embedding per speech segment; `[]` where unusable.

    `segments` is anything with `start`/`end` seconds attributes --
    `diarize.SpeechSegment` in production. Returns exactly
    `len(segments)` entries so `assign_speakers` can zip them. Unusable
    segments (silent, too short, off diarization, past end of audio) come back
    as `[]`, never as an exception; `TranscribeError` is raised only when
    ffmpeg itself fails.
    """
    if len(segments) == 0:
        return []
    empties: list[list[float]] = [[] for _ in segments]
    if not diarization_enabled(settings):
        return empties
    np = _numpy()
    if np is None:
        return empties

    # Sample windows to collect: [start, min(end, start + 4 s)) per segment.
    ranges: list[tuple[int, int] | None] = []
    for seg in segments:
        try:
            start = max(0.0, float(seg.start))
            end = min(float(seg.end), start + SEGMENT_CAP_S)
        except (AttributeError, TypeError, ValueError):
            ranges.append(None)
            continue
        lo = int(round(start * SAMPLE_RATE))
        hi = int(round(end * SAMPLE_RATE))
        ranges.append((lo, hi) if hi > lo else None)
    if all(r is None for r in ranges):
        return empties

    buffers = _collect_pcm(path, ranges, settings, np)
    return [
        _embed(pcm, np) if pcm is not None else [] for pcm in buffers
    ]


# --- PCM streaming ----------------------------------------------------------


def _collect_pcm(path, ranges, settings: Settings, np):
    """Decode once, streaming, and slice out every segment's sample window.

    Returns one float32 array (possibly short or empty) per range; None where
    the range itself was None. Stops reading as soon as the furthest window is
    filled. Raises `TranscribeError` only when ffmpeg cannot run or dies
    before delivering the PCM it was still expected to produce.
    """
    last_needed = max(hi for r in ranges if r is not None for hi in (r[1],))
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "a:0",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:  # the one genuine "cannot even try" case
        raise TranscribeError(_NO_FFMPEG) from exc

    parts: list[list | None] = [[] if r is not None else None for r in ranges]
    offset = 0  # absolute sample index of the next byte on the pipe
    leftover = b""  # bytes that don't yet form a whole float32
    stopped_early = False

    stdout = proc.stdout
    assert stdout is not None
    try:
        while True:
            chunk = stdout.read(_BLOCK_SAMPLES * _BYTES_PER_SAMPLE)
            if not chunk:
                break
            if leftover:
                chunk = leftover + chunk
                leftover = b""
            whole = len(chunk) - (len(chunk) % _BYTES_PER_SAMPLE)
            leftover = chunk[whole:]
            if whole == 0:
                continue
            samples = np.frombuffer(chunk[:whole], dtype="<f4")
            chunk_end = offset + len(samples)
            for i, r in enumerate(ranges):
                if r is None:
                    continue
                lo, hi = r
                if hi <= offset or lo >= chunk_end:
                    continue
                parts[i].append(samples[max(lo, offset) - offset : min(hi, chunk_end) - offset])
            offset = chunk_end
            if offset >= last_needed:
                stopped_early = True
                break
    finally:
        _shutdown(proc)

    if not stopped_early and proc.returncode not in (0, None):
        # ffmpeg died before the PCM we still needed arrived: no audio track,
        # unreadable file, broken decoder. That is ffmpeg failing, not a
        # quality judgement -- report it (callers treat diarization as
        # best-effort anyway).
        raise TranscribeError(_DECODE_FAILED)

    collected = []
    for p in parts:
        if p is None:
            collected.append(None)
        elif p:
            collected.append(np.concatenate(p))
        else:
            collected.append(np.empty(0, dtype=np.float32))
    return collected


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


# --- The embedding math -----------------------------------------------------


def _numpy():
    """numpy, or None (it ships with opencv-python-headless, but degrade)."""
    try:
        import numpy as np  # noqa: PLC0415 -- optional dep, import not cheap

        return np
    except Exception:
        log.info("numpy is not installed; speaker diarization disabled")
        return None


@lru_cache(maxsize=1)
def _analysis_tables():
    """(hamming, mel filterbank, DCT-II rows for c1..c12), computed once."""
    import numpy as np  # noqa: PLC0415 -- only called after _numpy() succeeded

    window = np.hamming(FRAME_SIZE)

    def hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    mel_points = np.linspace(hz_to_mel(MEL_LOW_HZ), hz_to_mel(MEL_HIGH_HZ), MEL_BANDS + 2)
    hz_points = 700.0 * (10.0 ** (mel_points / 2595.0) - 1.0)
    freqs = np.fft.rfftfreq(FFT_SIZE, d=1.0 / SAMPLE_RATE)
    filterbank = np.zeros((MEL_BANDS, freqs.size))
    for m in range(MEL_BANDS):
        left, center, right = hz_points[m], hz_points[m + 1], hz_points[m + 2]
        rising = (freqs - left) / (center - left)
        falling = (right - freqs) / (right - center)
        filterbank[m] = np.maximum(0.0, np.minimum(rising, falling))

    # DCT-II rows k = 1..12 over the 24 log-mel energies (c0 dropped).
    n = np.arange(MEL_BANDS)
    k = np.arange(FIRST_COEFF, LAST_COEFF + 1)
    dct_rows = np.cos(np.pi / MEL_BANDS * (n[None, :] + 0.5) * k[:, None])

    return window, filterbank, dct_rows


def _embed(pcm, np) -> list[float]:
    """The 24-dim MFCC-stats fingerprint of one segment's PCM, or []."""
    try:
        if pcm is None or len(pcm) < FRAME_SIZE:
            return []
        frame_count = 1 + (len(pcm) - FRAME_SIZE) // HOP_SIZE
        idx = np.arange(FRAME_SIZE)[None, :] + HOP_SIZE * np.arange(frame_count)[:, None]
        frames = np.asarray(pcm, dtype=np.float64)[idx]

        # The voiced-frame gate runs on the raw (unwindowed) frame RMS.
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        frames = frames[rms >= VOICED_RMS]
        if frames.shape[0] < MIN_VOICED_FRAMES:
            return []

        window, filterbank, dct_rows = _analysis_tables()
        spectra = np.abs(np.fft.rfft(frames * window, n=FFT_SIZE, axis=1))
        mel = spectra @ filterbank.T
        logmel = np.log(np.maximum(mel, LOG_FLOOR))
        cepstra = logmel @ dct_rows.T  # (frames, 12): c1..c12 per frame

        embedding = np.concatenate([cepstra.mean(axis=0), cepstra.std(axis=0)])
        norm = float(np.linalg.norm(embedding))
        if not np.isfinite(embedding).all() or norm == 0.0:
            return []
        return (embedding / norm).tolist()
    except Exception:  # any numeric surprise: this segment just goes unlabelled
        log.warning("speaker embedding failed for one segment", exc_info=True)
        return []
