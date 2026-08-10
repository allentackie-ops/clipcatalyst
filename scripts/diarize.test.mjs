// Behavioural tests for the shared speaker-assignment core.
import {
  assignSpeakers,
  buildSpeechSegments,
  MAX_SPEAKERS,
  SPEAKER_COLORS,
} from "../.croptrack-build/diarize.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};

// Deterministic pseudo-noise (no Math.random — tests must be reproducible).
const noise = (seed) => {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return (s / 2 ** 32 - 0.5) * 2;
  };
};

/** Unit vector with jitter, renormalized — one "voice" = one base direction. */
const voice = (base, jitter, rnd) => {
  const v = base.map((x) => x + jitter * rnd());
  const n = Math.hypot(...v);
  return v.map((x) => x / n);
};

const A = [1, 0, 0, 0, 0, 0, 0, 0];
const B = [0, 1, 0, 0, 0, 0, 0, 0];
const C = [0, 0, 1, 0, 0, 0, 0, 0];
const D = [0, 0, 0, 1, 0, 0, 0, 0];
const E = [0, 0, 0, 0, 1, 0, 0, 0];

/** Build words + segments for alternating turns of `secs` seconds each.
 *
 * Word times are computed from integers each time, never accumulated: float
 * drift once made turn 4 start at 19.999…, so `floor(start / span)` mapped a
 * segment to the WRONG turn's voice and the fixture blamed the module for
 * faithfully labeling the inconsistent input it was given. */
function conversation(turnBases, secs, jitter, seed) {
  const rnd = noise(seed);
  const words = [];
  const span = secs + 1.0; // turn + pause
  for (let turn = 0; turn < turnBases.length; turn++) {
    const wordsInTurn = Math.max(2, Math.round(secs * 2.5));
    for (let w = 0; w < wordsInTurn; w++) {
      const start = turn * span + (w * secs) / wordsInTurn;
      words.push({
        text: ` w${words.length}`,
        start,
        end: start + secs / wordsInTurn - 0.05,
      });
    }
  }
  const segments = buildSpeechSegments(words);
  // One embedding per segment: whichever turn the segment's midpoint is in.
  const embeddings = segments.map((seg) => {
    const mid = (seg.start + seg.end) / 2;
    const turn = Math.min(turnBases.length - 1, Math.floor(mid / span));
    return voice(turnBases[turn], jitter, rnd);
  });
  return { words, segments, embeddings };
}

// 1. The false-split guard: ONE voice with heavy prosody variation stays one.
{
  const { words, segments, embeddings } = conversation(
    [A, A, A, A, A, A], 4, 0.45, 7
  );
  const r = assignSpeakers(words, segments, embeddings);
  ok("one varied voice -> one speaker", r.speakerCount === 1, `got ${r.speakerCount}`);
  ok("single speaker -> zero confidence", r.confidence === 0);
  ok("single speaker -> every word is speaker 0", r.wordSpeakers.every((s) => s === 0));
}

// 2. Two clearly distinct voices alternating -> two speakers, correct turns.
{
  const { words, segments, embeddings } = conversation(
    [A, B, A, B, A, B], 4, 0.08, 11
  );
  const r = assignSpeakers(words, segments, embeddings);
  ok("two voices -> two speakers", r.speakerCount === 2, `got ${r.speakerCount}`);
  ok("two voices -> confident", r.confidence > 0.4, `conf=${r.confidence}`);
  const first = r.wordSpeakers[0];
  const second = r.wordSpeakers[words.length - 1];
  ok("alternating turns get different labels", first !== second, `${first} vs ${second}`);
  ok("turns alternate", r.turns.length >= 4 && r.turns.every((t, i, a) => i === 0 || t.speaker !== a[i - 1].speaker), `turns=${r.turns.length}`);
}

// 3. Equal speech -> labels ordered by speech time desc, ties by first start.
{
  const { words, segments, embeddings } = conversation(
    [A, B, A, B], 4, 0.05, 3
  );
  const r = assignSpeakers(words, segments, embeddings);
  ok("speaker 0 speaks first on a tie", r.wordSpeakers[0] === 0, `got ${r.wordSpeakers[0]}`);
}

