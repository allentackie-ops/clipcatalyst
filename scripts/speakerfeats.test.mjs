// Behavioural tests for the browser speaker-feature extractor: synthetic PCM
// in pure JS (no ffmpeg) through frames → segment embeddings → the shared
// assignSpeakers core. Mirrors the discrimination properties the Python
// engine proves on its side.
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

/** Voice A: sawtooth at 110 Hz. */
function sawtooth(out, offset, seconds, freq = 110) {
  const n = Math.round(seconds * SR);
  const period = SR / freq;
  for (let i = 0; i < n; i++) {
    const phase = (i % period) / period;
    out[offset + i] = AMP * (2 * phase - 1);
  }
}

/** Voice B: square wave at 280 Hz. */
function square(out, offset, seconds, freq = 280) {
  const n = Math.round(seconds * SR);
  const period = SR / freq;
  for (let i = 0; i < n; i++) {
    const phase = (i % period) / period;
    out[offset + i] = phase < 0.5 ? AMP : -AMP;
  }
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
{
  const { result: r, embeddings } = await diarizeConversation([
    sawtooth, square, sawtooth, square, sawtooth, square,
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
    sawtooth, sawtooth, sawtooth, sawtooth, sawtooth, sawtooth,
  ]);
  ok("one voice -> speakerCount 1", r.speakerCount === 1, `got ${r.speakerCount}`);
  ok("one voice -> every word speaker 0", r.wordSpeakers.every((s) => s === 0));
}

// 3. Silence-only segment → empty embedding; a voiced one stays usable.
{
  const pcm = new Float32Array(6 * SR); // silence...
  sawtooth(pcm, 4 * SR, 2); // ...except the last 2 s
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
  const a = await diarizeConversation([sawtooth, square, sawtooth, square]);
  const b = await diarizeConversation([sawtooth, square, sawtooth, square]);
  ok(
    "deterministic embeddings",
    JSON.stringify(a.embeddings) === JSON.stringify(b.embeddings)
  );
  ok(
    "deterministic assignment",
    JSON.stringify(a.result) === JSON.stringify(b.result)
  );
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
