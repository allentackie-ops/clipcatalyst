// Behavioural tests for the frozen edit-math core.
import {
  applyCommand,
  findPauses,
  resolveEdits,
  outputTimeAt,
  rewindowWords,
  retimeTrack,
  describeEdits,
  isEmptyEdits,
  EMPTY_EDITS,
  MIN_CLIP_S,
  PAUSE_MIN_S,
  TIGHTEN_MIN_PAUSE_S,
  CUT_PAD_S,
  MIN_CUT_S,
  ZOOM_DEFAULT_SCALE,
  ZOOM_MIN_SCALE,
  ZOOM_MAX_SCALE,
  ZOOM_DEFAULT_DURATION_S,
  ZOOM_RAMP_S,
  MAX_ZOOMS,
} from "../.croptrack-build/edits.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// One clip: plan [10, 36.3] in a 60 s source. Inter-word gaps (absolute):
//   A 11.5→12.7  (1.2 s)   B 13.3→13.77 (0.47 s)
//   C 14.5→14.94 (0.44 s — under PAUSE_MIN_S)   D 16.0→16.8 (0.8 s)
// plus a 0.6 s lead-in before the first word and a 0.8 s tail after the last.
const W = (text, start, end, speaker) =>
  speaker === undefined ? { text, start, end } : { text, start, end, speaker };
const ctx = {
  planStart: 10,
  planEnd: 36.3,
  sourceDuration: 60,
  words: [
    W(" We", 10.6, 11.0, 0), W(" were", 11.0, 11.5, 0),
    W(" talking", 12.7, 13.3, 0),
    W(" about", 13.77, 14.5, 1),
    W(" funnels", 14.94, 16.0, 1),
    W(" and", 16.8, 18.0, 0), W(" retention", 18.0, 20.0, 0),
    W(" because", 20.0, 23.0, 0), W(" numbers", 23.0, 26.0, 1),
    W(" matter", 26.0, 29.0, 1), W(" a", 29.0, 32.0, 0), W(" lot", 32.0, 35.5, 0),
  ],
  hooks: ["Hook one", "Hook two", "Hook three"],
};
const run = (cmds, e = EMPTY_EDITS, c = ctx) => {
  let cur = e, summary = "";
  for (const cmd of cmds) ({ edits: cur, summary } = applyCommand(cur, cmd, c));
  return { edits: cur, summary };
};

// 1. EMPTY_EDITS / isEmptyEdits contract.
ok("EMPTY_EDITS shape", eq(EMPTY_EDITS, { startDelta: 0, endDelta: 0, cuts: [], zooms: [], captions: true, hookIndex: 0 }));
ok("isEmptyEdits(EMPTY_EDITS)", isEmptyEdits(EMPTY_EDITS));
ok("captions off is not empty", !isEmptyEdits({ ...EMPTY_EDITS, captions: false }));
ok("hookIndex 1 is not empty", !isEmptyEdits({ ...EMPTY_EDITS, hookIndex: 1 }));

// 2. Tunables pinned directly (behavioural pins follow below).
ok("MIN_CLIP_S = 3.0", MIN_CLIP_S === 3.0);
ok("PAUSE_MIN_S = 0.45", PAUSE_MIN_S === 0.45);
ok("TIGHTEN_MIN_PAUSE_S = 0.5", TIGHTEN_MIN_PAUSE_S === 0.5);
ok("CUT_PAD_S = 0.12", CUT_PAD_S === 0.12);
ok("MIN_CUT_S = 0.2", MIN_CUT_S === 0.2);
ok("ZOOM_DEFAULT_SCALE = 1.28", ZOOM_DEFAULT_SCALE === 1.28);
ok("ZOOM_MIN_SCALE = 1.1", ZOOM_MIN_SCALE === 1.1);
ok("ZOOM_MAX_SCALE = 1.6", ZOOM_MAX_SCALE === 1.6);
ok("ZOOM_DEFAULT_DURATION_S = 1.6", ZOOM_DEFAULT_DURATION_S === 1.6);
ok("ZOOM_RAMP_S = 0.25 (renderer contract)", ZOOM_RAMP_S === 0.25);
ok("MAX_ZOOMS = 6", MAX_ZOOMS === 6);

// 3. findPauses: display time, threshold, lead-in/tail never counted.
{
  const p = findPauses(EMPTY_EDITS, ctx);
  ok("three pauses found (0.44 s gap excluded)", p.length === 3, `got ${p.length}`);
  ok("pause A exact", eq(p[0], { start: 1.5, end: 2.7, length: 1.2 }), JSON.stringify(p[0]));
  ok("pause B exact (0.47 s counts)", eq(p[1], { start: 3.3, end: 3.77, length: 0.47 }), JSON.stringify(p[1]));
  ok("pause D exact", eq(p[2], { start: 6.0, end: 6.8, length: 0.8 }), JSON.stringify(p[2]));
}

