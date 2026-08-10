"""Speaker-embedding tests against real, synthesized audio.

Rather than mock the DSP, these tests build the SPEAKERS.md synthetic voices
with ffmpeg — voice A a sawtooth ~110 Hz through a ~900 Hz lowpass, voice B a
square ~280 Hz through a ~400 Hz highpass — alternate them in 4 s turns with
1 s silences, and run the whole decode → frame → mel → DCT → stats path. The
embeddings must cluster A-turns with A-turns (cosine), and pushed through the
shared judgement core (`pipeline.diarize`, the port of diarize.ts) the
two-voice fixture must come out as exactly two speakers with alternating
turns, while a single-voice fixture must stay one speaker.

Degradation matters as much as detection: silent or off-the-end segments must
embed as `[]`, CC_DIARIZATION=off must short-circuit before ffmpeg is even
consulted, and only ffmpeg itself failing may raise — a bad diarization day
must never fail a clip.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from clipcatalyst_api.pipeline.diarize import (
    SpeechSegment,
    assign_speakers,
    build_speech_segments,
)
from clipcatalyst_api.pipeline.speaker_embed import (
    diarization_enabled,
    segment_embeddings,
)
from clipcatalyst_api.pipeline.types import TranscribeError, Word
from clipcatalyst_api.settings import get_settings

SAMPLE_RATE = 16000
TURN_S = 4.0
GAP_S = 1.0
SPAN_S = TURN_S + GAP_S  # one turn plus the silence after it

#: The SPEAKERS.md synthetic voices: distinct enough in spectrum that MFCC
#: statistics must separate them, or the recipe is wrong.
VOICE_A_SRC = "aevalsrc='0.5*(2*(110*t-floor(0.5+110*t)))'"  # sawtooth ~110 Hz
VOICE_A_FILTER = "lowpass=f=900"
VOICE_B_SRC = "aevalsrc='0.4*(2*gt(sin(2*PI*280*t),0)-1)'"  # square ~280 Hz
VOICE_B_FILTER = "highpass=f=400"


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


def synth(ffmpeg: str, out: Path, source: str, audio_filter: str, seconds: float) -> Path:
    """One voice (or silence) as a 16 kHz mono wav."""
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"{source}:s={SAMPLE_RATE}:d={seconds:g}",
            "-af", audio_filter, "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", str(out),
        ],
        check=True,
    )
    return out


def concat(ffmpeg: str, out: Path, pieces: list[Path]) -> Path:
    """Splice same-format wavs end to end (turns and the silences between)."""
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p}'\n" for p in pieces))
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out),
        ],
        check=True,
    )
    return out


@pytest.fixture(scope="module")
def pieces(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("voices")
    return {
        "A": synth(ffmpeg_bin, d / "voiceA.wav", VOICE_A_SRC, VOICE_A_FILTER, TURN_S),
        "B": synth(ffmpeg_bin, d / "voiceB.wav", VOICE_B_SRC, VOICE_B_FILTER, TURN_S),
        "-": synth(ffmpeg_bin, d / "silence.wav", "aevalsrc=0", "anull", GAP_S),
    }


def alternating(ffmpeg: str, directory: Path, pieces: dict[str, Path], turns: str) -> Path:
    """`turns` like "ABAB": those voices in order, 1 s of silence between."""
    sequence = [pieces[t] for turn in turns for t in (turn, "-")][:-1]
    return concat(ffmpeg, directory / f"{turns}.wav", sequence)


@pytest.fixture(scope="module")
def two_voice_wav(tmp_path_factory, ffmpeg_bin: str, pieces) -> Path:
    return alternating(ffmpeg_bin, tmp_path_factory.mktemp("mix"), pieces, "ABABAB")


@pytest.fixture(scope="module")
def single_voice_wav(tmp_path_factory, ffmpeg_bin: str, pieces) -> Path:
    return alternating(ffmpeg_bin, tmp_path_factory.mktemp("mix"), pieces, "AAAA")


def turn_transcript(turn_count: int) -> list[Word]:
    """Words matching the fixture's turn grid — the same shape whisper yields.

    Times are computed from integers each time, never accumulated (float drift
    once broke the TS fixture by sliding a segment into the wrong turn).
    """
    words: list[Word] = []
    per_turn = max(2, round(TURN_S * 2.5))
    for turn in range(turn_count):
        for w in range(per_turn):
            start = turn * SPAN_S + (w * TURN_S) / per_turn
            words.append(
                Word(text=f" w{len(words)}", start=start, end=start + TURN_S / per_turn - 0.05)
            )
    return words


def cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return 1 - dot / (math.hypot(*a) * math.hypot(*b))


# --- The embeddings themselves ---------------------------------------------


def test_embeddings_are_24_dim_and_unit_length(two_voice_wav: Path, settings) -> None:
    segments = build_speech_segments(turn_transcript(6))
    assert len(segments) == 6, "fixture drift: expected one segment per turn"

    embeddings = segment_embeddings(two_voice_wav, segments, settings)

    assert len(embeddings) == len(segments)
    assert all(len(e) == 24 for e in embeddings)
    for e in embeddings:
        assert math.hypot(*e) == pytest.approx(1.0, abs=1e-6)


def test_same_voice_clusters_tight_and_voices_sit_far_apart(
    two_voice_wav: Path, settings
) -> None:
    """The §2 contract: within-voice cosine distance << across-voice."""
    segments = build_speech_segments(turn_transcript(6))
    embeddings = segment_embeddings(two_voice_wav, segments, settings)

    a, b = embeddings[0::2], embeddings[1::2]
    within = [cosine_distance(x, y) for grp in (a, b) for i, x in enumerate(grp) for y in grp[i + 1 :]]
    across = [cosine_distance(x, y) for x in a for y in b]

    assert max(within) < 0.2, f"within-voice spread too wide: {max(within)}"
    assert min(across) > 0.5, f"voices not separated: {min(across)}"
    assert max(within) * 3 < min(across)


def test_only_the_first_four_seconds_of_a_segment_are_heard(
    tmp_path_factory, ffmpeg_bin: str, pieces, two_voice_wav: Path, settings
) -> None:
    """An 8 s A-then-B segment must fingerprint as A: the cap keeps it local."""
    ab = concat(
        ffmpeg_bin, tmp_path_factory.mktemp("cap") / "ab.wav", [pieces["A"], pieces["B"]]
    )
    (capped,) = segment_embeddings(ab, [SpeechSegment(start=0.0, end=2 * TURN_S)], settings)

    pure = segment_embeddings(
        two_voice_wav,
        [SpeechSegment(start=0.0, end=TURN_S), SpeechSegment(start=SPAN_S, end=SPAN_S + TURN_S)],
        settings,
    )
    assert cosine_distance(capped, pure[0]) < 0.2  # sounds like A
    assert cosine_distance(capped, pure[1]) > 0.5  # not like B


# --- Through the shared judgement core --------------------------------------


def test_two_voices_come_out_as_two_speakers_with_alternating_turns(
    two_voice_wav: Path, settings
) -> None:
    words = turn_transcript(6)
    segments = build_speech_segments(words)
    embeddings = segment_embeddings(two_voice_wav, segments, settings)

    result = assign_speakers(words, segments, embeddings)

    assert result.speaker_count == 2
    assert result.confidence > 0.4
    assert len(result.turns) == 6
    for earlier, later in zip(result.turns, result.turns[1:]):
        assert later.speaker != earlier.speaker, "turns should alternate"
    # Equal speech time: the tie goes to whoever spoke first — voice A is 0.
    assert result.word_speakers[0] == 0
    assert result.word_speakers[-1] == 1
    # Every word inside one turn agrees on its speaker.
    per_turn = len(words) // 6
    for turn in range(6):
        labels = set(result.word_speakers[turn * per_turn : (turn + 1) * per_turn])
        assert len(labels) == 1
        assert labels == {turn % 2}


def test_a_single_voice_stays_one_speaker(single_voice_wav: Path, settings) -> None:
    """The false-split guard: one voice must never paint as two colors."""
    words = turn_transcript(4)
    segments = build_speech_segments(words)
    embeddings = segment_embeddings(single_voice_wav, segments, settings)

    result = assign_speakers(words, segments, embeddings)

    assert result.speaker_count == 1
    assert result.confidence == 0
    assert all(s == 0 for s in result.word_speakers)


# --- Degrading gracefully ---------------------------------------------------


def test_a_silent_segment_embeds_empty(two_voice_wav: Path, settings) -> None:
    """The 1 s gap between turns has no voiced frames — [] , not a guess."""
    gap = SpeechSegment(start=TURN_S + 0.05, end=TURN_S + GAP_S - 0.05)
    assert segment_embeddings(two_voice_wav, [gap], settings) == [[]]


def test_a_segment_past_the_end_of_audio_embeds_empty(
    two_voice_wav: Path, settings
) -> None:
    beyond = SpeechSegment(start=500.0, end=504.0)
    assert segment_embeddings(two_voice_wav, [beyond], settings) == [[]]


def test_a_segment_too_short_for_five_frames_embeds_empty(
    two_voice_wav: Path, settings
) -> None:
    sliver = SpeechSegment(start=0.0, end=0.05)  # 800 samples ≈ 2 frames
    assert segment_embeddings(two_voice_wav, [sliver], settings) == [[]]


def test_no_segments_is_an_empty_answer(two_voice_wav: Path, settings) -> None:
    assert segment_embeddings(two_voice_wav, [], settings) == []


def test_diarization_off_returns_all_empty(two_voice_wav: Path, settings) -> None:
    off = replace(settings, diarization="off")
    segments = build_speech_segments(turn_transcript(6))
    assert segment_embeddings(two_voice_wav, segments, off) == [[] for _ in segments]


def test_diarization_off_never_touches_ffmpeg(two_voice_wav: Path, settings) -> None:
    """The off switch must short-circuit before any decoding is attempted."""
    off = replace(settings, diarization="off", ffmpeg_bin="/nonexistent/ffmpeg")
    seg = SpeechSegment(start=0.0, end=TURN_S)
    assert segment_embeddings(two_voice_wav, [seg], off) == [[]]


def test_env_var_turns_diarization_off(
    two_voice_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CC_DIARIZATION", "off")
    get_settings.cache_clear()
    try:
        seg = SpeechSegment(start=0.0, end=TURN_S)
        assert segment_embeddings(two_voice_wav, [seg], get_settings()) == [[]]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["off", "0", "false", "no", ""])
def test_off_switch_accepts_the_usual_spellings(settings, value: str) -> None:
    assert diarization_enabled(replace(settings, diarization=value)) is False


@pytest.mark.parametrize("value", ["on", "1", "true", "ON"])
def test_on_is_the_default_and_the_obvious_spellings(settings, value: str) -> None:
    assert settings.diarization == "on"
    assert diarization_enabled(replace(settings, diarization=value)) is True


def test_missing_ffmpeg_raises(two_voice_wav: Path, settings) -> None:
    """The one thing worth raising for: we could not even run the decoder."""
    broken = replace(settings, ffmpeg_bin="/nonexistent/ffmpeg")
    with pytest.raises(TranscribeError):
        segment_embeddings(two_voice_wav, [SpeechSegment(start=0.0, end=TURN_S)], broken)


def test_undecodable_input_raises_transcribe_error(settings, tmp_path: Path) -> None:
    """ffmpeg exiting in error before the PCM arrives is ffmpeg failing —
    reported as TranscribeError; the worker treats the stage as best-effort."""
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"not audio" * 500)
    with pytest.raises(TranscribeError):
        segment_embeddings(garbage, [SpeechSegment(start=0.0, end=TURN_S)], settings)
