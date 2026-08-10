// Speaker assignment: turns voice-embedding vectors into "who said which word".
//
// Like croptrack, the split of labour is: feature extraction is per-engine
// (MFCC stats in JS on the browser's PCM; numpy on the server), but every
// judgement lives here and must behave identically in both engines —
// segmentation, clustering, the one-vs-many decision, smoothing, labeling.
// `diarize.py` is a direct port; keep the two in step.
//
// The costliest mistake in this module is a FALSE SPLIT: painting a single
// host as two alternating colors reads as "the app is broken", while missing
// a genuine second voice merely loses a nicety. Every threshold below leans
// conservative for that reason.
//
// Pure and deterministic — no randomness (agglomerative clustering, ordered
// tie-breaks), no timers, no DOM.

import type { TranscriptWord } from "./types";

export type SpeechSegment = {
  start: number;
  end: number;
  /** Indices into the transcript's word array. */
  wordIdxs: number[];
};

export type SpeakerTurn = { start: number; end: number; speaker: number };

export type DiarizationResult = {
  /** 1..MAX_SPEAKERS. */
  speakerCount: number;
  /** Per transcript-word speaker index; undefined where nothing was assigned. */
  wordSpeakers: (number | undefined)[];
  /** Merged, ordered speaking turns. */
  turns: SpeakerTurn[];
  /** Cluster-separation quality 0..1 (0 when speakerCount is 1). */
  confidence: number;
};

// --- Tunables --------------------------------------------------------------

/** Words further apart than this start a new speech segment. */
const SEGMENT_GAP_S = 0.6;
/** Split any segment that grows longer than this (embedding stays local). */
const SEGMENT_MAX_S = 6.0;
/** Segments shorter than this carry too little voice to embed reliably. */
const SEGMENT_MIN_S = 0.6;
/** Hard cap on distinct speakers (also the caption palette size). */
export const MAX_SPEAKERS = 4;
/** Agglomerative merge threshold on cosine distance of embeddings. */
const CLUSTER_TAU = 0.35;
/**
 * After clustering, clusters must be at least this much farther apart than
 * they are wide, or we collapse to one speaker. This is the false-split guard:
 * one voice with varied prosody forms one wide cloud, not two distant ones.
 *
 * Placed empirically in the gap between the two populations: the most
 * adversarial 2-way split of a single voice cloud tops out near 2.4 (measured
 * across jitter levels and seeds, worst case over ALL possible partitions),
 * while genuinely distinct voices stay above 3.7 even with very noisy
 * embeddings. 2.8 sits in the empty band with margin on both sides.
 */
const MIN_SEPARATION_RATIO = 2.8;
/** A real speaker owns at least this much total speech. */
const MIN_SPEAKER_SPEECH_S = 3.0;
/** A segment this short sandwiched between one speaker joins that speaker. */
const SMOOTH_MAX_S = 1.2;

/**
 * Caption palette, index = speaker. Speaker 0 keeps the brand violet the
 * single-speaker path has always used, so one-voice clips look unchanged.
 */
export const SPEAKER_COLORS = [
  "#a78bfa", // violet — speaker 0 / the existing single-speaker highlight
  "#fbbf24", // amber
  "#38bdf8", // sky
  "#fb7185", // rose
] as const;

function assertFinite(v: number): number {
  return Number.isFinite(v) ? v : 0;
}

// --- Segmentation ----------------------------------------------------------

/** Group transcript words into speech segments an embedder can average over. */
export function buildSpeechSegments(words: TranscriptWord[]): SpeechSegment[] {
  const segments: SpeechSegment[] = [];
  let current: number[] = [];

  const flush = () => {
    if (current.length === 0) return;
    const start = words[current[0]].start;
    const end = words[current[current.length - 1]].end;
    if (end - start >= SEGMENT_MIN_S) {
      segments.push({ start, end, wordIdxs: current });
    } else if (segments.length > 0) {
      // Too short to embed alone — fold into the previous segment so its
      // words still get a speaker.
      const prev = segments[segments.length - 1];
      prev.wordIdxs = prev.wordIdxs.concat(current);
      prev.end = end;
    } else {
      segments.push({ start, end, wordIdxs: current });
    }
    current = [];
  };

  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (!Number.isFinite(w.start) || !Number.isFinite(w.end)) continue;
    if (current.length > 0) {
      const segStart = words[current[0]].start;
      const prevEnd = words[current[current.length - 1]].end;
      if (w.start - prevEnd > SEGMENT_GAP_S || w.end - segStart > SEGMENT_MAX_S) {
        flush();
      }
    }
    current.push(i);
  }
  flush();
  return segments;
}

// --- Clustering ------------------------------------------------------------