// 4. Pause at the exact boundary: trimming to a gap edge turns it into
//    lead-in, which is trim territory, not a pause.
{
  const { edits } = run([{ type: "trim", edge: "start", seconds: 1.5 }]);
  const p = findPauses(edits, ctx);
  ok("gap touching the start bound excluded", p.length === 2, `got ${p.length}`);
  ok("remaining pauses re-based to display time", eq(p.map((x) => x.start), [1.8, 4.5]), JSON.stringify(p));
}

// 5. cutPause longest: padded cut, exact numbers, truthful summary.
{
  const { edits, summary } = run([{ type: "cutPause", which: "longest" }]);
  ok("longest pause cut is padded", eq(edits.cuts, [{ start: 1.62, end: 2.58 }]), JSON.stringify(edits.cuts));
  ok("cutPause summary", summary === "Cut the 1.2 s pause at 0:02.", summary);

  // 6. A cut pause stops being reported, and "longest" moves to the next one.
  ok("cut pause no longer reported", findPauses(edits, ctx).length === 2);
  const again = applyCommand(edits, { type: "cutPause", which: "longest" }, ctx);
  ok("second longest is D", eq(again.edits.cuts, [{ start: 1.62, end: 2.58 }, { start: 6.12, end: 6.68 }]), JSON.stringify(again.edits.cuts));
}

// 7. cutPause all: every pause, padded, exact; output math checks out.
{
  const { edits, summary } = run([{ type: "cutPause", which: "all" }]);
  ok("all three pauses cut", eq(edits.cuts, [
    { start: 1.62, end: 2.58 }, { start: 3.42, end: 3.65 }, { start: 6.12, end: 6.68 },
  ]), JSON.stringify(edits.cuts));
  ok("cutPause all summary", summary === "Cut 3 pauses — saved 1.8 s.", summary);
  ok("output after all cuts", resolveEdits(edits, ctx).outputDuration === 24.55, `${resolveEdits(edits, ctx).outputDuration}`);
  ok("cutPause all again is a truthful no-op",
    applyCommand(edits, { type: "cutPause", which: "all" }, ctx).summary === "I couldn't find a pause of 0.45 s or longer to cut.");
}

// 8. tighten: threshold 0.5 leaves the 0.47 s pause alone.
{
  const { edits, summary } = run([{ type: "tighten" }]);
  ok("tighten cuts only A and D", eq(edits.cuts, [{ start: 1.62, end: 2.58 }, { start: 6.12, end: 6.68 }]), JSON.stringify(edits.cuts));
  ok("tighten summary", summary === "Tightened 2 pauses — saved 1.5 s.", summary);
}

// 9. cutPause {at}: containing or nearest within 1.0 s, else truthful no-op.
{
  const inside = run([{ type: "cutPause", which: { at: 1.9 } }]);
  ok("at inside pause A", eq(inside.edits.cuts, [{ start: 1.62, end: 2.58 }]));
  const near = run([{ type: "cutPause", which: { at: 7.7 } }]);
  ok("at 0.9 s past D still cuts D", eq(near.edits.cuts, [{ start: 6.12, end: 6.68 }]), JSON.stringify(near.edits.cuts));
  const far = run([{ type: "cutPause", which: { at: 7.9 } }]);
  ok("at 1.1 s past D refuses", far.edits.cuts.length === 0);
  ok("refusal is truthful", far.summary === "I couldn't find a pause within a second of 0:08.", far.summary);
}

// 10. THE pinned display-time case: trim 2 s off the start, then zoom at 5
//     must be STORED at original-clip-time 7.
{
  const { edits } = run([
    { type: "trim", edge: "start", seconds: 2 },
    { type: "zoom", at: 5 },
  ]);
  ok("startDelta after trim", edits.startDelta === 2);
  ok("zoom at 5 stored at original time 7", edits.zooms[0].start === 7, `${edits.zooms[0].start}`);
  ok("zoom end = start + default duration", edits.zooms[0].end === 8.6, `${edits.zooms[0].end}`);
}

