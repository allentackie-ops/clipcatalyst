// Behavioural tests for the browser speaker-feature extractor: synthetic PCM
// in pure JS (no ffmpeg) through frames → segment embeddings → the shared
// assignSpeakers core. Mirrors the discrimination properties the Python
// engine proves on its side, including the two adversarial-review fixtures:
// one voice shifting register (laugh/shout) must stay ONE speaker, and two
// similar same-mic voices merge by design (the documented conservative
// direction).
import {
  computeMfccFrames,
  segmentEmbeddingsFromFrames,
} from "../.croptrack-build/speakerfeats.js";
import {
  assignSpeakers,
  buildSpeechSegments,
} from "../.croptrack-build/diarize.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};

// --- Synthetic voices (deterministic sample loops, 16 kHz) ------------------

const SR = 16000;
const TURN_S = 4;
const PAUSE_S = 1;
const AMP = 0.35;

/**
 * Voice A — the SPEAKERS.md recipe voice: sawtooth ~110 Hz through a ~900 Hz
 * lowpass (two cascaded one-poles ≈ ffmpeg's 2nd-order `lowpass=f=900`). The
 * vocal-tract tilt matters: an UNFILTERED 110 Hz comb is spectrally flat over
 * 300–3000 Hz and would (rightly) trip the speech-likeness gate.
 */
function voiceA(out, offset, seconds) {
  const n = Math.round(seconds * SR);
  const period = SR / 110;
  const a = Math.exp((-2 * Math.PI * 900) / SR);
  let y1 = 0, y2 = 0;
  for (let i = 0; i < n; i++) {
    const phase = (i % period) / period;
    const s = AMP * (2 * phase - 1);
    y1 = (1 - a) * s + a * y1;
    y2 = (1 - a) * y1 + a * y2;
    out[offset + i] = y2;
  }
}

/** Voice B — square ~280 Hz through a ~400 Hz one-pole highpass (recipe). */
function voiceB(out, offset, seconds) {
  const n = Math.round(seconds * SR);
  const period = SR / 280;
  const a = Math.exp((-2 * Math.PI * 400) / SR);
  let lp = 0;
  for (let i = 0; i < n; i++) {
    const phase = (i % period) / period;
    const s = phase < 0.5 ? AMP : -AMP;
    lp = (1 - a) * s + a * lp;
    out[offset + i] = s - lp;
  }
}

/**
 * The SAME person shifting register — a laugh/shout burst: raised-F0
 * harmonics swamped by broadband noise, pre-emphasized for a brighter tilt.
 * Seeded 32-bit LCG so every run builds the identical burst (determinism).
 */
function excited(out, offset, seconds) {
  const n = Math.round(seconds * SR);
  const period = SR / 160;
  let seed = 0xbeef1234 >>> 0;
  let prev = 0;
  for (let i = 0; i < n; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const noise = (seed / 4294967296) * 2 - 1;
    const phase = (i % period) / period;
    const raw = 0.28 * (2 * phase - 1) + 0.22 * noise;
    out[offset + i] = raw - 0.7 * prev; // pre-emphasis: brighter tilt
    prev = raw;
  }
}

/**
 * Same excitation (110 Hz harmonic series, 1/k glottal rolloff), vocal-tract
 * formants at (800, 1200)·scale. Two of these ~8% apart imitate the axis real
 * same-mic voices actually differ on — and measure only ~0.01 cosine apart in
 * mean+std MFCC space, far under CLUSTER_TAU.
 */
function formantVoice(formantScale) {
  const F0 = 110;
  const partials = [];
  let peak = 0;
  for (let k = 1; k * F0 < 3200; k++) {
    const f = k * F0;
    let g = 0;
    for (const [fc, bw] of [[800 * formantScale, 120], [1200 * formantScale, 150]]) {
      g += 1 / (1 + ((f - fc) / bw) ** 2);
    }
    g /= k;
    partials.push([(2 * Math.PI * f) / SR, g]);
    peak += g;
  }
  return (out, offset, seconds) => {
    const n = Math.round(seconds * SR);
    for (let i = 0; i < n; i++) {
      let s = 0;
      for (const [w, g] of partials) s += g * Math.sin(w * i);
      out[offset + i] = (0.5 * s) / peak;
    }
  };
}

/**
 * Alternating turns with 1 s silences between them, plus transcript words
 * matching the turns (10 words per 4 s turn).
 */
function conversation(voices) {
  const span = TURN_S + PAUSE_S;
  const pcm = new Float32Array(Math.round(voices.length * span * SR));
  const words = [];
  for (let t = 0; t < voices.length; t++) {
    const turnStart = t * span;
    voices[t](pcm, Math.round(turnStart * SR), TURN_S);
    for (let w = 0; w < 10; w++) {
      const start = turnStart + w * 0.4;
      words.push({ text: ` w${words.length}`, start, end: start + 0.35 });
    }
  }
  return { pcm, words };
}

async function diarizeConversation(voices) {
  const { pcm, words } = conversation(voices);
  const mfcc = await computeMfccFrames(pcm);
  const segments = buildSpeechSegments(words);
  const embeddings = segmentEmbeddingsFromFrames(
    mfcc.frames, mfcc.dims, mfcc.hopSeconds, segments
  );
  return { result: assignSpeakers(words, segments, embeddings), embeddings, segments, mfcc };
}

const cosineDist = (a, b) => {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i];
  }
  return 1 - dot / Math.sqrt(na * nb);
};

