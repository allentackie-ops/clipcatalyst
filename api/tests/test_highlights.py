"""Tests for the Virality Engine port (clipcatalyst_api.pipeline.highlights).

Builds a synthetic ~47 s transcript of three 16 s "blocks", each opening with
a hooky sentence followed by calm filler sentences, plus synthetic
AudioFeatures (RMS with quiet gaps + matching silence spans). The block
openers are engineered so the three block-start windows are the strongest
candidates, giving three plans starting ≥ 15 s apart.
"""

from __future__ import annotations

from clipcatalyst_api.pipeline.highlights import plan_clips
from clipcatalyst_api.pipeline.types import (
    AudioFeatures,
    HighlightOptions,
    SilenceSpan,
    Transcript,
    Word,
)

HOP = 0.1  # seconds per RMS hop
BLOCK_SPACING = 16.0  # blocks start this far apart

# Hooky openers: question+numbers+you / question+intrigue (long, > 48 chars,
# forces a truncated "…" title) / superlatives+intrigue.
HOOK_SENTENCES = [
    "What if you could double your money in 30 days?",
    "Why do the smartest creators keep repeating the same expensive mistake every single week?",
    "Nobody tells you the secret truth behind the biggest launches.",
]

# Deliberately bland: no question openers, numbers, superlatives, intrigue,
# second person or contrast — so interior windows score below block starts.
FILLER_SENTENCES = [
    "The team spent months polishing the audio pipeline.",
    "A calm voice carried the story forward without hurry.",
    "The final render shipped after careful color grading.",
]


def _sentence_words(text: str, start: float, end: float) -> list[Word]:
    """Spread the sentence's words evenly over [start, end], Whisper-style
    (leading space on every word, small intra-word gaps < 0.8 s)."""
    tokens = text.split()
    slot = (end - start) / len(tokens)
    words: list[Word] = []
    for i, token in enumerate(tokens):
        s = start + i * slot
        words.append(
            Word(text=" " + token, start=round(s, 3), end=round(s + slot * 0.85, 3))
        )
    return words


def _build_fixture(
    hooks: list[str], audio_seconds: float
) -> tuple[Transcript, AudioFeatures]:
    """One 16 s block per hook: hook (4.5 s) + three fillers, 0.5 s gaps,
    0.8 s gap between blocks. RMS: 0.9 (spike) in hooks, 0.55 in speech,
    0.02 in gaps. Silences mirror the inter-sentence gaps."""
    words: list[Word] = []
    silences: list[SilenceSpan] = []
    hook_spans: list[tuple[float, float]] = []
    sentence_spans: list[tuple[float, float]] = []
    prev_end: float | None = None

    for block, hook in enumerate(hooks):
        t0 = BLOCK_SPACING * block
        layout = [
            (hook, t0 + 0.0, t0 + 4.5),
            (FILLER_SENTENCES[0], t0 + 5.0, t0 + 8.5),
            (FILLER_SENTENCES[1], t0 + 9.0, t0 + 12.0),
            (FILLER_SENTENCES[2], t0 + 12.5, t0 + 15.2),
        ]
        hook_spans.append((t0, t0 + 4.5))
        for text, s, e in layout:
            if prev_end is not None:
                silences.append(SilenceSpan(start=prev_end, end=s))
            words.extend(_sentence_words(text, s, e))
            sentence_spans.append((s, e))
            prev_end = e

    assert prev_end is not None
    if prev_end < audio_seconds:
        silences.append(SilenceSpan(start=prev_end, end=audio_seconds))

    rms: list[float] = []
    for i in range(int(round(audio_seconds / HOP))):
        t = i * HOP
        if any(s <= t < e for s, e in hook_spans):
            rms.append(0.9)  # spike level (> 0.85)
        elif any(s <= t < e for s, e in sentence_spans):
            rms.append(0.55)
        else:
            rms.append(0.02)  # quiet gap

    transcript = Transcript(words=words, text="".join(w.text for w in words))
    features = AudioFeatures(rms=rms, hop_seconds=HOP, silences=silences)
    return transcript, features


def _full_fixture() -> tuple[Transcript, AudioFeatures]:
    return _build_fixture(HOOK_SENTENCES, audio_seconds=48.0)


OPTIONS_3 = HighlightOptions(target_length=15, count=3)