// 11. Trim: summaries, both source bounds, and the MIN_CLIP_S floor.
{
  const t = run([{ type: "trim", edge: "start", seconds: 2 }]);
  ok("trim start summary", t.summary === "Start moved 2.0 s later — the clip is now 24.3 s.", t.summary);

  const ext = run([{ type: "trim", edge: "start", seconds: -15 }]);
  ok("extend start clamps at source 0", ext.edits.startDelta === -10, `${ext.edits.startDelta}`);
  ok("extend start summary", ext.summary === "Start moved 10.0 s earlier — the clip is now 36.3 s.", ext.summary);
  const ext2 = applyCommand(ext.edits, { type: "trim", edge: "start", seconds: -1 }, ctx);
  ok("further extension truthfully refused", ext2.summary === "The source starts there — the clip can't begin any earlier.");
  ok("refusal leaves edits alone", eq(ext2.edits, ext.edits));

  const late = run([{ type: "trim", edge: "end", seconds: -40 }]);
  ok("extend end clamps at source duration", late.edits.endDelta === 23.7, `${late.edits.endDelta}`);
  ok("extend end summary", late.summary === "End moved 23.7 s later — the clip is now 50.0 s.", late.summary);

  const floor = run([{ type: "trim", edge: "end", seconds: 40 }]);
  ok("trim end clamps at the 3 s floor", floor.edits.endDelta === -23.3, `${floor.edits.endDelta}`);
  ok("floored clip is exactly MIN_CLIP_S", resolveEdits(floor.edits, ctx).outputDuration === 3);
  const floor2 = applyCommand(floor.edits, { type: "trim", edge: "end", seconds: 1 }, ctx);
  ok("floor refusal wording", floor2.summary === "That would leave less than 3 s of clip, so I left it alone.", floor2.summary);

  const start24 = run([{ type: "trim", edge: "start", seconds: 24 }]);
  ok("trim start clamps at the floor too", start24.edits.startDelta === 23.3, `${start24.edits.startDelta}`);
}

// 12. The floor clamp counts OUTPUT time, not span: with 1.75 s already cut,
//     the earliest legal end lands mid-clip at 14.19, not at 13.0.
{
  const base = run([{ type: "cutPause", which: "all" }]).edits;
  const { edits } = run([{ type: "trim", edge: "end", seconds: 26 }], base);
  ok("cut-aware end clamp", edits.endDelta === -22.11, `${edits.endDelta}`);
  ok("cut-aware floor output is exactly 3", resolveEdits(edits, ctx).outputDuration === 3);
}

// 13. Explicit cut: keep fragments under MIN_CUT_S merge into the cut.
{
  const base = run([{ type: "cutPause", which: "longest" }]).edits; // cut [1.62, 2.58]
  const { edits } = run([{ type: "cut", start: 2.7, end: 3.42 }], base); // 0.12 s keep fragment
  const r = resolveEdits(edits, ctx);
  ok("0.12 s fragment absorbed", eq(r.keeps, [{ from: 10, to: 11.62 }, { from: 13.42, to: 36.3 }]), JSON.stringify(r.keeps));
  ok("fragment counted as removed", r.outputDuration === 24.5, `${r.outputDuration}`);

  const whole = run([{ type: "cut", start: 0, end: 26.3 }]);
  ok("cut spanning the whole clip refused", whole.edits.cuts.length === 0);
  ok("whole-clip refusal wording", whole.summary === "That would leave less than 3 s of clip, so I left it alone.", whole.summary);
  const wild = run([{ type: "cut", start: -5, end: 100 }]);
  ok("cut past both bounds refused", wild.edits.cuts.length === 0);
  const tiny = run([{ type: "cut", start: 5, end: 5.1 }]);
  ok("cut under MIN_CUT_S refused", tiny.edits.cuts.length === 0);
  ok("tiny-cut refusal wording", tiny.summary === "That's shorter than 0.2 s — too small to cut.", tiny.summary);
}

