"""Virality Engine v0 — faithful Python port of lib/studio/highlights.ts.

Plans the best clip windows from a transcript + audio features. Pure and
deterministic: same inputs always yield the same plans (no clocks, no RNG —
the only "noise" is a seeded FNV-1a string hash used for score dithering).

Pipeline: words → sentences → candidate windows (sentence-aligned, near the
target length) → six-component score → greedy non-overlapping selection →
silence/sentence snapping → human-facing title/hooks/reason/tip per plan.

Constants, lexicons and weights are kept byte-identical with the TS source so
the two engines produce the same plans for the same inputs.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .types import (
    AudioFeatures,
    ClipPlan,
    HighlightOptions,
    Sentence,
    SilenceSpan,
    Transcript,
    Word,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_WORD_GAP_S = 0.8  # inter-word gap that forces a sentence break
MAX_SENTENCE_WORDS = 25  # hard cap per sentence
MIN_LENGTH_RATIO = 0.6  # accepted window: [0.6, 1.35] × target_length
MAX_LENGTH_RATIO = 1.35
MIN_START_GAP_S = 15  # selected clips must start ≥ 15 s apart
COLD_OPEN_CUTOFF_S = 8  # windows starting earlier get penalized
SNAP_RADIUS_S = 0.75  # how far we look for a silence edge to snap to
LEAD_IN_S = 0.15  # breathing room added before the first word
TAIL_S = 0.25  # breathing room added after the last word
ENERGY_SPIKE_LEVEL = 0.85  # normalized RMS counting as a spike
DENSITY_FULL_MARKS = 0.9  # unique content words/sec that maps to 1.0

#: Relative weight of each scoring component (penalties subtract).
WEIGHTS: dict[str, float] = {
    "hook": 0.34,
    "density": 0.22,
    "energy": 0.2,
    "completeness": 0.08,
    "pause": 0.16,
    "coldOpen": 0.1,
}

# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

#: Small stopword/filler set used only for info-density (tokens ≥ 4 chars).
STOPWORDS = {
    "that", "this", "these", "those", "there", "then", "than", "them", "they",
    "their", "theirs", "what", "when", "where", "which", "while", "with",
    "without", "will", "would", "could", "should", "shall", "might", "must",
    "have", "having", "been", "being", "were", "does", "doing", "from", "into",
    "over", "under", "about", "after", "before", "again", "because", "just",
    "very", "really", "quite", "some", "such", "only", "also", "your", "yours",
    "youre", "youve", "youll", "thats", "theyre", "dont", "cant", "wont",
    "didnt", "doesnt", "isnt", "arent", "gonna", "gotta", "wanna", "yeah",
    "okay", "right", "well", "like", "know", "going", "want", "make", "makes",
    "made", "thing", "things", "stuff", "kind", "sort", "mean", "means",
    "actually", "basically", "literally", "think", "thought", "says", "said",
    "look", "looks", "come", "comes", "came", "gets", "getting", "every",
    "even", "much", "more", "most", "here", "been", "lets", "little", "people",
    "time", "ways",
}

#: Hook lexicon as scored categories — each has its own weight and hit test.
#: Insertion order matters: it decides ties for the dominant category, exactly
#: like Object.keys(HOOK_WEIGHTS) iteration order in the TS source.
HOOK_WEIGHTS: dict[str, float] = {
    "question": 0.9,  # questions open a loop the viewer wants closed
    "intrigue": 0.75,  # secrets/mistakes/truths promise a payoff
    "superlative": 0.65,  # absolutes are bold claims that invite reaction
    "number": 0.6,  # specificity reads as credibility
    "secondPerson": 0.55,  # "you" makes it personal
    "contrast": 0.5,  # "but / here's the thing" signals a turn
}

QUESTION_OPENERS = {
    "what", "why", "how", "who", "when", "where", "which", "did", "do", "does",
    "is", "are", "was", "can", "could", "would", "should", "have", "has",
    "ever", "imagine", "guess",
}

SECOND_PERSON = {
    "you", "your", "youre", "yours", "yourself", "youll", "youve", "youd",
}

SUPERLATIVES = {
    "best", "worst", "never", "always", "nobody", "everyone", "everybody",
    "everything", "nothing", "biggest", "smallest", "greatest", "fastest",
    "easiest", "hardest", "most", "least", "only", "guaranteed", "impossible",
    "ultimate", "perfect", "ever",
}

INTRIGUE_WORDS = {
    "secret", "secrets", "mistake", "mistakes", "truth", "wrong", "actually",
    "honestly", "surprised", "surprising", "surprise", "insane", "crazy",
    "shocking", "shocked", "hidden", "warning", "weird", "unbelievable",
    "hack", "hacks", "trick", "tricks", "lie", "lies", "lied", "exposed",
    "problem", "dangerous",
}

CONTRAST_OPENERS = {
    "but", "instead", "however", "meanwhile", "except", "yet",
}

CONTRAST_PHRASES = [
    "here's the thing", "heres the thing", "the problem is", "the truth is",
    "what if", "turns out", "it turns out", "plot twist", "the catch is",
]

FILLER_OPENERS = {
    "so", "and", "um", "uh", "like", "well", "okay", "ok", "right", "yeah",
    "anyway", "also", "basically",
}

FILLER_PHRASES = ["you know", "i mean", "sort of", "kind of"]

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def _js_round(value: float) -> int:
    """JS Math.round — half-way cases round toward +infinity."""
    return math.floor(value + 0.5)


def _round3(value: float) -> float:
    return _js_round(value * 1000) / 1000


def _hash_string(text: str) -> int:
    """FNV-1a — a tiny deterministic string hash used only for score dithering.

    Iterates UTF-16 code units to match JS ``charCodeAt`` exactly.
    """
    h = 2166136261
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


_TOKEN_JUNK_RE = re.compile(r"[^a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric tokens with punctuation and apostrophes removed."""
    cleaned = _TOKEN_JUNK_RE.sub(" ", text.lower())
    out: list[str] = []
    for raw in cleaned.split(" "):
        token = raw.replace("'", "")
        if token:
            out.append(token)
    return out


