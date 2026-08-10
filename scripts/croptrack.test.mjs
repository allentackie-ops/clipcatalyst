// Behavioural tests for the shared crop-track math.
import { buildCropTrack, cropCenterAt, cropHalfWidth } from "../.croptrack-build/croptrack.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};

const OPTS = { duration: 10, targetAspect: 9 / 16, sourceAspect: 16 / 9 };
const half = cropHalfWidth(9 / 16, 16 / 9);
const LO = half, HI = 1 - half;

// half-width of a 9:16 window from 16:9 = (0.5625/1.7778)/2 = 0.1582
ok("crop half-width for 9:16 from 16:9", Math.abs(half - 0.1582) < 0.001, `got ${half.toFixed(4)}`);

// 1. No detections at all -> centered, static, zero coverage
{
  const tr = buildCropTrack([], OPTS);
  ok("empty input -> centered static", tr.isStatic && tr.keyframes.length === 1 && tr.keyframes[0].cx === 0.5 && tr.coverage === 0);
}

// 2. Perfectly still subject -> static single keyframe near the subject
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) s.push({ t, cx: 0.35, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, OPTS);
  ok("still subject -> static", tr.isStatic && tr.keyframes.length === 1);
  ok("still subject -> parks near subject", Math.abs(tr.keyframes[0].cx - 0.35) < 0.03, `got ${tr.keyframes[0].cx}`);
}

// 3. Jitter within the deadband must NOT produce a moving camera
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) s.push({ t, cx: 0.5 + (t % 1 === 0 ? 0.008 : -0.008), size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, OPTS);
  ok("sub-deadband jitter -> static", tr.isStatic, `keyframes=${tr.keyframes.length}`);
}

// 4. Subject walks left -> right: camera follows, never exceeds speed cap
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) s.push({ t, cx: 0.2 + (t / 10) * 0.6, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, OPTS);
  ok("moving subject -> non-static", !tr.isStatic);
  const start = cropCenterAt(tr, 0), end = cropCenterAt(tr, 10);
  ok("camera travels rightward", end > start + 0.1, `${start.toFixed(3)} -> ${end.toFixed(3)}`);
  // velocity check across the whole track
  let maxV = 0;
  for (let t = 0; t < 10; t += 0.1) {
    const v = Math.abs(cropCenterAt(tr, t + 0.1) - cropCenterAt(tr, t)) / 0.1;
    maxV = Math.max(maxV, v);
  }
  ok("respects pan speed ceiling", maxV <= 0.145, `maxV=${maxV.toFixed(4)}`);
}

// 5. Camera must never place the crop window outside the frame
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) s.push({ t, cx: t < 5 ? 0.02 : 0.98, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, OPTS);
  let inBounds = true;
  for (let t = 0; t <= 10; t += 0.05) {
    const cx = cropCenterAt(tr, t);
    if (cx < LO - 1e-6 || cx > HI + 1e-6) { inBounds = false; break; }
  }
  ok("crop window stays inside the frame", inBounds, `bounds ${LO.toFixed(3)}..${HI.toFixed(3)}`);
}