// 14. Zoom: defaults, clamps, bounds clipping, replacement, MAX_ZOOMS.
{
  const z = run([{ type: "zoom", at: 5 }]);
  ok("zoom defaults", eq(z.edits.zooms, [{ start: 5, end: 6.6, scale: 1.28 }]), JSON.stringify(z.edits.zooms));
  ok("zoom summary", z.summary === "Added a 1.6 s zoom at 0:05.", z.summary);

  const hi = run([{ type: "zoom", at: 5, scale: 3 }]);
  ok("scale clamps to max", hi.edits.zooms[0].scale === 1.6);
  const lo = run([{ type: "zoom", at: 5, scale: 0.5 }]);
  ok("scale clamps to min", lo.edits.zooms[0].scale === 1.1);

  const edge = run([{ type: "zoom", at: 25.5 }]);
  ok("zoom clips to the end bound", eq(edge.edits.zooms, [{ start: 25.5, end: 26.3, scale: 1.28 }]), JSON.stringify(edge.edits.zooms));
  const out = run([{ type: "zoom", at: 30 }]);
  ok("zoom past the end refused", out.edits.zooms.length === 0);
  ok("out-of-bounds wording", out.summary === "0:30 is outside the clip, so I didn't add a zoom.", out.summary);

  const rep = run([{ type: "zoom", at: 5 }, { type: "zoom", at: 6 }]);
  ok("overlapping zoom replaces", rep.edits.zooms.length === 1 && rep.edits.zooms[0].start === 6, JSON.stringify(rep.edits.zooms));
  ok("replace summary", rep.summary === "Replaced the overlapping zoom with a 1.6 s zoom at 0:06.", rep.summary);

  const six = run([0, 3, 6, 9, 12, 15].map((at) => ({ type: "zoom", at })));
  ok("six zooms allowed", six.edits.zooms.length === 6);
  const seven = applyCommand(six.edits, { type: "zoom", at: 20 }, ctx);
  ok("seventh zoom refused", seven.edits.zooms.length === 6);
  ok("MAX_ZOOMS wording", seven.summary === "That would make more than 6 zooms — remove one first.", seven.summary);
}

// 15. Normalize: a zoom fully inside a later cut is dropped.
{
  const { edits } = run([{ type: "zoom", at: 5 }, { type: "cut", start: 4.5, end: 7 }]);
  ok("cut swallows the zoom", edits.zooms.length === 0, JSON.stringify(edits.zooms));
  ok("the cut itself stays", eq(edits.cuts, [{ start: 4.5, end: 7 }]));
}

// 16. resolveEdits: exact keeps math, anchor stability, renderer-relative zooms.
{
  const { edits } = run([
    { type: "trim", edge: "start", seconds: 2 },
    { type: "cutPause", which: "all" },
    { type: "zoom", at: 5 },
  ]);
  // Cuts land at the SAME original-frame values as the untrimmed case: the
  // anchor never moves as edits accumulate.
  ok("cuts anchored to original clip time", eq(edits.cuts, [{ start: 3.42, end: 3.65 }, { start: 6.12, end: 6.68 }]), JSON.stringify(edits.cuts));
  const r = resolveEdits(edits, ctx);
  ok("resolved bounds", r.start === 12 && r.end === 36.3, `${r.start}..${r.end}`);
  ok("resolved keeps exact", eq(r.keeps, [
    { from: 12, to: 13.42 }, { from: 13.65, to: 16.12 }, { from: 16.68, to: 36.3 },
  ]), JSON.stringify(r.keeps));
  ok("outputDuration exact", r.outputDuration === 23.51, `${r.outputDuration}`);
  ok("zooms re-based to resolved start", eq(r.zooms, [{ start: 5, end: 6.6, scale: 1.28 }]), JSON.stringify(r.zooms));
  ok("captions ride through", r.captions === true);

  // 17. outputTimeAt across those keeps.
  ok("outT at start", outputTimeAt(r, 12) === 0);
  ok("outT at first cut edge", outputTimeAt(r, 13.42) === 1.42);
  ok("outT frozen inside a cut", outputTimeAt(r, 13.5) === 1.42);
  ok("outT resumes after the cut", outputTimeAt(r, 14) === 1.77, `${outputTimeAt(r, 14)}`);
  ok("outT mid third keep", outputTimeAt(r, 20) === 7.21, `${outputTimeAt(r, 20)}`);
  ok("outT at the end equals outputDuration", outputTimeAt(r, 36.3) === 23.51);
  ok("outT before start is 0", outputTimeAt(r, 11) === 0);
  ok("outT past the end saturates", outputTimeAt(r, 50) === 23.51);
}

// 18. retimeTrack: shift by −startDelta, round3, no mutation.
{
  const track = { keyframes: [{ t: 0, cx: 0.32 }, { t: 5.5, cx: 0.71 }], isStatic: false, coverage: 0.9 };
  const moved = retimeTrack(track, 1.234);
  ok("keyframes shifted", eq(moved.keyframes, [{ t: -1.234, cx: 0.32 }, { t: 4.266, cx: 0.71 }]), JSON.stringify(moved.keyframes));
  ok("flags carried", moved.isStatic === false && moved.coverage === 0.9);
  ok("original track untouched", track.keyframes[0].t === 0 && track.keyframes[1].t === 5.5);
}