_TERMINAL_RE = re.compile(r"[.?!][\"')\]»”’]*$")


def _ends_with_terminal(text: str) -> bool:
    return _TERMINAL_RE.search(text.strip()) is not None


def _format_clip_time(seconds: float) -> str:
    s = max(0, _js_round(seconds))
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# Sentence building
# ---------------------------------------------------------------------------

_WS_RUN_RE = re.compile(r"\s+")


def _build_sentences(words: list[Word]) -> list[Sentence]:
    sentences: list[Sentence] = []
    current: list[Word] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = _WS_RUN_RE.sub(" ", "".join(w.text for w in current)).strip()
        if text:
            sentences.append(
                Sentence(
                    text=text,
                    start=current[0].start,
                    end=current[-1].end,
                    words=current,
                )
            )
        current = []

    for i, word in enumerate(words):
        current.append(word)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt.start - word.end) if nxt is not None else math.inf
        if (
            _ends_with_terminal(word.text)
            or gap > MAX_WORD_GAP_S
            or len(current) >= MAX_SENTENCE_WORDS
        ):
            flush()
    flush()
    return sentences


# ---------------------------------------------------------------------------
# Scoring components
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _HookInfo:
    #: 0..1 saturating combination of all category hits.
    score: float
    #: The category contributing most — drives the "reason" phrasing.
    dominant: str | None


_NUMBER_RE = re.compile(r"\$\s*\d[\d,.]*|\d[\d,.]*\s*%|\b\d[\d,.]*\b", re.ASCII)


def _score_hook(text: str) -> _HookInfo:
    tokens = _tokenize(text)
    lower = text.lower()
    first = tokens[0] if tokens else None
    hits: dict[str, float] = {}

    if "?" in lower or (first is not None and first in QUESTION_OPENERS):
        hits["question"] = 1.0

    second_person = 0
    superlative = 0
    intrigue = 0
    for t in tokens:
        if t in SECOND_PERSON:
            second_person += 1
        if t in SUPERLATIVES:
            superlative += 1
        if t in INTRIGUE_WORDS:
            intrigue += 1
    if second_person > 0:
        hits["secondPerson"] = min(1.0, second_person / 2)
    if superlative > 0:
        hits["superlative"] = min(1.0, superlative / 2)
    if intrigue > 0:
        hits["intrigue"] = min(1.0, intrigue / 2)

    number_matches = _NUMBER_RE.findall(lower)
    if number_matches:
        hits["number"] = min(1.0, len(number_matches) / 2)

    if (first is not None and first in CONTRAST_OPENERS) or any(
        p in lower for p in CONTRAST_PHRASES
    ):
        hits["contrast"] = 1.0

    total = 0.0
    dominant: str | None = None
    dominant_value = 0.0
    for key, weight in HOOK_WEIGHTS.items():
        hit = hits.get(key)
        if hit is None:
            continue
        contribution = weight * hit
        total += contribution
        if contribution > dominant_value:
            dominant_value = contribution
            dominant = key
    # Saturating squash: one strong category ≈ 0.63, stacked categories → 0.9+.
    return _HookInfo(score=1 - math.exp(-1.1 * total), dominant=dominant)


