"""Speaker assignment: turns voice-embedding vectors into "who said which word".

Direct port of ``lib/studio/diarize.ts`` (the reference implementation). Like
croptrack, the split of labour is: feature extraction is per-engine (MFCC stats
in JS on the browser's PCM; numpy on the server), but every judgement lives in
this one algorithm and must behave identically in both engines -- segmentation,
clustering, the one-vs-many decision, smoothing, labeling.

The costliest mistake in this module is a FALSE SPLIT: painting a single host
as two alternating colors reads as "the app is broken", while missing a genuine
second voice merely loses a nicety. Every threshold below leans conservative
for that reason.

Pure and deterministic -- no randomness (agglomerative clustering, ordered
tie-breaks), no timers, no I/O, stdlib only. The TypeScript module is the
reference; this file follows it line for line, including JavaScript rounding
semantics (see ``_js_round``). Keep the two in step:
``api/tests/test_diarize.py`` cross-checks them by running both on identical
embedding fixtures, and when they disagree it is this file that moves.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


class _TimedWord(Protocol):
    """Anything with word timings -- ``types.Word`` qualifies."""

    start: float
    end: float


@dataclass
class SpeechSegment:
    start: float
    end: float
    word_idxs: list[int] = field(default_factory=list)
    """Indices into the transcript's word list."""


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: int


@dataclass(frozen=True)
class DiarizationResult:
    speaker_count: int
    """1..MAX_SPEAKERS."""
    word_speakers: list[int | None]
    """Per transcript-word speaker index; None where nothing was assigned."""
    turns: list[SpeakerTurn]
    """Merged, ordered speaking turns."""
    confidence: float
    """Cluster-separation quality 0..1 (0 when speaker_count is 1)."""


# --- Tunables ---------------------------------------------------------------
# Mirrored verbatim from diarize.ts -- do not re-tune on one side only.

SEGMENT_GAP_S = 0.6
"""Words further apart than this start a new speech segment."""
SEGMENT_MAX_S = 6.0
"""Split any segment that grows longer than this (embedding stays local)."""
SEGMENT_MIN_S = 0.6
"""Segments shorter than this carry too little voice to embed reliably."""
MAX_SPEAKERS = 4
"""Hard cap on distinct speakers (also the caption palette size)."""
CLUSTER_TAU = 0.35
"""Agglomerative merge threshold on cosine distance of embeddings."""
MIN_SEPARATION_RATIO = 2.8
"""After clustering, clusters must be at least this much farther apart than
they are wide, or we collapse to one speaker. This is the false-split guard:
one voice with varied prosody forms one wide cloud, not two distant ones.

Placed empirically in the gap between the two populations: the most
adversarial 2-way split of a single voice cloud tops out near 2.4 (measured
across jitter levels and seeds, worst case over ALL possible partitions),
while genuinely distinct voices stay above 3.7 even with very noisy
embeddings. 2.8 sits in the empty band with margin on both sides."""
MIN_SPEAKER_SPEECH_S = 3.0
"""A real speaker owns at least this much total speech."""
SMOOTH_MAX_S = 1.2
"""A segment this short sandwiched between one speaker joins that speaker."""

SPEAKER_COLORS = (
    "#a78bfa",  # violet -- speaker 0 / the existing single-speaker highlight
    "#fbbf24",  # amber
    "#38bdf8",  # sky
    "#fb7185",  # rose
)
"""Caption palette, index = speaker. Speaker 0 keeps the brand violet the
single-speaker path has always used, so one-voice clips look unchanged."""


def _assert_finite(v: float) -> float:
    return v if math.isfinite(v) else 0.0


def _js_round(x: float) -> float:
    """``Math.round`` semantics: nearest integer, ties toward +infinity."""
    if not math.isfinite(x):
        return x
    f = math.floor(x)
    if x - f < 0.5:
        return float(f)
    return float(f + 1)


# --- Segmentation -----------------------------------------------------------