// 19. rewindowWords: mirror of highlights.ts windowWords behaviour.
{
  const words = [
    { text: " a", start: 1, end: 2, speaker: 1 },
    { text: " b", start: 1.8, end: 2.6 },
    { text: " c", start: 2.5, end: 4.5, speaker: 0 },
    { text: " d", start: 5, end: 6 },
    { text: " e", start: 2.123456, end: 3.987654, speaker: 2 },
  ];
  const out = rewindowWords(words, 2, 5);
  ok("boundary words excluded (end<=start, start>=end)", out.length === 3, `got ${out.length}`);
  ok("spanning word clipped and re-based", eq(out[0], { text: " b", start: 0, end: 0.6 }), JSON.stringify(out[0]));
  ok("no speaker -> property absent", !("speaker" in out[0]));
  ok("speaker 0 carried (falsy-safe)", out[1].speaker === 0);
  ok("word clipped at the window end", out[1].end === 2.5);
  ok("round3 on re-based times", eq(out[2], { text: " e", start: 0.123, end: 1.988, speaker: 2 }), JSON.stringify(out[2]));
}

// 20. energetic: tighten + zooms at the first word at/after 1/3 and 2/3 of
//     the post-tighten output; deterministic; respects existing zooms.
{
  const a = run([{ type: "energetic" }]);
  ok("energetic tightens", eq(a.edits.cuts, [{ start: 1.62, end: 2.58 }, { start: 6.12, end: 6.68 }]));
  ok("energetic zoom placement exact", eq(a.edits.zooms, [
    { start: 10, end: 11.4, scale: 1.22 }, { start: 19, end: 20.4, scale: 1.22 },
  ]), JSON.stringify(a.edits.zooms));
  ok("energetic summary names both parts", a.summary === "Tightened 2 pauses and added 2 punch-in zooms.", a.summary);
  const b = run([{ type: "energetic" }]);
  ok("energetic is deterministic", eq(a.edits, b.edits));

  // A zoom within 2 s of a beat point blocks that placement (no replace).
  const pre = run([{ type: "zoom", at: 10.2 }]).edits;
  const c = applyCommand(pre, { type: "energetic" }, ctx);
  ok("placement near existing zoom skipped, zoom kept", eq(c.edits.zooms, [
    { start: 10.2, end: 11.8, scale: 1.28 }, { start: 19, end: 20.4, scale: 1.22 },
  ]), JSON.stringify(c.edits.zooms));
  ok("skip is reported truthfully", c.summary === "Tightened 2 pauses and added 1 punch-in zoom.", c.summary);

  // A zoom MORE than 2 s from both beat points blocks nothing.
  const far = run([{ type: "zoom", at: 13.5 }]).edits;
  const d = applyCommand(far, { type: "energetic" }, ctx);
  ok("distant zoom blocks nothing", d.edits.zooms.length === 3, JSON.stringify(d.edits.zooms));
}

// 21. describeEdits: chips in order, display-time zoom label, empty case.
{
  const { edits } = run([
    { type: "trim", edge: "start", seconds: 2 },
    { type: "trim", edge: "end", seconds: 1.5 },
    { type: "cutPause", which: "all" },
    { type: "zoom", at: 8 },
    { type: "captions", on: false },
    { type: "useHook", index: 1 },
  ]);
  ok("chips exact", eq(describeEdits(edits, ctx), [
    "Start +2.0s", "End -1.5s", "2 cuts", "Zoom at 0:08", "Captions off", "Hook 2",
  ]), JSON.stringify(describeEdits(edits, ctx)));
  ok("no edits -> no chips", eq(describeEdits(EMPTY_EDITS, ctx), []));
}

// 22. Floating-point accumulation: repeated trims stay exact via round3.
{
  let cur = EMPTY_EDITS;
  for (let i = 0; i < 10; i++) cur = applyCommand(cur, { type: "trim", edge: "start", seconds: 0.1 }, ctx).edits;
  ok("ten 0.1 s trims land on exactly 1", cur.startDelta === 1, `${cur.startDelta}`);
  let three = EMPTY_EDITS;
  for (let i = 0; i < 3; i++) three = applyCommand(three, { type: "trim", edge: "start", seconds: 0.1 }, ctx).edits;
  ok("three 0.1 s trims land on exactly 0.3", three.startDelta === 0.3, `${three.startDelta}`);
}

