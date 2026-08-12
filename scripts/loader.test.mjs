// Behavioural tests for the branded-loading timing machine (lib/loader.ts).
//
// The module is pure and import-free, so the npm script compiles that single
// file into .croptrack-build/ and we drive it here with explicit timestamps —
// no DOM, no timers, no React. Every timing below is the real one shipped in
// the app: 180 ms delay, 420 ms minimum visible.
//
// Run:  npm run test:loader

import {
  DEFAULT_TIMING,
  IDLE,
  LOADER_DELAY_MS,
  LOADER_MIN_VISIBLE_MS,
  nextDeadline,
  normalizeTiming,
  reduceLoader,
  runLoader,
} from "../.croptrack-build/loader.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};

const D = LOADER_DELAY_MS;        // 180
const M = LOADER_MIN_VISIBLE_MS;  // 420

const start = (at) => ({ type: "start", at });
const finish = (at) => ({ type: "finish", at });
const tick = (at) => ({ type: "tick", at });

/** Fold a timeline from idle with the shipped timing unless told otherwise. */
const run = (events, timing) => runLoader(events, timing);

// --- 1. The constants are the ones the spec fixed -------------------------

{
  ok("delay is 180 ms", D === 180, String(D));
  ok("minimum visible is 420 ms", M === 420, String(M));
  ok("DEFAULT_TIMING carries both",
    DEFAULT_TIMING.delayMs === 180 && DEFAULT_TIMING.minVisibleMs === 420,
    JSON.stringify(DEFAULT_TIMING));
  ok("IDLE is not visible", IDLE.visible === false && IDLE.phase === "idle");
}

// --- 2. Shows nothing before the delay ------------------------------------

{
  ok("nothing at the instant the load starts",
    run([start(0)]).visible === false);

  for (const t of [1, 50, 100, 179]) {
    ok(`nothing at ${t} ms (before the 180 ms delay)`,
      run([start(0), tick(t)]).visible === false);
  }

  ok("at 179 ms the machine is still merely waiting",
    run([start(0), tick(179)]).phase === "waiting");

  ok("a tick one millisecond early does not paint",
    run([start(0), tick(D - 1)]).visible === false);
}

// --- 3. Shows after the delay ---------------------------------------------

{
  const s = run([start(0), tick(180)]);
  ok("visible at exactly 180 ms", s.visible === true, JSON.stringify(s));
  ok("phase is showing at 180 ms", s.phase === "showing", s.phase);
  ok("shownAt records the moment it was painted", s.shownAt === 180, String(s.shownAt));

  ok("still visible at 400 ms with the load still running",
    run([start(0), tick(180), tick(400)]).visible === true);

  // A late timer (a busy main thread) paints late, and the minimum window is
  // measured from the paint — not from the deadline it missed.
  const late = run([start(0), tick(300)]);
  ok("a late tick paints and measures the window from the paint",
    late.visible === true && late.shownAt === 300, JSON.stringify(late));
}

// --- 4. Once shown, stays for the minimum ---------------------------------

{
  // The spec's case: painted at 180 ms, load finished at 200 ms.
  const held = run([start(0), tick(180), finish(200)]);
  ok("a load finishing at 200 ms leaves the loader on screen",
    held.visible === true, JSON.stringify(held));
  ok("finishing at 200 ms moves it to holding, not idle",
    held.phase === "holding", held.phase);

  ok("still visible at 599 ms (one ms inside the 420 ms window)",
    run([start(0), tick(180), finish(200), tick(599)]).visible === true);

  const gone = run([start(0), tick(180), finish(200), tick(600)]);
  ok("gone at 600 ms = 180 + 420", gone.visible === false, JSON.stringify(gone));
  ok("and back to idle", gone.phase === "idle", gone.phase);

  ok("the loader was visible for exactly the 420 ms minimum",
    600 - 180 === M);

  // A load that outlives the minimum on its own leaves immediately.
  const long = run([start(0), tick(180), tick(1000), finish(2000)]);
  ok("a load finishing after the window is already served hides at once",
    long.visible === false && long.phase === "idle", JSON.stringify(long));

  const exact = run([start(0), tick(180), finish(600)]);
  ok("finishing exactly on the boundary hides at once",
    exact.visible === false, JSON.stringify(exact));
}