def _info_density(sentences: list[Sentence], duration_seconds: float) -> float:
    """Unique content words (≥ 4 chars, not stopwords) per second, normalized."""
    if duration_seconds <= 0:
        return 0.0
    unique: set[str] = set()
    for sentence in sentences:
        for token in _tokenize(sentence.text):
            if len(token) >= 4 and token not in STOPWORDS:
                unique.add(token)
    return _clamp01(len(unique) / duration_seconds / DENSITY_FULL_MARKS)


def _energy_of(
    features: AudioFeatures, start: float, end: float
) -> tuple[float, bool]:
    rms = features.rms
    hop_seconds = features.hop_seconds
    if hop_seconds <= 0:
        return 0.0, False
    lo = max(0, math.floor(start / hop_seconds))
    hi = min(len(rms), math.ceil(end / hop_seconds))
    if hi <= lo:
        return 0.0, False
    total = 0.0
    spike = False
    for v in rms[lo:hi]:
        total += v
        if v > ENERGY_SPIKE_LEVEL:
            spike = True
    return _clamp01(total / (hi - lo) + (0.15 if spike else 0.0)), spike


def _pause_fraction(
    silences: list[SilenceSpan], start: float, end: float
) -> float:
    duration = end - start
    if duration <= 0:
        return 0.0
    covered = 0.0
    for s in silences:
        covered += max(0.0, min(s.end, end) - max(s.start, start))
    return _clamp01(covered / duration)


def _longest_pause_in(
    silences: list[SilenceSpan], start: float, end: float
) -> tuple[float, float] | None:
    """Longest silence overlapping [start, end] → (at, length), or None."""
    best: tuple[float, float] | None = None
    for s in silences:
        lo = max(s.start, start)
        hi = min(s.end, end)
        length = hi - lo
        if length <= 0:
            continue
        if best is None or length > best[1]:
            best = (lo, length)
    return best


@dataclass(frozen=True)
class _WindowScore:
    hook: _HookInfo
    density: float
    energy: float
    spike: bool
    pause: float
    completeness: float
    cold_open: float
    raw: float
    #: 0-100 display score.
    score: int


def _score_window(
    sentences: list[Sentence],
    start: float,
    end: float,
    features: AudioFeatures,
) -> _WindowScore:
    first = sentences[0]
    last = sentences[-1]

    hook = _score_hook(first.text)
    density = _info_density(sentences, end - start)
    energy, spike = _energy_of(features, start, end)
    pause = _pause_fraction(features.silences, start, end)
    completeness = 1.0 if _ends_with_terminal(last.text) else 0.0
    cold_open = (
        0.0
        if start >= COLD_OPEN_CUTOFF_S
        else (COLD_OPEN_CUTOFF_S - start) / COLD_OPEN_CUTOFF_S
    )

    raw = (
        WEIGHTS["hook"] * hook.score
        + WEIGHTS["density"] * density
        + WEIGHTS["energy"] * energy
        + WEIGHTS["completeness"] * completeness
        - WEIGHTS["pause"] * pause
        - WEIGHTS["coldOpen"] * cold_open
    )

    return _WindowScore(
        hook=hook,
        density=density,
        energy=energy,
        spike=spike,
        pause=pause,
        completeness=completeness,
        cold_open=cold_open,
        raw=raw,
        score=_to_display_score(raw, f"{_js_round(start * 1000)}|{first.text}"),
    )