// 6. Subject visible only at the start -> too little signal to pan; hold the
//    framing we actually saw rather than inventing a drift.
{
  const s = [];
  for (let t = 0; t <= 3; t += 0.5) s.push({ t, cx: 0.75, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, { ...OPTS, duration: 12 });
  ok("sparse coverage -> locked frame", tr.isStatic, `keyframes=${tr.keyframes.length} coverage=${tr.coverage.toFixed(2)}`);
  ok("locked frame sits on the subject", Math.abs(cropCenterAt(tr, 6) - 0.75) < 0.02, `got ${cropCenterAt(tr, 6).toFixed(3)}`);
}

// 6b. REGRESSION: a motionless subject with an ordinary detector dropout must
//     not produce visible movement. This shipped at 47% of the frame.
{
  const s = [];
  for (let t = 0; t <= 30; t += 0.5) {
    if (t > 10 && t < 13) continue; // 3s dropout, entirely routine on real footage
    s.push({ t, cx: 0.25, size: 0.2, score: 0.9 });
  }
  const tr = buildCropTrack(s, { ...OPTS, duration: 30 });
  let min = 1, max = 0;
  for (let t = 0; t <= 30; t += 0.1) { const c = cropCenterAt(tr, t); min = Math.min(min, c); max = Math.max(max, c); }
  ok("dropout on a still subject -> no visible drift", max - min < 0.02, `travel=${(max - min).toFixed(4)} of width`);
}

// 6c. A genuinely long absence still releases toward center — but gently.
{
  const s = [];
  for (let t = 0; t <= 14; t += 0.5) s.push({ t, cx: 0.75, size: 0.2, score: 0.9 });
  for (let t = 20; t <= 30; t += 0.5) s.push({ t, cx: 0.75, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, { ...OPTS, duration: 30 });
  let maxV = 0;
  for (let t = 14; t < 20; t += 0.1) maxV = Math.max(maxV, Math.abs(cropCenterAt(tr, t + 0.1) - cropCenterAt(tr, t)) / 0.1);
  ok("recenter drift is slower than a real pan", maxV <= 0.036, `maxV=${maxV.toFixed(4)} (cap 0.035)`);
}

// 6d. A stray single detection must never define the whole clip's framing.
{
  const tr = buildCropTrack([{ t: 20, cx: 0.85, size: 0.09, score: 0.95 }], { ...OPTS, duration: 30 });
  ok("one stray detection -> ignored entirely, stays centered", tr.isStatic && Math.abs(cropCenterAt(tr, 0) - 0.5) < 1e-6, `cx=${cropCenterAt(tr,0).toFixed(4)}`);
}

// 6e. Hard cut: the new speaker must be in frame almost immediately, not after
//     a multi-second glide.
{
  const s = [];
  for (let t = 0; t <= 8; t += 0.5) s.push({ t, cx: 0.22, size: 0.2, score: 0.9 });
  for (let t = 8.5; t <= 20; t += 0.5) s.push({ t, cx: 0.78, size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, { ...OPTS, duration: 20 });
  // Allow the 2-sample confirmation window, then the subject must be inside the frame.
  const cam = cropCenterAt(tr, 10.0);
  ok("cut -> subject inside frame within ~1.5s", Math.abs(cam - 0.78) <= half, `cam=${cam.toFixed(3)} needs |cam-0.78|<=${half.toFixed(3)}`);
}

// 6f. ...but a single outlier detection must NOT trigger a cut-snap.
{
  const s = [];
  for (let t = 0; t <= 20; t += 0.5) {
    s.push({ t, cx: Math.abs(t - 10) < 0.01 ? 0.9 : 0.25, size: 0.2, score: 0.9 });
  }
  const tr = buildCropTrack(s, { ...OPTS, duration: 20 });
  let maxDev = 0;
  for (let t = 0; t <= 20; t += 0.1) maxDev = Math.max(maxDev, Math.abs(cropCenterAt(tr, t) - 0.25));
  ok("single outlier does not fling the frame", maxDev < 0.05, `maxDev=${maxDev.toFixed(4)}`);
}

// 7. Two faces: detector flip-flopping must not make the camera ping-pong
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) {
    s.push({ t, cx: 0.3, size: 0.20, score: 0.9 });               // steady speaker
    s.push({ t, cx: 0.7, size: 0.21 + (Math.sin(t * 7) * 0.02), score: 0.9 }); // rival, similar size
  }
  const tr = buildCropTrack(s, OPTS);
  let maxSwing = 0;
  for (let t = 0; t < 10; t += 0.25) {
    maxSwing = Math.max(maxSwing, Math.abs(cropCenterAt(tr, t + 0.25) - cropCenterAt(tr, t)));
  }
  ok("two similar faces -> no ping-ponging", maxSwing < 0.05, `maxSwing=${maxSwing.toFixed(4)}`);
}

// 8. Low-confidence detections are ignored
{
  const s = [];
  for (let t = 0; t <= 10; t += 0.5) s.push({ t, cx: 0.9, size: 0.2, score: 0.1 });
  const tr = buildCropTrack(s, OPTS);
  ok("low-confidence samples ignored", tr.coverage === 0 && tr.keyframes[0].cx === 0.5);
}

// 9. Keyframe cap under pathological input
{
  const s = [];
  for (let t = 0; t <= 60; t += 0.2) s.push({ t, cx: 0.5 + 0.35 * Math.sin(t * 2.3), size: 0.2, score: 0.9 });
  const tr = buildCropTrack(s, { ...OPTS, duration: 60 });
  ok("keyframe count capped", tr.keyframes.length <= 48, `got ${tr.keyframes.length}`);
  const sorted = tr.keyframes.every((k, i, a) => i === 0 || k.t > a[i - 1].t);
  ok("keyframes strictly ordered", sorted);
}

// 10. Portrait source (nothing to crop) must not blow up
{
  const tr = buildCropTrack([{ t: 0, cx: 0.5, size: 0.3, score: 0.9 }], { duration: 5, targetAspect: 9 / 16, sourceAspect: 9 / 16 });
  ok("portrait source -> centered, no crash", tr.keyframes.length >= 1 && Math.abs(tr.keyframes[0].cx - 0.5) < 1e-6);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