def build_speech_segments(words: Sequence[_TimedWord]) -> list[SpeechSegment]:
    """Group transcript words into speech segments an embedder can average over."""
    segments: list[SpeechSegment] = []
    current: list[int] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        start = words[current[0]].start
        end = words[current[-1]].end
        if end - start >= SEGMENT_MIN_S:
            segments.append(SpeechSegment(start=start, end=end, word_idxs=current))
        elif segments:
            # Too short to embed alone -- fold into the previous segment so its
            # words still get a speaker.
            prev = segments[-1]
            prev.word_idxs = prev.word_idxs + current
            prev.end = end
        else:
            segments.append(SpeechSegment(start=start, end=end, word_idxs=current))
        current = []

    for i, w in enumerate(words):
        if not (math.isfinite(w.start) and math.isfinite(w.end)):
            continue
        if current:
            seg_start = words[current[0]].start
            prev_end = words[current[-1]].end
            if w.start - prev_end > SEGMENT_GAP_S or w.end - seg_start > SEGMENT_MAX_S:
                flush()
        current.append(i)
    flush()
    return segments


# --- Clustering -------------------------------------------------------------


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(min(len(a), len(b))):
        dot += a[i] * b[i]
        na += a[i] * a[i]
        nb += b[i] * b[i]
    if na == 0 or nb == 0:
        return 1.0
    return _assert_finite(1 - dot / math.sqrt(na * nb))


@dataclass
class _Cluster:
    members: list[int]


def _linkage(a: _Cluster, b: _Cluster, dist) -> float:
    """Mean pairwise distance between two clusters (average linkage)."""
    total = 0.0
    count = 0
    for i in a.members:
        for j in b.members:
            total += dist(i, j)
            count += 1
    return 1.0 if count == 0 else total / count


def _agglomerate(count: int, dist, tau: float, max_clusters: int) -> list[_Cluster]:
    """Deterministic average-linkage agglomerative clustering.

    Merges the closest pair (ties: lowest first-member index) until the closest
    pair is farther than tau, then keeps merging only if over the speaker cap.
    """
    clusters = [_Cluster(members=[i]) for i in range(count)]

    while True:
        if len(clusters) <= 1:
            break
        best_a = -1
        best_b = -1
        best_d = math.inf
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = _linkage(clusters[a], clusters[b], dist)
                if d < best_d - 1e-12:
                    best_d = d
                    best_a = a
                    best_b = b
        must_merge = len(clusters) > max_clusters
        if not must_merge and best_d > tau:
            break
        merged = _Cluster(members=sorted(clusters[best_a].members + clusters[best_b].members))
        clusters = [c for i, c in enumerate(clusters) if i != best_a and i != best_b]
        clusters.append(merged)
        # Keep order deterministic: by lowest member index.
        clusters.sort(key=lambda c: c.members[0])
    return clusters


# --- Main entry -------------------------------------------------------------