# ---------------------------------------------------------------------------
# Count behavior
# ---------------------------------------------------------------------------


def test_returns_requested_count_when_windows_fit() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    assert len(plans) == 3

    one = plan_clips(transcript, features, HighlightOptions(target_length=15, count=1))
    assert len(one) == 1


def test_returns_fewer_when_windows_do_not_fit() -> None:
    # A single ~15 s block: only one window can exist with starts ≥ 15 s apart.
    transcript, features = _build_fixture([HOOK_SENTENCES[0]], audio_seconds=20.0)
    plans = plan_clips(transcript, features, OPTIONS_3)
    assert len(plans) == 1


def test_empty_transcript_returns_no_plans() -> None:
    _, features = _full_fixture()
    plans = plan_clips(Transcript(words=[], text=""), features, OPTIONS_3)
    assert plans == []


# ---------------------------------------------------------------------------
# Window geometry
# ---------------------------------------------------------------------------


def test_plans_non_overlapping_and_starts_spaced() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    assert len(plans) >= 2
    for plan in plans:
        assert 0.0 <= plan.start < plan.end
    for i, a in enumerate(plans):
        for b in plans[i + 1 :]:
            assert a.end <= b.start or b.end <= a.start  # no overlap
            assert abs(a.start - b.start) >= 15.0


def test_plans_clamped_to_audio_duration_when_transcript_overruns() -> None:
    # Regression: Whisper timestamps overrun the decoded audio — the audio is
    # ground truth, so no plan may extend past it.
    audio_seconds = 40.0
    transcript, features = _build_fixture(HOOK_SENTENCES, audio_seconds=audio_seconds)
    assert transcript.words[-1].end > audio_seconds  # transcript really overruns
    assert len(features.rms) * features.hop_seconds == audio_seconds

    plans = plan_clips(transcript, features, OPTIONS_3)
    assert len(plans) >= 1
    for plan in plans:
        assert plan.start >= 0.0
        assert plan.end <= audio_seconds + 1e-9
        assert plan.start < plan.end


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


def test_scores_in_range_and_spread() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    scores = [plan.score for plan in plans]
    for score in scores:
        assert isinstance(score, int)
        assert 0 <= score <= 100
        assert 35 <= score <= 96  # the engine's actual display band
    assert len(set(scores)) >= 2  # not all identical — the mapping spreads
    # Plans arrive strongest-first.
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Human-facing fields
# ---------------------------------------------------------------------------


def test_titles_nonempty_bounded_with_ellipsis_behavior() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    for plan in plans:
        assert plan.title
        assert len(plan.title) <= 49  # ≤ 48 chars + "…"
        assert plan.title[0] == plan.title[0].upper()
    titles = [plan.title for plan in plans]
    # The long hooks truncate at a word boundary and gain an ellipsis…
    assert any(t.endswith("…") for t in titles)
    # …while the short hook (< 48 chars) keeps its full text, no ellipsis.
    assert any(not t.endswith("…") for t in titles)
    assert "What if you could double your money in 30 days" in titles


def test_hooks_at_most_five_and_nonempty() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    for plan in plans:
        assert 1 <= len(plan.hooks) <= 5
        for hook in plan.hooks:
            assert hook
            assert len(hook) <= 80  # trimmed hooks: ≤ 79 chars + "…"


def test_reason_and_tip_present() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    for plan in plans:
        assert plan.reason.endswith(".")
        assert " and " in plan.reason
        assert plan.tip


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def test_words_rebased_to_clip_start() -> None:
    transcript, features = _full_fixture()
    plans = plan_clips(transcript, features, OPTIONS_3)
    for plan in plans:
        assert plan.words
        first = plan.words[0]
        assert 0.0 <= first.start < 1.0  # re-based: near the clip start
        clip_length = plan.end - plan.start
        for word in plan.words:
            assert 0.0 <= word.start <= word.end
            assert word.end <= clip_length + 1e-6
        starts = [w.start for w in plan.words]
        assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_for_same_inputs() -> None:
    transcript, features = _full_fixture()
    first = plan_clips(transcript, features, OPTIONS_3)
    second = plan_clips(transcript, features, OPTIONS_3)
    assert first == second
    assert [p.id for p in first] == [p.id for p in second]
    for index, plan in enumerate(first):
        assert plan.id == f"clip-{index}-{round(plan.start * 1000)}"
