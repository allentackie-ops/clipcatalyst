// Virality Engine v0 — plans the best clip windows from a transcript + audio
// features. Pure and deterministic: same inputs always yield the same plans
// (no Date.now, no Math.random — the only "noise" is a seeded string hash).
//
// Pipeline: words → sentences → candidate windows (sentence-aligned, near the
// target length) → six-component score → greedy non-overlapping selection →
// silence/sentence snapping → human-facing title/hooks/reason/tip per plan.

import type {
  AudioFeatures,
  ClipPlan,
  HighlightOptions,
  Sentence,
  Transcript,
  TranscriptWord,
} from "./types";

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

const MAX_WORD_GAP_S = 0.8; // inter-word gap that forces a sentence break
const MAX_SENTENCE_WORDS = 25; // hard cap per sentence
const MIN_LENGTH_RATIO = 0.6; // accepted window: [0.6, 1.35] × targetLength
const MAX_LENGTH_RATIO = 1.35;
const MIN_START_GAP_S = 15; // selected clips must start ≥ 15 s apart
const COLD_OPEN_CUTOFF_S = 8; // windows starting earlier get penalized
const SNAP_RADIUS_S = 0.75; // how far we look for a silence edge to snap to
const LEAD_IN_S = 0.15; // breathing room added before the first word
const TAIL_S = 0.25; // breathing room added after the last word
const ENERGY_SPIKE_LEVEL = 0.85; // normalized RMS counting as a spike
const DENSITY_FULL_MARKS = 0.9; // unique content words/sec that maps to 1.0

/** Relative weight of each scoring component (penalties subtract). */
const WEIGHTS = {
  hook: 0.34,
  density: 0.22,
  energy: 0.2,
  completeness: 0.08,
  pause: 0.16,
  coldOpen: 0.1,
} as const;

// ---------------------------------------------------------------------------
// Lexicons
// ---------------------------------------------------------------------------

/** Small stopword/filler set used only for info-density (tokens ≥ 4 chars). */
const STOPWORDS = new Set([
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
]);

/** Hook lexicon as scored categories — each has its own weight and hit test. */
type HookCategory =
  | "question"
  | "intrigue"
  | "superlative"
  | "number"
  | "secondPerson"
  | "contrast";

const HOOK_WEIGHTS: Record<HookCategory, number> = {
  question: 0.9, // questions open a loop the viewer wants closed
  intrigue: 0.75, // secrets/mistakes/truths promise a payoff
  superlative: 0.65, // absolutes are bold claims that invite reaction
  number: 0.6, // specificity reads as credibility
  secondPerson: 0.55, // "you" makes it personal
  contrast: 0.5, // "but / here's the thing" signals a turn
};

const QUESTION_OPENERS = new Set([
  "what", "why", "how", "who", "when", "where", "which", "did", "do", "does",
  "is", "are", "was", "can", "could", "would", "should", "have", "has",
  "ever", "imagine", "guess",
]);

const SECOND_PERSON = new Set([
  "you", "your", "youre", "yours", "yourself", "youll", "youve", "youd",
]);

const SUPERLATIVES = new Set([
  "best", "worst", "never", "always", "nobody", "everyone", "everybody",
  "everything", "nothing", "biggest", "smallest", "greatest", "fastest",
  "easiest", "hardest", "most", "least", "only", "guaranteed", "impossible",
  "ultimate", "perfect", "ever",
]);

const INTRIGUE_WORDS = new Set([
  "secret", "secrets", "mistake", "mistakes", "truth", "wrong", "actually",
  "honestly", "surprised", "surprising", "surprise", "insane", "crazy",
  "shocking", "shocked", "hidden", "warning", "weird", "unbelievable",
  "hack", "hacks", "trick", "tricks", "lie", "lies", "lied", "exposed",
  "problem", "dangerous",
]);

const CONTRAST_OPENERS = new Set([
  "but", "instead", "however", "meanwhile", "except", "yet",
]);

const CONTRAST_PHRASES = [
  "here's the thing", "heres the thing", "the problem is", "the truth is",
  "what if", "turns out", "it turns out", "plot twist", "the catch is",
];