function cosineDistance(a: readonly number[], b: readonly number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  if (na === 0 || nb === 0) return 1;
  return assertFinite(1 - dot / Math.sqrt(na * nb));
}

type Cluster = { members: number[] };

/** Mean pairwise distance between two clusters (average linkage). */
function linkage(
  a: Cluster,
  b: Cluster,
  dist: (i: number, j: number) => number
): number {
  let sum = 0;
  let count = 0;
  for (const i of a.members) {
    for (const j of b.members) {
      sum += dist(i, j);
      count++;
    }
  }
  return count === 0 ? 1 : sum / count;
}

/**
 * Deterministic average-linkage agglomerative clustering.
 * Merges the closest pair (ties: lowest first-member index) until the closest
 * pair is farther than tau, then keeps merging only if over the speaker cap.
 */
function agglomerate(
  count: number,
  dist: (i: number, j: number) => number,
  tau: number,
  maxClusters: number
): Cluster[] {
  let clusters: Cluster[] = [];
  for (let i = 0; i < count; i++) clusters.push({ members: [i] });

  for (;;) {
    if (clusters.length <= 1) break;
    let bestA = -1;
    let bestB = -1;
    let bestD = Infinity;
    for (let a = 0; a < clusters.length; a++) {
      for (let b = a + 1; b < clusters.length; b++) {
        const d = linkage(clusters[a], clusters[b], dist);
        if (d < bestD - 1e-12) {
          bestD = d;
          bestA = a;
          bestB = b;
        }
      }
    }
    const mustMerge = clusters.length > maxClusters;
    if (!mustMerge && bestD > tau) break;
    const merged: Cluster = {
      members: clusters[bestA].members.concat(clusters[bestB].members).sort((x, y) => x - y),
    };
    clusters = clusters.filter((_, i) => i !== bestA && i !== bestB);
    clusters.push(merged);
    // Keep order deterministic: by lowest member index.
    clusters.sort((a, b) => a.members[0] - b.members[0]);
  }
  return clusters;
}

// --- Main entry ------------------------------------------------------------

/**
 * Assign speakers given speech segments and one embedding per segment.
 *
 * `embeddings[i]` belongs to `segments[i]`; engines may pass an empty vector
 * for segments they could not embed (those inherit a neighbour's speaker).
 */