// --- 5. A load finishing at 100 ms shows nothing at all -------------------

{
  const quick = run([start(0), finish(100)]);
  ok("a 100 ms load never becomes visible", quick.visible === false, JSON.stringify(quick));
  ok("a 100 ms load ends idle", quick.phase === "idle", quick.phase);
  ok("a 100 ms load never records a paint", quick.shownAt === null, String(quick.shownAt));
  ok("no later tick can resurrect it",
    run([start(0), finish(100), tick(180), tick(600)]).visible === false);
  ok("a 179 ms load still shows nothing",
    run([start(0), finish(179), tick(180)]).visible === false);
}

// --- 6. Repeated starts do not stack timers -------------------------------

{
  // There is only ever one deadline, and re-starting never moves it.
  const a = run([start(0)]);
  ok("one start arms a single deadline at 180 ms",
    nextDeadline(a, DEFAULT_TIMING) === 180, String(nextDeadline(a)));

  const b = run([start(0), start(50), start(120), start(179)]);
  ok("four starts still arm exactly one deadline, unmoved at 180 ms",
    nextDeadline(b) === 180, String(nextDeadline(b)));
  ok("repeated starts do not push the paint later",
    run([start(0), start(50), start(120), tick(180)]).visible === true);
  ok("repeated starts do not paint early either",
    run([start(0), start(50), start(120), tick(179)]).visible === false);

  // Re-starting while visible must not extend the window.
  const c = run([start(0), tick(180), start(300), finish(400)]);
  ok("a start while showing does not re-arm the delay", c.phase === "holding");
  ok("a start while showing does not extend the minimum",
    nextDeadline(c) === 600, String(nextDeadline(c)));
  ok("and it still hides at 600 ms",
    run([start(0), tick(180), start(300), finish(400), tick(600)]).visible === false);

  // A start during the hold reclaims the loader instead of arming a delay.
  const d = run([start(0), tick(180), finish(200), start(300)]);
  ok("a start during the hold keeps the loader up", d.visible === true);
  ok("a start during the hold returns to showing", d.phase === "showing", d.phase);
  ok("a reclaimed loader has no pending deadline", nextDeadline(d) === null);
  ok("a reclaimed loader keeps its original paint time", d.shownAt === 180, String(d.shownAt));

  // The state machine never has two deadlines: every phase yields 0 or 1.
  for (const [name, s] of [
    ["idle", IDLE],
    ["waiting", run([start(0)])],
    ["showing", run([start(0), tick(180)])],
    ["holding", run([start(0), tick(180), finish(200)])],
  ]) {
    const dl = nextDeadline(s);
    ok(`${name} yields at most one deadline`, dl === null || typeof dl === "number", String(dl));
  }
  ok("idle has no deadline", nextDeadline(IDLE) === null);
  ok("showing has no deadline (it waits on the load, not a clock)",
    nextDeadline(run([start(0), tick(180)])) === null);
}

// --- 7. Full timelines ----------------------------------------------------

{
  // The common case in a static export: everything is already there.
  ok("a 40 ms navigation renders no loader at any point",
    [0, 10, 20, 39].every((t) => run([start(0), tick(t)]).visible === false) &&
      run([start(0), finish(40), tick(180)]).visible === false);

  // A real wait on a phone.
  const steps = [
    [0, false], [100, false], [179, false], [180, true], [900, true],
  ];
  ok("a 2 s load: hidden until 180 ms, then visible",
    steps.every(([t, want]) => run([start(0), tick(t)]).visible === want));
  // It has been on screen for 1820 ms by then, so the 420 ms floor is long
  // since paid — it leaves the instant the content arrives, no farewell hold.
  ok("…and leaves immediately when a 2 s load finishes",
    run([start(0), tick(180), tick(2000), finish(2000)]).visible === false);
}