// 23. applyCommand never throws and never mutates its input.
{
  const deepFreeze = (o) => {
    for (const v of Object.values(o)) if (v && typeof v === "object") deepFreeze(v);
    return Object.freeze(o);
  };
  const frozen = deepFreeze({
    startDelta: 1, endDelta: 0, cuts: [{ start: 3, end: 4 }],
    zooms: [{ start: 8, end: 9, scale: 1.3 }], captions: true, hookIndex: 0,
  });
  const crash = "Something went wrong applying that, so nothing changed.";
  const cmds = [
    { type: "trim", edge: "start", seconds: 1 },
    { type: "cutPause", which: "all" },
    { type: "cutPause", which: { at: 2 } },
    { type: "cut", start: 5, end: 6 },
    { type: "tighten" },
    { type: "energetic" },
    { type: "zoom", at: 3 },
    { type: "clearZooms" },
    { type: "clearCuts" },
    { type: "captions", on: false },
    { type: "useHook", index: 2 },
    { type: "cycleHook" },
    { type: "reset" },
  ];
  let clean = true;
  for (const cmd of cmds) {
    const r = applyCommand(frozen, cmd, ctx);
    if (typeof r.summary !== "string" || r.summary === crash) { clean = false; break; }
  }
  ok("every command runs pure on frozen edits", clean);
  ok("frozen input unchanged", frozen.startDelta === 1 && frozen.cuts[0].start === 3);

  const bad = [
    { type: "trim", edge: "start", seconds: NaN },
    { type: "trim", edge: "end", seconds: Infinity },
    { type: "zoom", at: NaN },
    { type: "cutPause", which: { at: NaN } },
    { type: "cut", start: 5, end: 2 },
    { type: "nonsense" },
  ];
  let safe = true;
  for (const cmd of bad) {
    const r = applyCommand(EMPTY_EDITS, cmd, ctx);
    if (typeof r.summary !== "string" || !isEmptyEdits(r.edits)) { safe = false; break; }
  }
  ok("garbage commands are truthful no-ops", safe);
  const inf = applyCommand(EMPTY_EDITS, { type: "trim", edge: "end", seconds: Infinity }, ctx);
  ok("Infinity trim leaves deltas alone", inf.edits.endDelta === 0);
}

// 24. Captions and hooks.
{
  const off = run([{ type: "captions", on: false }]);
  ok("captions off", off.edits.captions === false && off.summary === "Captions are now off.");
  const twice = applyCommand(off.edits, { type: "captions", on: false }, ctx);
  ok("captions already off is truthful", twice.summary === "Captions are already off." && twice.edits.captions === false);

  const clampHi = run([{ type: "useHook", index: 99 }]);
  ok("useHook clamps to hooks.length-1", clampHi.edits.hookIndex === 2);
  ok("useHook clamp is truthful", clampHi.summary === "There are only 3 hooks — switched to hook 3.", clampHi.summary);
  const pick = run([{ type: "useHook", index: 1 }]);
  ok("useHook picks", pick.edits.hookIndex === 1 && pick.summary === "Switched to hook 2 of 3.");
  const cycle = run([{ type: "useHook", index: 2 }, { type: "cycleHook" }]);
  ok("cycleHook wraps", cycle.edits.hookIndex === 0, `${cycle.edits.hookIndex}`);
  const noHooks = applyCommand(EMPTY_EDITS, { type: "useHook", index: 1 }, { ...ctx, hooks: [] });
  ok("no hooks is a truthful no-op", noHooks.edits.hookIndex === 0);
  const oneHook = applyCommand(EMPTY_EDITS, { type: "cycleHook" }, { ...ctx, hooks: ["only"] });
  ok("cycleHook with one hook refuses", oneHook.summary === "There's only one hook for this clip.");
}

// 25. reset + clears.
{
  const { edits } = run([
    { type: "trim", edge: "start", seconds: 2 },
    { type: "cutPause", which: "all" },
    { type: "zoom", at: 5 },
    { type: "captions", on: false },
  ]);
  const r = applyCommand(edits, { type: "reset" }, ctx);
  ok("reset returns EMPTY_EDITS", isEmptyEdits(r.edits) && r.summary === "Reset — back to the original clip.");
  ok("reset on empty is truthful", applyCommand(EMPTY_EDITS, { type: "reset" }, ctx).summary === "There are no edits to reset.");

  const cz = applyCommand(edits, { type: "clearZooms" }, ctx);
  ok("clearZooms", cz.edits.zooms.length === 0 && cz.summary === "Removed 1 zoom.");
  ok("clearZooms on none is truthful", run([{ type: "clearZooms" }]).summary === "There are no zooms to remove.");
  const cc = applyCommand(edits, { type: "clearCuts" }, ctx);
  ok("clearCuts", cc.edits.cuts.length === 0);
  ok("clearCuts on none is truthful", run([{ type: "clearCuts" }]).summary === "There are no cuts to restore.");
}