def assign_speakers(
    words: Sequence[_TimedWord],
    segments: Sequence[SpeechSegment],
    embeddings: Sequence[Sequence[float]],
) -> DiarizationResult:
    """Assign speakers given speech segments and one embedding per segment.

    ``embeddings[i]`` belongs to ``segments[i]``; engines may pass an empty
    vector for segments they could not embed (those inherit a neighbour's
    speaker).
    """

    def none() -> DiarizationResult:
        return DiarizationResult(
            speaker_count=1,
            word_speakers=[0] * len(words),
            turns=(
                [SpeakerTurn(start=words[0].start, end=words[-1].end, speaker=0)]
                if len(words) > 0
                else []
            ),
            confidence=0.0,
        )

    if len(segments) == 0 or len(embeddings) != len(segments):
        return none()

    # Only segments with a usable embedding participate in clustering.
    usable: list[int] = []
    for i in range(len(segments)):
        e = embeddings[i]
        if e is not None and len(e) > 0 and any(v != 0 for v in e):
            usable.append(i)
    if len(usable) < 2:
        return none()

    dist_cache: dict[tuple[int, int], float] = {}

    def dist(ui: int, uj: int) -> float:
        key = (ui, uj) if ui < uj else (uj, ui)
        d = dist_cache.get(key)
        if d is None:
            d = _cosine_distance(embeddings[usable[ui]], embeddings[usable[uj]])
            dist_cache[key] = d
        return d

    clusters = _agglomerate(len(usable), dist, CLUSTER_TAU, MAX_SPEAKERS)

    # Absorb clusters with too little total speech into their nearest cluster --
    # a real speaker talks for more than a stray breath.
    while True:
        if len(clusters) <= 1:
            break
        speech = [
            sum(segments[usable[ui]].end - segments[usable[ui]].start for ui in c.members)
            for c in clusters
        ]
        tiny = next((i for i, s in enumerate(speech) if s < MIN_SPEAKER_SPEECH_S), -1)
        if tiny == -1:
            break
        nearest = -1
        nearest_d = math.inf
        for i in range(len(clusters)):
            if i == tiny:
                continue
            d = _linkage(clusters[tiny], clusters[i], dist)
            if d < nearest_d:
                nearest_d = d
                nearest = i
        clusters[nearest].members = sorted(clusters[nearest].members + clusters[tiny].members)
        del clusters[tiny]

    # The false-split guard: demand clear separation relative to cluster width.
    # Applied iteratively on the CLOSEST pair: carving one voice's cloud into
    # three shards can leave the overall averages looking separated while the
    # two nearest shards clearly are not -- merge those first and re-judge.
    confidence = 0.0
    while len(clusters) > 1:
        intra = 0.0
        intra_count = 0
        for c in clusters:
            for a in range(len(c.members)):
                for b in range(a + 1, len(c.members)):
                    intra += dist(c.members[a], c.members[b])
                    intra_count += 1
        width = max(intra / intra_count if intra_count > 0 else 0.05, 0.05)

        closest_a = 0
        closest_b = 1
        closest_d = math.inf
        inter_sum = 0.0
        inter_count = 0
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = _linkage(clusters[a], clusters[b], dist)
                inter_sum += d
                inter_count += 1
                if d < closest_d - 1e-12:
                    closest_d = d
                    closest_a = a
                    closest_b = b

        if closest_d / width < MIN_SEPARATION_RATIO:
            merged = _Cluster(
                members=sorted(clusters[closest_a].members + clusters[closest_b].members)
            )
            clusters = [c for i, c in enumerate(clusters) if i != closest_a and i != closest_b]
            clusters.append(merged)
            clusters.sort(key=lambda c: c.members[0])
            continue
        inter_mean = inter_sum / inter_count
        confidence = max(0.0, min(1.0, 1 - width / inter_mean))
        break
    if len(clusters) <= 1:
        confidence = 0.0

    # Label speakers by total speech time (0 = talks most), ties by first start.
    stats = []
    for idx, c in enumerate(clusters):
        speech_total = 0.0
        first = math.inf
        for ui in c.members:
            seg = segments[usable[ui]]
            speech_total += seg.end - seg.start
            first = min(first, seg.start)
        stats.append((idx, speech_total, first))
    stats.sort(key=lambda s: (-s[1], s[2]))
    label = {idx: rank for rank, (idx, _, _) in enumerate(stats)}

    seg_speaker: list[int | None] = [None] * len(segments)
    for ci, c in enumerate(clusters):
        for ui in c.members:
            seg_speaker[usable[ui]] = label[ci]

    # Segments without embeddings inherit the nearest labelled neighbour.
    for i in range(len(segments)):
        if seg_speaker[i] is not None:
            continue
        best: int | None = None
        best_gap = math.inf
        for j in range(len(segments)):
            if seg_speaker[j] is None:
                continue
            gap = abs(segments[j].start - segments[i].start)
            if gap < best_gap:
                best_gap = gap
                best = seg_speaker[j]
        seg_speaker[i] = best if best is not None else 0

    # Temporal smoothing: a blip between two stretches of one speaker joins it.
    for i in range(1, len(segments) - 1):
        dur_i = segments[i].end - segments[i].start
        if (
            dur_i <= SMOOTH_MAX_S
            and seg_speaker[i - 1] == seg_speaker[i + 1]
            and seg_speaker[i] != seg_speaker[i - 1]
        ):
            seg_speaker[i] = seg_speaker[i - 1]

    speaker_count = len(clusters)
    if speaker_count <= 1:
        return none()

    word_speakers: list[int | None] = [None] * len(words)
    for i in range(len(segments)):
        for wi in segments[i].word_idxs:
            word_speakers[wi] = seg_speaker[i]
    # Words that fell outside every segment inherit the previous word's speaker.
    for i in range(len(word_speakers)):
        if word_speakers[i] is None:
            word_speakers[i] = word_speakers[i - 1] if i > 0 else 0

    turns: list[SpeakerTurn] = []
    for i in range(len(segments)):
        sp = seg_speaker[i] if seg_speaker[i] is not None else 0
        if turns and turns[-1].speaker == sp:
            turns[-1] = SpeakerTurn(start=turns[-1].start, end=segments[i].end, speaker=sp)
        else:
            turns.append(SpeakerTurn(start=segments[i].start, end=segments[i].end, speaker=sp))

    return DiarizationResult(
        speaker_count=speaker_count,
        word_speakers=word_speakers,
        turns=turns,
        confidence=_js_round(confidence * 1000) / 1000,
    )