def _to_display_score(raw: float, seed: str) -> int:
    """Map the raw weighted sum (≈ −0.26..0.84) onto 0-100 with spread: winners
    land in the 70s-90s, weak windows in the 40s-60s, never 100. A ±1
    hash-seeded dither breaks ties without breaking determinism.
    """
    norm = _clamp01((raw - 0.02) / 0.66)
    base = 38 + 57 * norm**0.9
    dither = (_hash_string(seed) % 3) - 1
    return _js_round(_clamp(base + dither, 35, 96))


# ---------------------------------------------------------------------------
# Snapping — align cut points with silence edges for clean ins and outs
# ---------------------------------------------------------------------------


def _snap_start(
    raw_start: float, silences: list[SilenceSpan], video_duration: float
) -> float:
    edge = raw_start
    best_distance = math.inf
    for s in silences:
        distance = abs(s.end - raw_start)
        if (
            s.end <= raw_start + 0.2
            and distance <= SNAP_RADIUS_S
            and distance < best_distance
        ):
            best_distance = distance
            edge = s.end
    return _round3(_clamp(edge - LEAD_IN_S, 0.0, video_duration))


def _snap_end(
    raw_end: float, silences: list[SilenceSpan], video_duration: float
) -> float:
    edge = raw_end
    best_distance = math.inf
    for s in silences:
        distance = abs(s.start - raw_end)
        if (
            s.start >= raw_end - 0.2
            and distance <= SNAP_RADIUS_S
            and distance < best_distance
        ):
            best_distance = distance
            edge = s.start
    return _round3(_clamp(edge + TAIL_S, 0.0, video_duration))


# ---------------------------------------------------------------------------
# Candidate windows + selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    sentences: list[Sentence]
    #: Snapped, clamped bounds.
    start: float
    end: float
    score: _WindowScore


def _make_candidate(
    slice_: list[Sentence], features: AudioFeatures, video_duration: float
) -> _Candidate:
    start = _snap_start(slice_[0].start, features.silences, video_duration)
    end = max(
        _snap_end(slice_[-1].end, features.silences, video_duration),
        _round3(min(video_duration, start + 1)),
    )
    return _Candidate(
        sentences=slice_,
        start=start,
        end=end,
        score=_score_window(slice_, start, end, features),
    )


def _build_candidates(
    sentences: list[Sentence],
    target_length: float,
    features: AudioFeatures,
    video_duration: float,
) -> list[_Candidate]:
    min_duration = MIN_LENGTH_RATIO * target_length
    max_duration = MAX_LENGTH_RATIO * target_length
    candidates: list[_Candidate] = []

    for i in range(len(sentences)):
        # Extend whole sentences until we reach the target (or run out).
        j = i
        while (
            j + 1 < len(sentences)
            and sentences[j].end - sentences[i].start < target_length
        ):
            j += 1
        duration = sentences[j].end - sentences[i].start
        # If the last sentence overshot the cap, try backing off one sentence.
        if duration > max_duration and j > i:
            shorter = sentences[j - 1].end - sentences[i].start
            if shorter >= min_duration:
                j -= 1
                duration = shorter
        if duration < min_duration or duration > max_duration:
            continue
        # Windows starting at (or clamped into) the very end of the video can't
        # produce a real clip.
        if sentences[i].start >= video_duration - max(3.0, min_duration / 2):
            continue
        candidate = _make_candidate(sentences[i : j + 1], features, video_duration)
        if candidate.end - candidate.start >= min(min_duration, 5.0):
            candidates.append(candidate)
    return candidates


def _select_top(candidates: list[_Candidate], count: int) -> list[_Candidate]:
    """Greedy: best score first; no overlaps; starts ≥ 15 s apart."""
    ranked = sorted(
        candidates, key=lambda c: (-c.score.score, -c.score.raw, c.start)
    )
    picked: list[_Candidate] = []
    for candidate in ranked:
        if len(picked) >= count:
            break
        compatible = all(
            (candidate.end <= p.start or candidate.start >= p.end)
            and abs(candidate.start - p.start) >= MIN_START_GAP_S
            for p in picked
        )
        if compatible:
            picked.append(candidate)
    return picked  # already sorted by score desc


# ---------------------------------------------------------------------------
# Human-facing plan fields
# ---------------------------------------------------------------------------