// --- 8. Custom timings ----------------------------------------------------

{
  const t = { delayMs: 300, minVisibleMs: 1000 };
  ok("a custom delay is honoured",
    run([start(0), tick(299)], t).visible === false &&
      run([start(0), tick(300)], t).visible === true);
  ok("a custom minimum is honoured",
    run([start(0), tick(300), finish(310), tick(1299)], t).visible === true &&
      run([start(0), tick(300), finish(310), tick(1300)], t).visible === false);

  const zero = { delayMs: 0, minVisibleMs: 420 };
  const z = run([start(0)], zero);
  ok("a zero delay paints on start without needing a tick",
    z.visible === true && z.phase === "showing", JSON.stringify(z));
  ok("a zero delay still serves the minimum",
    run([start(0), finish(10), tick(419)], zero).visible === true &&
      run([start(0), finish(10), tick(420)], zero).visible === false);

  const noHold = { delayMs: 180, minVisibleMs: 0 };
  ok("a zero minimum leaves the moment the load ends",
    run([start(0), tick(180), finish(181)], noHold).visible === false);
}

// --- 9. Timing normalisation ---------------------------------------------

{
  ok("undefined timing falls back to the shipped defaults",
    normalizeTiming(undefined).delayMs === 180 &&
      normalizeTiming(undefined).minVisibleMs === 420);
  ok("a partial timing keeps the other default",
    normalizeTiming({ delayMs: 50 }).minVisibleMs === 420 &&
      normalizeTiming({ minVisibleMs: 50 }).delayMs === 180);
  ok("negative timings clamp to zero",
    normalizeTiming({ delayMs: -100, minVisibleMs: -1 }).delayMs === 0 &&
      normalizeTiming({ delayMs: -100, minVisibleMs: -1 }).minVisibleMs === 0);
  ok("NaN falls back rather than poisoning the deadline",
    normalizeTiming({ delayMs: NaN }).delayMs === 180 &&
      normalizeTiming({ delayMs: Infinity }).delayMs === 180);
  ok("a NaN delay cannot make the machine unpaintable",
    run([start(0), tick(180)], { delayMs: NaN }).visible === true);
}

// --- 10. Contract: purity and no accidental sharing -----------------------

{
  const before = run([start(0)]);
  const snapshot = JSON.stringify(before);
  reduceLoader(before, tick(180), DEFAULT_TIMING);
  reduceLoader(before, finish(180), DEFAULT_TIMING);
  ok("reduceLoader does not mutate the state it is given",
    JSON.stringify(before) === snapshot, JSON.stringify(before));

  const twice1 = reduceLoader(before, tick(180), DEFAULT_TIMING);
  const twice2 = reduceLoader(before, tick(180), DEFAULT_TIMING);
  ok("reduceLoader is deterministic",
    JSON.stringify(twice1) === JSON.stringify(twice2));

  ok("an unchanged fold returns the very same object (views can bail on identity)",
    reduceLoader(before, tick(100), DEFAULT_TIMING) === before);
  ok("a finish on idle is a no-op returning the same object",
    reduceLoader(IDLE, finish(10), DEFAULT_TIMING) === IDLE);
  ok("a tick on idle is a no-op returning the same object",
    reduceLoader(IDLE, tick(10), DEFAULT_TIMING) === IDLE);

  let frozen = true;
  try { IDLE.visible = true; } catch { /* strict mode throws, which is fine */ }
  if (IDLE.visible === true) frozen = false;
  ok("IDLE is frozen, so a caller cannot corrupt everyone else's initial state", frozen);

  ok("runLoader on an empty timeline is the identity",
    runLoader([], DEFAULT_TIMING) === IDLE);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