// 26. Bound folding: a cut abutting a trim bound (or slivering against one)
//     must fold the resolved bounds onto the keeps — otherwise the renderer
//     seeks to `start` / stops at `end` and RECORDS the supposedly-cut head
//     or tail while the chat claims the cut worked (review finding A).
{
  const ctxA = { planStart: 12, planEnd: 40, sourceDuration: 60, words: [], hooks: [] };
  const head = run([{ type: "cut", start: 0, end: 2 }], EMPTY_EDITS, ctxA);
  ok("head-abutting cut summary", head.summary === "Cut 2.0 s — the clip is now 26.0 s.", head.summary);
  const rHead = resolveEdits(head.edits, ctxA);
  ok("head-abutting cut folds start", eq(
    { start: rHead.start, end: rHead.end, keeps: rHead.keeps, outputDuration: rHead.outputDuration },
    { start: 14, end: 40, keeps: [{ from: 14, to: 40 }], outputDuration: 26 }
  ), JSON.stringify(rHead));

  const ctxB = { planStart: 12, planEnd: 38, sourceDuration: 60, words: [], hooks: [] };
  const tail = run([{ type: "cut", start: 24, end: 26 }], EMPTY_EDITS, ctxB);
  const rTail = resolveEdits(tail.edits, ctxB);
  ok("tail-abutting cut folds end", eq(
    { start: rTail.start, end: rTail.end, keeps: rTail.keeps, outputDuration: rTail.outputDuration },
    { start: 12, end: 36, keeps: [{ from: 12, to: 36 }], outputDuration: 24 }
  ), JSON.stringify(rTail));

  const sliver = run([{ type: "cut", start: 22, end: 25.9 }], EMPTY_EDITS, ctxB);
  const rSliver = resolveEdits(sliver.edits, ctxB);
  ok("absorbed 0.1 s tail sliver folds end", eq(
    { start: rSliver.start, end: rSliver.end, keeps: rSliver.keeps, outputDuration: rSliver.outputDuration },
    { start: 12, end: 34, keeps: [{ from: 12, to: 34 }], outputDuration: 22 }
  ), JSON.stringify(rSliver));

  // Zooms re-base against the FOLDED start and re-clip to the folded window.
  const zHead = run([{ type: "cut", start: 0, end: 2 }, { type: "zoom", at: 5 }], EMPTY_EDITS, ctxA);
  const rZHead = resolveEdits(zHead.edits, ctxA);
  ok("zoom re-based to the folded start", eq(rZHead.zooms, [{ start: 3, end: 4.6, scale: 1.28 }]), JSON.stringify(rZHead.zooms));
  const zTail = run([{ type: "cut", start: 22, end: 25.9 }, { type: "zoom", at: 21 }], EMPTY_EDITS, ctxB);
  const rZTail = resolveEdits(zTail.edits, ctxB);
  ok("zoom re-clipped to the folded end", eq(rZTail.zooms, [{ start: 21, end: 22, scale: 1.28 }]), JSON.stringify(rZTail.zooms));
}