_FILLER_OPENER_RE = re.compile(r"^([A-Za-z']+)[\s,]+")
_LEADING_WS_COMMA_RE = re.compile(r"^[\s,]+")


def _strip_filler_openers(text: str) -> str:
    t = text.strip()
    while True:
        lower = t.lower()
        phrase = next(
            (
                p
                for p in FILLER_PHRASES
                if lower.startswith(f"{p} ") or lower.startswith(f"{p}, ")
            ),
            None,
        )
        if phrase is not None:
            t = _LEADING_WS_COMMA_RE.sub("", t[len(phrase) :])
            continue
        match = _FILLER_OPENER_RE.match(t)
        if match and match.group(1).lower() in FILLER_OPENERS:
            t = t[match.end() :]
            continue
        break
    return t.strip()


def _truncate_at_word(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    slice_ = text[: max_len + 1]
    last_space = slice_.rfind(" ")
    return (slice_[:last_space] if last_space > 0 else text[:max_len]).strip()


_TRAILING_PUNCTUATION_RE = re.compile(r"[\s.,!?;:…\"'“”‘’-]+$")


def _make_title(sentence: Sentence) -> str:
    t = _strip_filler_openers(sentence.text)
    if len(t) == 0:
        t = sentence.text.strip()
    t = _TRAILING_PUNCTUATION_RE.sub("", t)
    full = t
    t = _TRAILING_PUNCTUATION_RE.sub("", _truncate_at_word(t, 48))
    if len(t) == 0:
        t = "Untitled moment"
    title = t[:1].upper() + t[1:]
    return f"{title}…" if len(t) < len(full) else title


def _strongest_sentence(sentences: list[Sentence]) -> Sentence:
    """Strongest sentence = best hook, with a mild prior toward fuller lines."""
    best = sentences[0]
    best_strength = -1.0
    for sentence in sentences:
        strength = _score_hook(sentence.text).score + 0.12 * min(
            1.0, len(sentence.words) / 8
        )
        if strength > best_strength + 1e-9:
            best_strength = strength
            best = sentence
    return best


_HOOK_TRAIL_RE = re.compile(r"[\s.,;:-]+$")


def _trim_hook(text: str) -> str:
    t = text.strip()
    if len(t) <= 80:
        return t
    return f"{_HOOK_TRAIL_RE.sub('', _truncate_at_word(t, 79))}…"


def _make_hooks(sentences: list[Sentence]) -> list[str]:
    scored = [
        (sentence, order, _score_hook(sentence.text).score)
        for order, sentence in enumerate(sentences)
    ]
    scored.sort(key=lambda item: (-item[2], item[1]))
    hooks = [_trim_hook(sentence.text) for sentence, _, _ in scored[:5]]
    return [h for h in hooks if len(h) > 0]


_HOOK_FRAGMENTS = {
    "question": "opens on a question",
    "secondPerson": "talks straight to the viewer",
    "number": "leads with hard numbers",
    "superlative": "makes a bold, absolute claim",
    "intrigue": "teases a payoff you want to hear",
    "contrast": "opens on a sharp contrast",
}


def _hook_fragment(hook: _HookInfo) -> str:
    if hook.dominant is None:
        return "comes in with a strong first line"
    return _HOOK_FRAGMENTS.get(hook.dominant, "comes in with a strong first line")


def _make_reason(score: _WindowScore) -> str:
    """One concrete line naming the two strongest scoring components."""
    fragments = [
        (WEIGHTS["hook"] * score.hook.score, _hook_fragment(score.hook)),
        (WEIGHTS["density"] * score.density, "packs ideas in tight"),
        (
            WEIGHTS["energy"] * score.energy,
            "rides an energy spike" if score.spike else "keeps the vocal energy up",
        ),
        (
            WEIGHTS["completeness"] * score.completeness,
            "lands on a finished thought",
        ),
    ]
    fragments.sort(key=lambda f: -f[0])  # stable: ties keep original order
    line = f"{fragments[0][1]} and {fragments[1][1]}"
    return f"{line[:1].upper()}{line[1:]}."


def _make_tip(
    score: _WindowScore,
    silences: list[SilenceSpan],
    start: float,
    end: float,
) -> str:
    """One actionable line derived from the weakest component."""
    liabilities = [
        (
            WEIGHTS["hook"] * (1 - score.hook.score),
            "Punch up the first line. Open on a question or a bold claim so nobody scrolls past.",
        ),
        (
            WEIGHTS["density"] * (1 - score.density),
            "Tighten the wording. Cutting filler raises ideas-per-second, and that holds attention.",
        ),
        (
            WEIGHTS["energy"] * (1 - score.energy),
            "The delivery sits flat here. Pick a take with more vocal punch, or add cuts for pace.",
        ),
        (
            WEIGHTS["completeness"] * (1 - score.completeness),
            "It cuts off mid-thought. End on a finished sentence so the clip feels complete.",
        ),
        (
            WEIGHTS["coldOpen"] * score.cold_open,
            "This starts inside the intro. Skip the warm-up and open where the story begins.",
        ),
    ]
    pause = _longest_pause_in(silences, start, end)
    if pause is not None:
        pause_at, pause_length = pause
        liabilities.append(
            (
                WEIGHTS["pause"] * score.pause + (0.02 if pause_length >= 0.7 else 0.0),
                f"Trim the pause at {_format_clip_time(pause_at - start)}. Dead air is retention poison.",
            )
        )
    liabilities.sort(key=lambda l: -l[0])  # stable: ties keep original order
    return liabilities[0][1]


def _window_words(words: list[Word], start: float, end: float) -> list[Word]:
    """Words overlapping [start, end], re-based so 0 = clip start."""
    out: list[Word] = []
    for word in words:
        if word.end <= start or word.start >= end:
            continue
        out.append(
            Word(
                text=word.text,
                start=_round3(max(0.0, word.start - start)),
                end=_round3(max(0.0, min(end, word.end) - start)),
                speaker=word.speaker,
            )
        )
    return out


def _to_plan(
    candidate: _Candidate,
    index: int,
    transcript: Transcript,
    features: AudioFeatures,
) -> ClipPlan:
    start = candidate.start
    end = candidate.end
    score = candidate.score
    sentences = candidate.sentences
    return ClipPlan(
        id=f"clip-{index}-{_js_round(start * 1000)}",
        start=start,
        end=end,
        score=score.score,
        title=_make_title(_strongest_sentence(sentences)),
        hooks=_make_hooks(sentences),
        reason=_make_reason(score),
        tip=_make_tip(score, features.silences, start, end),
        words=_window_words(transcript.words, start, end),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _whole_video_candidate(
    sentences: list[Sentence],
    target_length: float,
    features: AudioFeatures,
    video_duration: float,
) -> _Candidate:
    """Fallback: one plan spanning the whole video, capped at 1.35 × target."""
    start = _snap_start(sentences[0].start, features.silences, video_duration)
    end = _snap_end(sentences[-1].end, features.silences, video_duration)
    end = _round3(min(end, start + MAX_LENGTH_RATIO * target_length))
    end = max(end, _round3(min(video_duration, start + 1)))
    in_window = [s for s in sentences if s.start < end]
    slice_ = in_window if in_window else sentences
    return _Candidate(
        sentences=slice_,
        start=start,
        end=end,
        score=_score_window(slice_, start, end, features),
    )


def plan_clips(
    transcript: Transcript,
    features: AudioFeatures,
    options: HighlightOptions,
) -> list[ClipPlan]:
    sentences = _build_sentences(transcript.words)
    if not sentences:
        return []

    target_length = max(5, options.target_length)
    count = int(_clamp(math.floor(options.count) or 1, 1, 3))
    # The decoded audio is ground truth for how much video exists — Whisper
    # timestamps can overrun it at chunk boundaries, and the renderer cannot
    # render past the end of the source.
    audio_duration = len(features.rms) * features.hop_seconds
    video_duration = audio_duration if audio_duration > 0 else sentences[-1].end

    speech_span = sentences[-1].end - sentences[0].start
    if speech_span < target_length:
        # Shorter than one clip: return a single whole-video plan.
        picked = [
            _whole_video_candidate(sentences, target_length, features, video_duration)
        ]
    else:
        picked = _select_top(
            _build_candidates(sentences, target_length, features, video_duration),
            count,
        )
        if not picked:
            picked = [
                _whole_video_candidate(
                    sentences, target_length, features, video_duration
                )
            ]

    return [
        _to_plan(candidate, index, transcript, features)
        for index, candidate in enumerate(picked)
    ]