export function assignSpeakers(
  words: TranscriptWord[],
  segments: SpeechSegment[],
  embeddings: readonly (readonly number[])[]
): DiarizationResult {
  const none: DiarizationResult = {
    speakerCount: 1,
    wordSpeakers: words.map(() => (words.length > 0 ? 0 : undefined)),
    turns:
      words.length > 0
        ? [{ start: words[0].start, end: words[words.length - 1].end, speaker: 0 }]
        : [],
    confidence: 0,
  };
  if (segments.length === 0 || embeddings.length !== segments.length) return none;

  // Only segments with a usable embedding participate in clustering.
  const usable: number[] = [];
  for (let i = 0; i < segments.length; i++) {
    const e = embeddings[i];
    if (e && e.length > 0 && e.some((v) => v !== 0)) usable.push(i);
  }
  if (usable.length < 2) return none;

  const distCache = new Map<number, number>();
  const dist = (ui: number, uj: number): number => {
    const key = ui < uj ? ui * usable.length + uj : uj * usable.length + ui;
    let d = distCache.get(key);
    if (d === undefined) {
      d = cosineDistance(embeddings[usable[ui]], embeddings[usable[uj]]);
      distCache.set(key, d);
    }
    return d;
  };

  let clusters = agglomerate(usable.length, dist, CLUSTER_TAU, MAX_SPEAKERS);

  // Absorb clusters with too little total speech into their nearest cluster —
  // a real speaker talks for more than a stray breath.
  for (;;) {
    if (clusters.length <= 1) break;
    const speech = clusters.map((c) =>
      c.members.reduce((s, ui) => {
        const seg = segments[usable[ui]];
        return s + (seg.end - seg.start);
      }, 0)
    );
    const tiny = speech.findIndex((s) => s < MIN_SPEAKER_SPEECH_S);
    if (tiny === -1) break;
    let nearest = -1;
    let nearestD = Infinity;
    for (let i = 0; i < clusters.length; i++) {
      if (i === tiny) continue;
      const d = linkage(clusters[tiny], clusters[i], dist);
      if (d < nearestD) {
        nearestD = d;
        nearest = i;
      }
    }
    clusters[nearest].members = clusters[nearest].members
      .concat(clusters[tiny].members)
      .sort((x, y) => x - y);
    clusters.splice(tiny, 1);
  }

  // The false-split guard: demand clear separation relative to cluster width.
  // Applied iteratively on the CLOSEST pair: carving one voice's cloud into
  // three shards can leave the overall averages looking separated while the
  // two nearest shards clearly are not — merge those first and re-judge.
  let confidence = 0;
  while (clusters.length > 1) {
    let intra = 0;
    let intraCount = 0;
    for (const c of clusters) {
      for (let a = 0; a < c.members.length; a++) {
        for (let b = a + 1; b < c.members.length; b++) {
          intra += dist(c.members[a], c.members[b]);
          intraCount++;
        }
      }
    }
    const width = Math.max(intraCount > 0 ? intra / intraCount : 0.05, 0.05);

    let closestA = 0;
    let closestB = 1;
    let closestD = Infinity;
    let interSum = 0;
    let interCount = 0;
    for (let a = 0; a < clusters.length; a++) {
      for (let b = a + 1; b < clusters.length; b++) {
        const d = linkage(clusters[a], clusters[b], dist);
        interSum += d;
        interCount++;
        if (d < closestD - 1e-12) {
          closestD = d;
          closestA = a;
          closestB = b;
        }
      }
    }

    if (closestD / width < MIN_SEPARATION_RATIO) {
      const merged: Cluster = {
        members: clusters[closestA].members
          .concat(clusters[closestB].members)
          .sort((x, y) => x - y),
      };
      clusters = clusters.filter((_, i) => i !== closestA && i !== closestB);
      clusters.push(merged);
      clusters.sort((a, b) => a.members[0] - b.members[0]);
      continue;
    }
    const interMean = interSum / interCount;
    confidence = Math.max(0, Math.min(1, 1 - width / interMean));
    break;
  }
  if (clusters.length <= 1) confidence = 0;

  // Label speakers by total speech time (0 = talks most), ties by first start.
  const stats = clusters.map((c, idx) => {
    let speech = 0;
    let first = Infinity;
    for (const ui of c.members) {
      const seg = segments[usable[ui]];
      speech += seg.end - seg.start;
      first = Math.min(first, seg.start);
    }
    return { idx, speech, first };
  });
  stats.sort((a, b) => b.speech - a.speech || a.first - b.first);
  const label = new Map<number, number>();
  stats.forEach((s, rank) => label.set(s.idx, rank));

  const segSpeaker = new Array<number | undefined>(segments.length).fill(undefined);
  clusters.forEach((c, ci) => {
    for (const ui of c.members) segSpeaker[usable[ui]] = label.get(ci);
  });

  // Segments without embeddings inherit the nearest labelled neighbour.
  for (let i = 0; i < segments.length; i++) {
    if (segSpeaker[i] !== undefined) continue;
    let best: number | undefined;
    let bestGap = Infinity;
    for (let j = 0; j < segments.length; j++) {
      if (segSpeaker[j] === undefined) continue;
      const gap = Math.abs(segments[j].start - segments[i].start);
      if (gap < bestGap) {
        bestGap = gap;
        best = segSpeaker[j];
      }
    }
    segSpeaker[i] = best ?? 0;
  }

  // Temporal smoothing: a blip between two stretches of one speaker joins it.
  for (let i = 1; i < segments.length - 1; i++) {
    const durI = segments[i].end - segments[i].start;
    if (
      durI <= SMOOTH_MAX_S &&
      segSpeaker[i - 1] === segSpeaker[i + 1] &&
      segSpeaker[i] !== segSpeaker[i - 1]
    ) {
      segSpeaker[i] = segSpeaker[i - 1];
    }
  }

  const speakerCount = clusters.length;
  if (speakerCount <= 1) return { ...none, confidence: 0 };

  const wordSpeakers = new Array<number | undefined>(words.length).fill(undefined);
  for (let i = 0; i < segments.length; i++) {
    for (const wi of segments[i].wordIdxs) wordSpeakers[wi] = segSpeaker[i];
  }
  // Words that fell outside every segment inherit the previous word's speaker.
  for (let i = 0; i < wordSpeakers.length; i++) {
    if (wordSpeakers[i] === undefined) wordSpeakers[i] = i > 0 ? wordSpeakers[i - 1] : 0;
  }

  const turns: SpeakerTurn[] = [];
  for (let i = 0; i < segments.length; i++) {
    const sp = segSpeaker[i] ?? 0;
    const last = turns[turns.length - 1];
    if (last && last.speaker === sp) {
      last.end = segments[i].end;
    } else {
      turns.push({ start: segments[i].start, end: segments[i].end, speaker: sp });
    }
  }

  return {
    speakerCount,
    wordSpeakers,
    turns,
    confidence: Math.round(confidence * 1000) / 1000,
  };
}