// 4. A dominant host with a brief guest interjection: a 2s guest turn is
//    under the speech floor and gets absorbed into the host.
{
  const rnd = noise(5);
  const words = [];
  let t = 0;
  const mk = (base, secs) => {
    const idxs = [];
    const n = Math.max(2, Math.round(secs * 2.5));
    for (let w = 0; w < n; w++) {
      idxs.push(words.length);
      words.push({ text: ` w${words.length}`, start: t, end: t + secs / n - 0.05 });
      t += secs / n;
    }
    t += 1.0;
    return { base, start: idxs };
  };
  const layoutShort = [mk(A, 6), mk(A, 6), mk(B, 2), mk(A, 6), mk(A, 6)];
  const segsShort = buildSpeechSegments(words);
  const embShort = segsShort.map((seg) => {
    let cursor = 0;
    for (const turn of layoutShort) {
      const s = words[turn.start[0]].start;
      const e = words[turn.start[turn.start.length - 1]].end;
      if (seg.start >= s - 0.01 && seg.start <= e + 0.01) return voice(turn.base, 0.05, rnd);
      cursor++;
    }
    return voice(A, 0.05, rnd);
  });
  const r = assignSpeakers(words, segsShort, embShort);
  ok("2s interjection under the floor -> absorbed", r.speakerCount === 1, `got ${r.speakerCount}`);
}

// 5. Three and five voices: found, and capped at MAX_SPEAKERS.
{
  const three = conversation([A, B, C, A, B, C], 4, 0.06, 13);
  const r3 = assignSpeakers(three.words, three.segments, three.embeddings);
  ok("three voices -> three speakers", r3.speakerCount === 3, `got ${r3.speakerCount}`);

  const five = conversation([A, B, C, D, E, A, B, C, D, E], 4, 0.05, 17);
  const r5 = assignSpeakers(five.words, five.segments, five.embeddings);
  ok(`five voices -> capped at ${MAX_SPEAKERS}`, r5.speakerCount <= MAX_SPEAKERS, `got ${r5.speakerCount}`);
}

// 6. Robustness: empty input, no embeddings, mismatched lengths.
{
  const r = assignSpeakers([], [], []);
  ok("empty input -> sane result", r.speakerCount === 1 && r.turns.length === 0);
  const words = [{ text: " hi", start: 0, end: 0.4 }];
  const segs = buildSpeechSegments(words);
  const r2 = assignSpeakers(words, segs, segs.map(() => []));
  ok("no usable embeddings -> single speaker", r2.speakerCount === 1 && r2.wordSpeakers[0] === 0);
  const r3 = assignSpeakers(words, segs, []);
  ok("mismatched embedding count -> single speaker", r3.speakerCount === 1);
}

// 7. Determinism: identical input -> identical output.
{
  const a = conversation([A, B, A, B], 4, 0.08, 23);
  const r1 = assignSpeakers(a.words, a.segments, a.embeddings);
  const r2 = assignSpeakers(a.words, a.segments, a.embeddings);
  ok("deterministic", JSON.stringify(r1) === JSON.stringify(r2));
}

// 8. Segmentation: gaps split, long monologues split, every word covered.
{
  const words = [];
  let t = 0;
  for (let i = 0; i < 50; i++) {
    words.push({ text: ` w${i}`, start: t, end: t + 0.3 });
    t += i === 24 ? 2.0 : 0.35; // one big gap in the middle
  }
  const segs = buildSpeechSegments(words);
  ok("gap splits segments", segs.length >= 2, `got ${segs.length}`);
  const covered = new Set(segs.flatMap((s) => s.wordIdxs));
  ok("every word belongs to a segment", covered.size === words.length, `${covered.size}/${words.length}`);
  ok("segments respect the max length", segs.every((s) => s.end - s.start <= 6.5), `max=${Math.max(...segs.map((s) => s.end - s.start)).toFixed(1)}`);
}

// 9. Palette contract: colors exist for every possible speaker.
ok("palette covers MAX_SPEAKERS", SPEAKER_COLORS.length === MAX_SPEAKERS);
ok("speaker 0 keeps the brand violet", SPEAKER_COLORS[0] === "#a78bfa");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