const FILLER_OPENERS = new Set([
  "so", "and", "um", "uh", "like", "well", "okay", "ok", "right", "yeah",
  "anyway", "also", "basically",
]);

const FILLER_PHRASES = ["you know", "i mean", "sort of", "kind of"];

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

function clamp01(value: number): number {
  return clamp(value, 0, 1);
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/** FNV-1a — a tiny deterministic string hash used only for score dithering. */
function hashString(input: string): number {
  let h = 2166136261;
  for (let i = 0; i < input.length; i++) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Lowercased alphanumeric tokens with punctuation and apostrophes removed. */
function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9']+/g, " ")
    .split(" ")
    .map((t) => t.replace(/'/g, ""))
    .filter((t) => t.length > 0);
}

const TERMINAL_RE = /[.?!]["')\]»”’]*$/;

function endsWithTerminal(text: string): boolean {
  return TERMINAL_RE.test(text.trim());
}

function formatClipTime(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Sentence building
// ---------------------------------------------------------------------------

function buildSentences(words: TranscriptWord[]): Sentence[] {
  const sentences: Sentence[] = [];
  let current: TranscriptWord[] = [];

  const flush = () => {
    if (current.length === 0) return;
    const text = current
      .map((w) => w.text)
      .join("")
      .replace(/\s+/g, " ")
      .trim();
    if (text.length > 0) {
      sentences.push({
        text,
        start: current[0].start,
        end: current[current.length - 1].end,
        words: current,
      });
    }
    current = [];
  };

  for (let i = 0; i < words.length; i++) {
    const word = words[i];
    current.push(word);
    const next = words[i + 1];
    const gap = next ? next.start - word.end : Number.POSITIVE_INFINITY;
    if (
      endsWithTerminal(word.text) ||
      gap > MAX_WORD_GAP_S ||
      current.length >= MAX_SENTENCE_WORDS
    ) {
      flush();
    }
  }
  flush();
  return sentences;
}

// ---------------------------------------------------------------------------
// Scoring components
// ---------------------------------------------------------------------------

type HookInfo = {
  /** 0..1 saturating combination of all category hits. */
  score: number;
  /** The category contributing most — drives the "reason" phrasing. */
  dominant: HookCategory | null;
};

function scoreHook(text: string): HookInfo {
  const tokens = tokenize(text);
  const lower = text.toLowerCase();
  const first = tokens[0];
  const hits: Partial<Record<HookCategory, number>> = {};

  if (lower.includes("?") || (first !== undefined && QUESTION_OPENERS.has(first))) {
    hits.question = 1;
  }

  let secondPerson = 0;
  let superlative = 0;
  let intrigue = 0;
  for (const t of tokens) {
    if (SECOND_PERSON.has(t)) secondPerson++;
    if (SUPERLATIVES.has(t)) superlative++;
    if (INTRIGUE_WORDS.has(t)) intrigue++;
  }
  if (secondPerson > 0) hits.secondPerson = Math.min(1, secondPerson / 2);
  if (superlative > 0) hits.superlative = Math.min(1, superlative / 2);
  if (intrigue > 0) hits.intrigue = Math.min(1, intrigue / 2);

  const numberMatches = lower.match(/\$\s*\d[\d,.]*|\d[\d,.]*\s*%|\b\d[\d,.]*\b/g);
  if (numberMatches && numberMatches.length > 0) {
    hits.number = Math.min(1, numberMatches.length / 2);
  }

  if (
    (first !== undefined && CONTRAST_OPENERS.has(first)) ||
    CONTRAST_PHRASES.some((p) => lower.includes(p))
  ) {
    hits.contrast = 1;
  }

  let sum = 0;
  let dominant: HookCategory | null = null;
  let dominantValue = 0;
  for (const key of Object.keys(HOOK_WEIGHTS) as HookCategory[]) {
    const hit = hits[key];
    if (hit === undefined) continue;
    const contribution = HOOK_WEIGHTS[key] * hit;
    sum += contribution;
    if (contribution > dominantValue) {
      dominantValue = contribution;
      dominant = key;
    }
  }
  // Saturating squash: one strong category ≈ 0.63, stacked categories → 0.9+.
  return { score: 1 - Math.exp(-1.1 * sum), dominant };
}

/** Unique content words (≥ 4 chars, not stopwords) per second, normalized. */
function infoDensity(sentences: Sentence[], durationSeconds: number): number {
  if (durationSeconds <= 0) return 0;
  const unique = new Set<string>();
  for (const sentence of sentences) {
    for (const token of tokenize(sentence.text)) {
      if (token.length >= 4 && !STOPWORDS.has(token)) unique.add(token);
    }
  }
  return clamp01(unique.size / durationSeconds / DENSITY_FULL_MARKS);
}

function energyOf(
  features: AudioFeatures,
  start: number,
  end: number
): { energy: number; spike: boolean } {
  const { rms, hopSeconds } = features;
  const from = Math.max(0, Math.floor(start / hopSeconds));
  const to = Math.min(rms.length, Math.ceil(end / hopSeconds));
  if (to <= from) return { energy: 0, spike: false };
  let sum = 0;
  let spike = false;
  for (let i = from; i < to; i++) {
    const v = rms[i];
    sum += v;
    if (v > ENERGY_SPIKE_LEVEL) spike = true;
  }
  return { energy: clamp01(sum / (to - from) + (spike ? 0.15 : 0)), spike };
}

function pauseFraction(
  silences: AudioFeatures["silences"],
  start: number,
  end: number
): number {
  const duration = end - start;
  if (duration <= 0) return 0;
  let covered = 0;
  for (const s of silences) {
    covered += Math.max(0, Math.min(s.end, end) - Math.max(s.start, start));
  }
  return clamp01(covered / duration);
}

function longestPauseIn(
  silences: AudioFeatures["silences"],
  start: number,
  end: number
): { at: number; length: number } | null {
  let best: { at: number; length: number } | null = null;
  for (const s of silences) {
    const from = Math.max(s.start, start);
    const to = Math.min(s.end, end);
    const length = to - from;
    if (length <= 0) continue;
    if (!best || length > best.length) best = { at: from, length };
  }
  return best;
}

type WindowScore = {
  hook: HookInfo;
  density: number;
  energy: number;
  spike: boolean;
  pause: number;
  completeness: number;
  coldOpen: number;
  raw: number;
  /** 0-100 display score. */
  score: number;
};

function scoreWindow(
  sentences: Sentence[],
  start: number,
  end: number,
  features: AudioFeatures
): WindowScore {
  const first = sentences[0];
  const last = sentences[sentences.length - 1];

  const hook = scoreHook(first.text);
  const density = infoDensity(sentences, end - start);
  const { energy, spike } = energyOf(features, start, end);
  const pause = pauseFraction(features.silences, start, end);
  const completeness = endsWithTerminal(last.text) ? 1 : 0;
  const coldOpen =
    start >= COLD_OPEN_CUTOFF_S ? 0 : (COLD_OPEN_CUTOFF_S - start) / COLD_OPEN_CUTOFF_S;

  const raw =
    WEIGHTS.hook * hook.score +
    WEIGHTS.density * density +
    WEIGHTS.energy * energy +
    WEIGHTS.completeness * completeness -
    WEIGHTS.pause * pause -
    WEIGHTS.coldOpen * coldOpen;

  return {
    hook,
    density,
    energy,
    spike,
    pause,
    completeness,
    coldOpen,
    raw,
    score: toDisplayScore(raw, `${Math.round(start * 1000)}|${first.text}`),
  };
}

/**
 * Map the raw weighted sum (≈ −0.26..0.84) onto 0-100 with spread: winners
 * land in the 70s-90s, weak windows in the 40s-60s, never 100. A ±1
 * hash-seeded dither breaks ties without breaking determinism.
 */
function toDisplayScore(raw: number, seed: string): number {
  const norm = clamp01((raw - 0.02) / 0.66);
  const base = 38 + 57 * Math.pow(norm, 0.9);
  const dither = (hashString(seed) % 3) - 1;
  return Math.round(clamp(base + dither, 35, 96));
}

// ---------------------------------------------------------------------------
// Snapping — align cut points with silence edges for clean ins and outs
// ---------------------------------------------------------------------------

function snapStart(
  rawStart: number,
  silences: AudioFeatures["silences"],
  videoDuration: number
): number {
  let edge = rawStart;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const s of silences) {
    const distance = Math.abs(s.end - rawStart);
    if (s.end <= rawStart + 0.2 && distance <= SNAP_RADIUS_S && distance < bestDistance) {
      bestDistance = distance;
      edge = s.end;
    }
  }
  return round3(clamp(edge - LEAD_IN_S, 0, videoDuration));
}

function snapEnd(
  rawEnd: number,
  silences: AudioFeatures["silences"],
  videoDuration: number
): number {
  let edge = rawEnd;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const s of silences) {
    const distance = Math.abs(s.start - rawEnd);
    if (s.start >= rawEnd - 0.2 && distance <= SNAP_RADIUS_S && distance < bestDistance) {
      bestDistance = distance;
      edge = s.start;
    }
  }
  return round3(clamp(edge + TAIL_S, 0, videoDuration));
}

// ---------------------------------------------------------------------------
// Candidate windows + selection
// ---------------------------------------------------------------------------

type Candidate = {
  sentences: Sentence[];
  /** Snapped, clamped bounds. */
  start: number;
  end: number;
  score: WindowScore;
};

function makeCandidate(
  slice: Sentence[],
  features: AudioFeatures,
  videoDuration: number
): Candidate {
  const start = snapStart(slice[0].start, features.silences, videoDuration);
  const end = Math.max(
    snapEnd(slice[slice.length - 1].end, features.silences, videoDuration),
    round3(Math.min(videoDuration, start + 1))
  );
  return { sentences: slice, start, end, score: scoreWindow(slice, start, end, features) };
}

function buildCandidates(
  sentences: Sentence[],
  targetLength: number,
  features: AudioFeatures,
  videoDuration: number
): Candidate[] {
  const minDuration = MIN_LENGTH_RATIO * targetLength;
  const maxDuration = MAX_LENGTH_RATIO * targetLength;
  const candidates: Candidate[] = [];

  for (let i = 0; i < sentences.length; i++) {
    // Extend whole sentences until we reach the target (or run out).
    let j = i;
    while (j + 1 < sentences.length && sentences[j].end - sentences[i].start < targetLength) {
      j++;
    }
    let duration = sentences[j].end - sentences[i].start;
    // If the last sentence overshot the cap, try backing off one sentence.
    if (duration > maxDuration && j > i) {
      const shorter = sentences[j - 1].end - sentences[i].start;
      if (shorter >= minDuration) {
        j -= 1;
        duration = shorter;
      }
    }
    if (duration < minDuration || duration > maxDuration) continue;
    // Windows starting at (or clamped into) the very end of the video can't
    // produce a real clip.
    if (sentences[i].start >= videoDuration - Math.max(3, minDuration / 2)) continue;
    const candidate = makeCandidate(sentences.slice(i, j + 1), features, videoDuration);
    if (candidate.end - candidate.start >= Math.min(minDuration, 5)) {
      candidates.push(candidate);
    }
  }
  return candidates;
}

/** Greedy: best score first; no overlaps; starts ≥ 15 s apart. */
function selectTop(candidates: Candidate[], count: number): Candidate[] {
  const ranked = [...candidates].sort(
    (a, b) => b.score.score - a.score.score || b.score.raw - a.score.raw || a.start - b.start
  );
  const picked: Candidate[] = [];
  for (const candidate of ranked) {
    if (picked.length >= count) break;
    const compatible = picked.every(
      (p) =>
        (candidate.end <= p.start || candidate.start >= p.end) &&
        Math.abs(candidate.start - p.start) >= MIN_START_GAP_S
    );
    if (compatible) picked.push(candidate);
  }
  return picked; // already sorted by score desc
}

// ---------------------------------------------------------------------------
// Human-facing plan fields
// ---------------------------------------------------------------------------

function stripFillerOpeners(text: string): string {
  let t = text.trim();
  for (;;) {
    const lower = t.toLowerCase();
    const phrase = FILLER_PHRASES.find(
      (p) => lower.startsWith(`${p} `) || lower.startsWith(`${p}, `)
    );
    if (phrase) {
      t = t.slice(phrase.length).replace(/^[\s,]+/, "");
      continue;
    }
    const match = /^([A-Za-z']+)[\s,]+/.exec(t);
    if (match && FILLER_OPENERS.has(match[1].toLowerCase())) {
      t = t.slice(match[0].length);
      continue;
    }
    break;
  }
  return t.trim();
}

function truncateAtWord(text: string, max: number): string {
  if (text.length <= max) return text;
  const slice = text.slice(0, max + 1);
  const lastSpace = slice.lastIndexOf(" ");
  return (lastSpace > 0 ? slice.slice(0, lastSpace) : text.slice(0, max)).trim();
}

const TRAILING_PUNCTUATION_RE = /[\s.,!?;:…"'“”‘’-]+$/;

function makeTitle(sentence: Sentence): string {
  let t = stripFillerOpeners(sentence.text);
  if (t.length === 0) t = sentence.text.trim();
  t = t.replace(TRAILING_PUNCTUATION_RE, "");
  const full = t;
  t = truncateAtWord(t, 48).replace(TRAILING_PUNCTUATION_RE, "");
  if (t.length === 0) t = "Untitled moment";
  const title = t.charAt(0).toUpperCase() + t.slice(1);
  return t.length < full.length ? `${title}…` : title;
}

/** Strongest sentence = best hook, with a mild prior toward fuller lines. */
function strongestSentence(sentences: Sentence[]): Sentence {
  let best = sentences[0];
  let bestStrength = -1;
  for (const sentence of sentences) {
    const strength =
      scoreHook(sentence.text).score + 0.12 * Math.min(1, sentence.words.length / 8);
    if (strength > bestStrength + 1e-9) {
      bestStrength = strength;
      best = sentence;
    }
  }
  return best;
}

function trimHook(text: string): string {
  const t = text.trim();
  if (t.length <= 80) return t;
  return `${truncateAtWord(t, 79).replace(/[\s.,;:-]+$/, "")}…`;
}

function makeHooks(sentences: Sentence[]): string[] {
  return sentences
    .map((sentence, order) => ({ sentence, order, hook: scoreHook(sentence.text).score }))
    .sort((a, b) => b.hook - a.hook || a.order - b.order)
    .slice(0, 5)
    .map(({ sentence }) => trimHook(sentence.text))
    .filter((t) => t.length > 0);
}

function hookFragment(hook: HookInfo): string {
  switch (hook.dominant) {
    case "question":
      return "opens on a question";
    case "secondPerson":
      return "talks straight to the viewer";
    case "number":
      return "leads with hard numbers";
    case "superlative":
      return "makes a bold, absolute claim";
    case "intrigue":
      return "teases a payoff you want to hear";
    case "contrast":
      return "opens on a sharp contrast";
    default:
      return "comes in with a strong first line";
  }
}

/** One concrete line naming the two strongest scoring components. */
function makeReason(score: WindowScore): string {
  const fragments = [
    { value: WEIGHTS.hook * score.hook.score, text: hookFragment(score.hook) },
    { value: WEIGHTS.density * score.density, text: "packs ideas in tight" },
    {
      value: WEIGHTS.energy * score.energy,
      text: score.spike ? "rides an energy spike" : "keeps the vocal energy up",
    },
    { value: WEIGHTS.completeness * score.completeness, text: "lands on a finished thought" },
  ].sort((a, b) => b.value - a.value);
  const line = `${fragments[0].text} and ${fragments[1].text}`;
  return `${line.charAt(0).toUpperCase()}${line.slice(1)}.`;
}

/** One actionable line derived from the weakest component. */
function makeTip(
  score: WindowScore,
  silences: AudioFeatures["silences"],
  start: number,
  end: number
): string {
  const liabilities = [
    {
      value: WEIGHTS.hook * (1 - score.hook.score),
      tip: "Punch up the first line — open on a question or a bold claim so nobody scrolls past.",
    },
    {
      value: WEIGHTS.density * (1 - score.density),
      tip: "Tighten the wording — cutting filler raises ideas-per-second, and that holds attention.",
    },
    {
      value: WEIGHTS.energy * (1 - score.energy),
      tip: "The delivery sits flat here — pick a take with more vocal punch, or add cuts for pace.",
    },
    {
      value: WEIGHTS.completeness * (1 - score.completeness),
      tip: "It cuts off mid-thought — end on a finished sentence so the clip feels complete.",
    },
    {
      value: WEIGHTS.coldOpen * score.coldOpen,
      tip: "This starts inside the intro — skip the warm-up and open where the story begins.",
    },
  ];
  const pause = longestPauseIn(silences, start, end);
  if (pause) {
    liabilities.push({
      value: WEIGHTS.pause * score.pause + (pause.length >= 0.7 ? 0.02 : 0),
      tip: `Trim the pause at ${formatClipTime(pause.at - start)} — dead air is retention poison.`,
    });
  }
  liabilities.sort((a, b) => b.value - a.value);
  return liabilities[0].tip;
}

/** Words overlapping [start, end], re-based so 0 = clip start. */
function windowWords(
  words: TranscriptWord[],
  start: number,
  end: number
): TranscriptWord[] {
  const out: TranscriptWord[] = [];
  for (const word of words) {
    if (word.end <= start || word.start >= end) continue;
    out.push({
      text: word.text,
      start: round3(Math.max(0, word.start - start)),
      end: round3(Math.max(0, Math.min(end, word.end) - start)),
    });
  }
  return out;
}

function toPlan(
  candidate: Candidate,
  index: number,
  transcript: Transcript,
  features: AudioFeatures
): ClipPlan {
  const { start, end, score, sentences } = candidate;
  return {
    id: `clip-${index}-${Math.round(start * 1000)}`,
    start,
    end,
    score: score.score,
    title: makeTitle(strongestSentence(sentences)),
    hooks: makeHooks(sentences),
    reason: makeReason(score),
    tip: makeTip(score, features.silences, start, end),
    words: windowWords(transcript.words, start, end),
  };
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/** Fallback: one plan spanning the whole video, capped at 1.35 × target. */
function wholeVideoCandidate(
  sentences: Sentence[],
  targetLength: number,
  features: AudioFeatures,
  videoDuration: number
): Candidate {
  const start = snapStart(sentences[0].start, features.silences, videoDuration);
  let end = snapEnd(sentences[sentences.length - 1].end, features.silences, videoDuration);
  end = round3(Math.min(end, start + MAX_LENGTH_RATIO * targetLength));
  end = Math.max(end, round3(Math.min(videoDuration, start + 1)));
  const inWindow = sentences.filter((s) => s.start < end);
  const slice = inWindow.length > 0 ? inWindow : sentences;
  return { sentences: slice, start, end, score: scoreWindow(slice, start, end, features) };
}

export function planClips(
  transcript: Transcript,
  features: AudioFeatures,
  options: HighlightOptions
): ClipPlan[] {
  const sentences = buildSentences(transcript.words);
  if (sentences.length === 0) return [];

  const targetLength = Math.max(5, options.targetLength);
  const count = clamp(Math.floor(options.count) || 1, 1, 3);
  // The decoded audio is ground truth for how much video exists — Whisper
  // timestamps can overrun it at chunk boundaries, and the renderer cannot
  // render past the end of the source.
  const audioDuration = features.rms.length * features.hopSeconds;
  const videoDuration =
    audioDuration > 0 ? audioDuration : sentences[sentences.length - 1].end;

  const speechSpan = sentences[sentences.length - 1].end - sentences[0].start;
  let picked: Candidate[];
  if (speechSpan < targetLength) {
    // Shorter than one clip: return a single whole-video plan.
    picked = [wholeVideoCandidate(sentences, targetLength, features, videoDuration)];
  } else {
    picked = selectTop(
      buildCandidates(sentences, targetLength, features, videoDuration),
      count
    );
    if (picked.length === 0) {
      picked = [wholeVideoCandidate(sentences, targetLength, features, videoDuration)];
    }
  }

  return picked.map((candidate, index) => toPlan(candidate, index, transcript, features));
}