// 27. Fold invariant sweep: 50 deterministic (fixed-seed LCG) nasty cut/trim
//     combos — bound-abutting cuts, head slivers, stacked cuts, random
//     deltas. For EVERY input: keeps[0].from === start, last keep's to ===
//     end, keeps sorted / non-overlapping / each ≥ MIN_CUT_S, outputDuration
//     = Σ keeps, zooms inside the folded window.
{
  const sweepCtx = { planStart: 12, planEnd: 40, sourceDuration: 60, words: [], hooks: [] };
  const len = sweepCtx.planEnd - sweepCtx.planStart; // 28
  let seed = 987654321 % 2147483647;
  const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  const r2 = (v) => Math.round(v * 100) / 100;
  let holds = true;
  let detail = "";
  for (let i = 0; i < 50 && holds; i++) {
    const sd = r2(rnd() * 12 - 2); // -2 .. 10
    const ed = r2(rnd() * 12 - 10); // -10 .. 2
    const cuts = [];
    const nCuts = 1 + Math.floor(rnd() * 3);
    for (let c = 0; c < nCuts; c++) {
      const mode = rnd();
      let s;
      let e;
      if (mode < 0.3) { s = sd; e = sd + 0.3 + rnd() * 3; } // abuts the start bound
      else if (mode < 0.6) { e = len + ed; s = e - 0.3 - rnd() * 3; } // abuts the end bound
      else if (mode < 0.8) { s = sd + rnd() * 0.19; e = s + 0.3 + rnd() * 2; } // head sliver
      else { s = rnd() * len; e = s + 0.25 + rnd() * 4; } // anywhere
      cuts.push({ start: r2(s), end: r2(e) });
    }
    const zStart = r2(rnd() * len);
    const edits = {
      startDelta: sd, endDelta: ed, cuts,
      zooms: [{ start: zStart, end: r2(zStart + 1.6), scale: 1.3 }],
      captions: true, hookIndex: 0,
    };
    const r = resolveEdits(edits, sweepCtx);
    const last = r.keeps[r.keeps.length - 1];
    const sum = Math.round(r.keeps.reduce((a, k) => a + (k.to - k.from), 0) * 1000) / 1000;
    const winLen = Math.round((r.end - r.start) * 1000) / 1000;
    const good =
      r.keeps.length >= 1 &&
      r.keeps[0].from === r.start &&
      last.to === r.end &&
      sum === r.outputDuration &&
      r.keeps.every((k) => k.to - k.from >= MIN_CUT_S - 1e-9) &&
      r.keeps.every((k, j) => j === 0 || k.from >= r.keeps[j - 1].to) &&
      r.zooms.every((z) => z.start >= 0 && z.end <= winLen + 1e-9);
    if (!good) {
      holds = false;
      detail = `case ${i}: ${JSON.stringify({ edits, resolved: r })}`;
    }
  }
  ok("fold invariant holds across the 50-combo sweep", holds, detail);
}

// 28. applyZoom refuses a zoom fully inside a cut instead of claiming to add
//     one that normalize silently drops — and the refused replace must not
//     destroy a surviving zoom first (review finding B).
{
  const b1 = run([{ type: "cut", start: 4, end: 8 }, { type: "zoom", at: 5 }]);
  ok("zoom inside a cut refused", b1.summary === "0:05 is inside a cut — clear the cut first or pick another time.", b1.summary);
  ok("refusal adds no zoom", b1.edits.zooms.length === 0, JSON.stringify(b1.edits.zooms));
  ok("refusal keeps the cut", eq(b1.edits.cuts, [{ start: 4, end: 8 }]), JSON.stringify(b1.edits.cuts));

  const b2 = run([
    { type: "cut", start: 4, end: 8 },
    { type: "zoom", at: 7, duration: 2 }, // straddles the cut edge — survives
    { type: "zoom", at: 5.5 }, // fully inside the cut — must refuse
  ]);
  ok("in-cut replace refused truthfully", b2.summary === "0:06 is inside a cut — clear the cut first or pick another time.", b2.summary);
  ok("surviving zoom not destroyed by the refused replace", eq(b2.edits.zooms, [{ start: 7, end: 9, scale: 1.28 }]), JSON.stringify(b2.edits.zooms));
}

// 29. applyTrim clamps on PRE-move keeps: a bound landing within MIN_CUT_S of
//     a cut edge lets computeKeeps drop the sliver and the output dips under
//     MIN_CLIP_S — the whole trim must be refused, state unchanged (review
//     finding C).
{
  const ctxC = { planStart: 10, planEnd: 19.9, sourceDuration: 60, words: [], hooks: [] };
  const floorMsg = "That would leave less than 3 s of clip, so I left it alone.";

  const startBase = run([{ type: "cut", start: 5, end: 7 }], EMPTY_EDITS, ctxC).edits;
  const s = applyCommand(startBase, { type: "trim", edge: "start", seconds: 10 }, ctxC);
  ok("sliver-dropping start trim refused", s.summary === floorMsg, s.summary);
  ok("start trim refusal leaves edits unchanged", eq(s.edits, startBase), JSON.stringify(s.edits));
  ok("start trim refusal keeps output above the floor",
    resolveEdits(s.edits, ctxC).outputDuration === 7.9, `${resolveEdits(s.edits, ctxC).outputDuration}`);

  const endBase = run([{ type: "cut", start: 2.9, end: 5 }], EMPTY_EDITS, ctxC).edits;
  const e = applyCommand(endBase, { type: "trim", edge: "end", seconds: 10 }, ctxC);
  ok("sliver-dropping end trim refused", e.summary === floorMsg, e.summary);
  ok("end trim refusal leaves edits unchanged", eq(e.edits, endBase), JSON.stringify(e.edits));
  ok("end trim refusal keeps output above the floor",
    resolveEdits(e.edits, ctxC).outputDuration === 7.8, `${resolveEdits(e.edits, ctxC).outputDuration}`);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
