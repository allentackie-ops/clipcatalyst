"""Behavioural tests for the shared speaker-assignment core.

The first twenty assertions mirror, one for one, the TypeScript suite in
``scripts/diarize.test.mjs`` that guards ``lib/studio/diarize.ts`` (the
reference implementation). The last three tests keep the port honest over time:
they compile the TypeScript, run both implementations over identical embedding
fixtures, and demand the clustering decisions agree exactly. If they ever fail,
the Python side is the side that moves.

Fixture construction note (inherited from the JS suite): word times are
computed from integers each time, never accumulated -- float drift once made
turn 4 start at 19.999..., so ``floor(start / span)`` mapped a segment to the
WRONG turn's voice and the fixture blamed the module for faithfully labeling
the inconsistent input it was given.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from clipcatalyst_api.pipeline.diarize import (
    MAX_SPEAKERS,
    SPEAKER_COLORS,
    _js_round,
    assign_speakers,
    build_speech_segments,
)
from clipcatalyst_api.pipeline.types import Word

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_SOURCE = REPO_ROOT / "lib" / "studio" / "diarize.ts"


# --- fixtures (faithful ports of the JS suite's helpers) --------------------


def _noise(seed: int):
    """Deterministic pseudo-noise -- same LCG as the JS suite, bit for bit.

    ``Math.imul(s, 1664525) + 1013904223 >>> 0`` is exactly
    ``(s * 1664525 + 1013904223) mod 2**32`` for uint32 ``s``.
    """
    s = seed & 0xFFFFFFFF

    def rnd() -> float:
        nonlocal s
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        return (s / 2**32 - 0.5) * 2

    return rnd


def _voice(base: list[float], jitter: float, rnd) -> list[float]:
    """Unit vector with jitter, renormalized -- one "voice" = one direction."""
    v = [x + jitter * rnd() for x in base]
    n = math.hypot(*v)
    return [x / n for x in v]


A = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
C = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
D = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
E = [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]


def _conversation(turn_bases: list[list[float]], secs: float, jitter: float, seed: int):
    """Words + segments + embeddings for alternating turns of ``secs`` seconds.

    Word times come from integers each time, never accumulated (see module
    docstring for why).
    """
    rnd = _noise(seed)
    words: list[Word] = []
    span = secs + 1.0  # turn + pause
    for turn in range(len(turn_bases)):
        words_in_turn = max(2, int(_js_round(secs * 2.5)))
        for w in range(words_in_turn):
            start = turn * span + (w * secs) / words_in_turn
            words.append(
                Word(text=f" w{len(words)}", start=start, end=start + secs / words_in_turn - 0.05)
            )
    segments = build_speech_segments(words)
    # One embedding per segment: whichever turn the segment's midpoint is in.
    embeddings: list[list[float]] = []
    for seg in segments:
        mid = (seg.start + seg.end) / 2
        turn = min(len(turn_bases) - 1, math.floor(mid / span))
        embeddings.append(_voice(turn_bases[turn], jitter, rnd))
    return words, segments, embeddings


def _interjection_fixture():
    """A dominant host with a brief 2s guest interjection (JS test 4)."""
    rnd = _noise(5)
    words: list[Word] = []
    t = 0.0

    def mk(base: list[float], secs: float):
        nonlocal t
        idxs: list[int] = []
        n = max(2, int(_js_round(secs * 2.5)))
        for _ in range(n):
            idxs.append(len(words))
            words.append(Word(text=f" w{len(words)}", start=t, end=t + secs / n - 0.05))
            t += secs / n
        t += 1.0
        return (base, idxs)

    layout = [mk(A, 6), mk(A, 6), mk(B, 2), mk(A, 6), mk(A, 6)]
    segs = build_speech_segments(words)
    emb: list[list[float]] = []
    for seg in segs:
        chosen: list[float] | None = None
        for base, idxs in layout:
            s = words[idxs[0]].start
            e = words[idxs[-1]].end
            if seg.start >= s - 0.01 and seg.start <= e + 0.01:
                chosen = _voice(base, 0.05, rnd)
                break
        emb.append(chosen if chosen is not None else _voice(A, 0.05, rnd))
    return words, segs, emb


# --- 1. the false-split guard: ONE voice with heavy prosody stays one -------


def test_one_varied_voice_stays_one_speaker() -> None:
    words, segments, embeddings = _conversation([A, A, A, A, A, A], 4, 0.45, 7)
    r = assign_speakers(words, segments, embeddings)
    assert r.speaker_count == 1, f"got {r.speaker_count}"


def test_single_speaker_has_zero_confidence() -> None:
    words, segments, embeddings = _conversation([A, A, A, A, A, A], 4, 0.45, 7)
    r = assign_speakers(words, segments, embeddings)
    assert r.confidence == 0


def test_single_speaker_assigns_every_word_speaker_zero() -> None:
    words, segments, embeddings = _conversation([A, A, A, A, A, A], 4, 0.45, 7)
    r = assign_speakers(words, segments, embeddings)
    assert all(s == 0 for s in r.word_speakers)


# --- 2. two clearly distinct voices alternating -----------------------------


def _two_voices():
    return _conversation([A, B, A, B, A, B], 4, 0.08, 11)


def test_two_voices_give_two_speakers() -> None:
    words, segments, embeddings = _two_voices()
    r = assign_speakers(words, segments, embeddings)
    assert r.speaker_count == 2, f"got {r.speaker_count}"


def test_two_voices_are_confident() -> None:
    words, segments, embeddings = _two_voices()
    r = assign_speakers(words, segments, embeddings)
    assert r.confidence > 0.4, f"conf={r.confidence}"


def test_alternating_turns_get_different_labels() -> None:
    words, segments, embeddings = _two_voices()
    r = assign_speakers(words, segments, embeddings)
    first = r.word_speakers[0]
    second = r.word_speakers[len(words) - 1]
    assert first != second, f"{first} vs {second}"


def test_turns_alternate() -> None:
    words, segments, embeddings = _two_voices()
    r = assign_speakers(words, segments, embeddings)
    assert len(r.turns) >= 4, f"turns={len(r.turns)}"
    assert all(t.speaker != r.turns[i - 1].speaker for i, t in enumerate(r.turns) if i > 0)


# --- 3. equal speech -> labels by speech time desc, ties by first start -----


def test_speaker_zero_speaks_first_on_a_tie() -> None:
    words, segments, embeddings = _conversation([A, B, A, B], 4, 0.05, 3)
    r = assign_speakers(words, segments, embeddings)
    assert r.word_speakers[0] == 0, f"got {r.word_speakers[0]}"


# --- 4. a 2s guest interjection is under the speech floor and absorbed ------


def test_short_interjection_under_the_floor_is_absorbed() -> None:
    words, segs, emb = _interjection_fixture()
    r = assign_speakers(words, segs, emb)
    assert r.speaker_count == 1, f"got {r.speaker_count}"


# --- 5. three and five voices: found, and capped at MAX_SPEAKERS ------------


def test_three_voices_give_three_speakers() -> None:
    words, segments, embeddings = _conversation([A, B, C, A, B, C], 4, 0.06, 13)
    r = assign_speakers(words, segments, embeddings)
    assert r.speaker_count == 3, f"got {r.speaker_count}"


def test_five_voices_capped_at_max_speakers() -> None:
    words, segments, embeddings = _conversation([A, B, C, D, E, A, B, C, D, E], 4, 0.05, 17)
    r = assign_speakers(words, segments, embeddings)
    assert r.speaker_count <= MAX_SPEAKERS, f"got {r.speaker_count}"


# --- 6. robustness: empty input, no embeddings, mismatched lengths ----------


def test_empty_input_gives_a_sane_result() -> None:
    r = assign_speakers([], [], [])
    assert r.speaker_count == 1
    assert r.turns == []


def test_no_usable_embeddings_gives_a_single_speaker() -> None:
    words = [Word(text=" hi", start=0.0, end=0.4)]
    segs = build_speech_segments(words)
    r = assign_speakers(words, segs, [[] for _ in segs])
    assert r.speaker_count == 1
    assert r.word_speakers[0] == 0


def test_mismatched_embedding_count_gives_a_single_speaker() -> None:
    words = [Word(text=" hi", start=0.0, end=0.4)]
    segs = build_speech_segments(words)
    r = assign_speakers(words, segs, [])
    assert r.speaker_count == 1


# --- 7. determinism: identical input -> identical output --------------------


def test_deterministic() -> None:
    words, segments, embeddings = _conversation([A, B, A, B], 4, 0.08, 23)
    r1 = assign_speakers(words, segments, embeddings)
    r2 = assign_speakers(words, segments, embeddings)
    assert r1 == r2


# --- 8. segmentation: gaps split, monologues split, every word covered ------


def _gappy_words() -> list[Word]:
    words: list[Word] = []
    t = 0.0
    for i in range(50):
        words.append(Word(text=f" w{i}", start=t, end=t + 0.3))
        t += 2.0 if i == 24 else 0.35  # one big gap in the middle
    return words


def test_gap_splits_segments() -> None:
    segs = build_speech_segments(_gappy_words())
    assert len(segs) >= 2, f"got {len(segs)}"


def test_every_word_belongs_to_a_segment() -> None:
    words = _gappy_words()
    segs = build_speech_segments(words)
    covered = {i for s in segs for i in s.word_idxs}
    assert len(covered) == len(words), f"{len(covered)}/{len(words)}"


def test_segments_respect_the_max_length() -> None:
    segs = build_speech_segments(_gappy_words())
    longest = max(s.end - s.start for s in segs)
    assert all(s.end - s.start <= 6.5 for s in segs), f"max={longest:.1f}"


# --- 9. palette contract: colors exist for every possible speaker -----------


def test_palette_covers_max_speakers() -> None:
    assert len(SPEAKER_COLORS) == MAX_SPEAKERS


def test_speaker_zero_keeps_the_brand_violet() -> None:
    assert SPEAKER_COLORS[0] == "#a78bfa"


# --- cross-implementation check ---------------------------------------------

_DRIVER = """
import { assignSpeakers, buildSpeechSegments } from "./diarize.mjs";
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(process.argv[2], "utf8"));
const segments = buildSpeechSegments(input.words);
const r = assignSpeakers(input.words, segments, input.embeddings);
process.stdout.write(JSON.stringify({
  segments: segments.map((s) => ({ start: s.start, end: s.end, wordIdxs: s.wordIdxs })),
  speakerCount: r.speakerCount,
  wordSpeakers: r.wordSpeakers.map((s) => (s === undefined ? null : s)),
  turns: r.turns,
  confidence: r.confidence,
}));
"""


@pytest.fixture(scope="session")
def js_module(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the reference TypeScript to an importable ES module, or skip."""
    tmp_path = tmp_path_factory.mktemp("diarize-ts")
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available; cannot cross-check against diarize.ts")
    target = tmp_path / "diarize.mjs"

    tsc = REPO_ROOT / "node_modules" / ".bin" / "tsc"
    if tsc.exists() and TS_SOURCE.exists():
        proc = subprocess.run(
            [
                str(tsc),
                str(TS_SOURCE),
                "--outDir",
                str(tmp_path),
                "--module",
                "es2020",
                "--target",
                "es2020",
                "--skipLibCheck",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        emitted = tmp_path / "diarize.js"
        if not emitted.exists():
            pytest.skip(f"tsc did not emit diarize.js: {proc.stdout or proc.stderr}")
        target.write_text(emitted.read_text(encoding="utf8"), encoding="utf8")
        _write_driver(tmp_path)
        return target

    prebuilt = REPO_ROOT / ".croptrack-build" / "diarize.js"
    if (
        prebuilt.exists()
        and TS_SOURCE.exists()
        and prebuilt.stat().st_mtime >= TS_SOURCE.stat().st_mtime
    ):
        target.write_text(prebuilt.read_text(encoding="utf8"), encoding="utf8")
        _write_driver(tmp_path)
        return target
    pytest.skip("no TypeScript compiler and no fresh prebuilt diarize.js to compare against")


def _write_driver(tmp_path: Path) -> None:
    (tmp_path / "driver.mjs").write_text(_DRIVER, encoding="utf8")


def _run_ts(module: Path, words: list[Word], embeddings: list[list[float]]):
    """Run the TypeScript build over the same input and return its result.

    JSON round-trips doubles exactly (shortest-repr both ways), so both
    implementations see bit-identical words and embeddings.
    """
    payload = {
        "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
        "embeddings": [list(e) for e in embeddings],
    }
    src = module.parent / "input.json"
    src.write_text(json.dumps(payload), encoding="utf8")
    proc = subprocess.run(
        [shutil.which("node") or "node", str(module.parent / "driver.mjs"), str(src)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(module.parent),
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _assert_bit_exact_agreement(js_module: Path, words, segments, embeddings):
    """Both engines, same fixture: segmentation, labels, turns must agree."""
    ts = _run_ts(js_module, words, embeddings)
    py = assign_speakers(words, segments, embeddings)

    # Segmentation must be identical, or the embeddings would not line up.
    assert len(ts["segments"]) == len(segments)
    for mine, theirs in zip(segments, ts["segments"]):
        assert mine.start == theirs["start"]
        assert mine.end == theirs["end"]
        assert mine.word_idxs == theirs["wordIdxs"]

    assert py.speaker_count == ts["speakerCount"]
    assert py.word_speakers == ts["wordSpeakers"]  # None == null after json.loads
    assert len(py.turns) == len(ts["turns"])
    for mine_t, theirs_t in zip(py.turns, ts["turns"]):
        assert mine_t.speaker == theirs_t["speaker"]
        assert abs(mine_t.start - theirs_t["start"]) < 1e-9
        assert abs(mine_t.end - theirs_t["end"]) < 1e-9
    assert abs(py.confidence - ts["confidence"]) < 1e-9
    return py, ts


def test_matches_typescript_on_two_voices(js_module: Path) -> None:
    """Labels, turns and confidence agree to 1e-9 on the two-voice case."""
    words, segments, embeddings = _two_voices()
    py, _ = _assert_bit_exact_agreement(js_module, words, segments, embeddings)
    # Only a real test if the split actually happens.
    assert py.speaker_count == 2
    assert py.word_speakers[0] != py.word_speakers[-1]
    assert py.confidence > 0.4


def test_matches_typescript_on_one_varied_voice(js_module: Path) -> None:
    """The 0.45-jitter single voice collapses to one speaker in BOTH engines.

    This pins the iterative closest-pair separation guard: a port that judged
    overall averages instead of the closest pair could split this cloud while
    the reference does not (or vice versa).
    """
    words, segments, embeddings = _conversation([A, A, A, A, A, A], 4, 0.45, 7)
    py, ts = _assert_bit_exact_agreement(js_module, words, segments, embeddings)
    assert py.speaker_count == 1
    assert ts["speakerCount"] == 1
    assert py.confidence == 0
    assert ts["confidence"] == 0


def test_matches_typescript_on_the_five_voice_cap(js_module: Path) -> None:
    """Five voices force merges past MAX_SPEAKERS -- the most order-sensitive
    path through the deterministic tie-breaks. Both engines must agree."""
    words, segments, embeddings = _conversation(
        [A, B, C, D, E, A, B, C, D, E], 4, 0.05, 17
    )
    py, _ = _assert_bit_exact_agreement(js_module, words, segments, embeddings)
    assert py.speaker_count <= MAX_SPEAKERS