// 1. Two distinct voices alternating → two speakers with alternating turns.
//    (The speech-likeness gate must NOT kill true detection.)
{
  const { result: r, embeddings } = await diarizeConversation([
    voiceA, voiceB, voiceA, voiceB, voiceA, voiceB,
  ]);
  ok("two voices -> speakerCount 2", r.speakerCount === 2, `got ${r.speakerCount}`);
  ok(
    "turns alternate",
    r.turns.length >= 4 &&
      r.turns.every((t, i, a) => i === 0 || t.speaker !== a[i - 1].speaker),
    `turns=${JSON.stringify(r.turns.map((t) => t.speaker))}`
  );
  ok(
    "first and second turns get different labels",
    r.wordSpeakers[0] !== r.wordSpeakers[10],
    `${r.wordSpeakers[0]} vs ${r.wordSpeakers[10]}`
  );
  ok("every word has a speaker", r.wordSpeakers.every((s) => s !== undefined));
  // Embedding-level discrimination: same-voice pairs closer than cross-voice.
  const within = cosineDist(embeddings[0], embeddings[2]);
  const across = cosineDist(embeddings[0], embeddings[1]);
  ok(
    "same voice closer than cross voice",
    within < across,
    `within=${within.toFixed(4)} across=${across.toFixed(4)}`
  );
}

// 2. One voice all the way through → one speaker (false-split guard holds).
{
  const { result: r } = await diarizeConversation([
    voiceA, voiceA, voiceA, voiceA, voiceA, voiceA,
  ]);
  ok("one voice -> speakerCount 1", r.speakerCount === 1, `got ${r.speakerCount}`);
  ok("one voice -> every word speaker 0", r.wordSpeakers.every((s) => s === 0));
}

// 3. Silence-only segment → empty embedding; a voiced one stays usable.
{
  const pcm = new Float32Array(6 * SR); // silence...
  voiceA(pcm, 4 * SR, 2); // ...except the last 2 s
  const mfcc = await computeMfccFrames(pcm);
  const segs = [
    { start: 0.5, end: 3.0, wordIdxs: [0] }, // pure silence
    { start: 4.1, end: 5.9, wordIdxs: [1] }, // voiced
  ];
  const embs = segmentEmbeddingsFromFrames(mfcc.frames, mfcc.dims, mfcc.hopSeconds, segs);
  ok("silence-only segment -> []", embs[0].length === 0, `got ${embs[0].length} dims`);
  ok("voiced segment -> 24-dim embedding", embs[1].length === 24, `got ${embs[1].length}`);
  const norm = Math.hypot(...embs[1]);
  ok("embedding is L2-normalized", Math.abs(norm - 1) < 1e-6, `norm=${norm}`);
}

// 4. Degenerate inputs never throw: short PCM, out-of-range segments.
{
  const tiny = await computeMfccFrames(new Float32Array(100)); // < one frame
  ok("sub-frame PCM -> zero frames", tiny.frames.length === 0);
  const embs = segmentEmbeddingsFromFrames(tiny.frames, tiny.dims, tiny.hopSeconds, [
    { start: 0, end: 1, wordIdxs: [0] },
  ]);
  ok("no frames -> empty embedding", embs.length === 1 && embs[0].length === 0);
  const some = await computeMfccFrames(new Float32Array(SR));
  const beyond = segmentEmbeddingsFromFrames(some.frames, some.dims, some.hopSeconds, [
    { start: 500, end: 504, wordIdxs: [0] }, // far past the end of the audio
  ]);
  ok("out-of-range segment -> empty embedding", beyond[0].length === 0);
}

// 5. Determinism: identical PCM → identical embeddings and assignment.
{
  const a = await diarizeConversation([voiceA, voiceB, voiceA, voiceB]);
  const b = await diarizeConversation([voiceA, voiceB, voiceA, voiceB]);
  ok(
    "deterministic embeddings",
    JSON.stringify(a.embeddings) === JSON.stringify(b.embeddings)
  );
  ok(
    "deterministic assignment",
    JSON.stringify(a.result) === JSON.stringify(b.result)
  );
}

// 6. THE HEADLINE FINDING: one voice shifting register — normal turns
//    alternating with laugh/shout turns — must NOT split into two speakers.
//    Without the frame-level speech-likeness gate this exact construction
//    split at ~0.97 confidence: the noise-excited turns sit ≥0.49 cosine from
//    the same voice's normal speech in a TIGHT second cloud the width-ratio
//    separation guard cannot catch.
{
  const { result: r, embeddings } = await diarizeConversation([
    voiceA, excited, voiceA, excited, voiceA, excited,
  ]);
  ok("style shift -> speakerCount 1", r.speakerCount === 1, `got ${r.speakerCount}`);
  ok("style shift -> every word speaker 0", r.wordSpeakers.every((s) => s === 0));
  ok(
    "laugh-like turns embed empty (gated at the frame level)",
    embeddings[1].length === 0 && embeddings[3].length === 0 && embeddings[5].length === 0,
    `lens=${embeddings.map((e) => e.length)}`
  );
}

// 7. KNOWN LIMITATION (documented, deliberate): two same-channel voices that
//    differ only by ~8% formant placement — the axis genuinely similar
//    same-mic voices differ on — measure well under CLUSTER_TAU and merge.
//    The feature reports ONE speaker rather than risk painting one person as
//    two; if the recipe ever grows a real identity axis, this test will fail
//    and should then be flipped to assert speakerCount 2.
{
  const va = formantVoice(1.0);
  const vb = formantVoice(1.08);
  const { result: r, embeddings } = await diarizeConversation([va, vb, va, vb, va, vb]);
  ok(
    "similar voices embed non-empty (the merge is a judgement, not a gate)",
    embeddings.every((e) => e.length === 24),
    `lens=${embeddings.map((e) => e.length)}`
  );
  ok(
    "similar same-mic voices -> ONE speaker today (conservative by design)",
    r.speakerCount === 1,
    `got ${r.speakerCount}`
  );
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
