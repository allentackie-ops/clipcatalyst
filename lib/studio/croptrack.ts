// Crop-track builder: turns noisy face detections into a calm camera move.
//
// Detection is per-engine (face-api in the browser, OpenCV on the server) but
// the *motion* must be identical in both, so all of the judgement lives here:
// pick a subject, hold through dropouts, smooth, refuse to chase jitter, and
// cap how fast the frame may travel.
//
// Units: every horizontal value is normalized to the SOURCE width (0..1), so
// the same track drives a 540x960 canvas and a 1080x1920 ffmpeg render.
//
// Pure and deterministic — no DOM, no timers. `croptrack.py` is a direct port;
// keep the two in step.

export type FaceSample = {
  /** Seconds from clip start. */
  t: number;
  /** Face-center x, normalized 0..1 of source width. */
  cx: number;
  /** Face width, normalized 0..1 of source width. Used to pick the subject. */
  size: number;
  /** Detector confidence 0..1. OpenCV Haar has none — pass 1. */
  score: number;
};

export type CropKeyframe = { t: number; cx: number };

export type CropTrack = {
  /** Ordered, deduplicated. Always has at least one entry. */
  keyframes: CropKeyframe[];
  /** True when the subject never moved enough to warrant panning. */
  isStatic: boolean;
  /** Fraction of samples where a face was actually found (0..1). */
  coverage: number;
};

export type CropTrackOptions = {
  /** Clip length in seconds. */
  duration: number;
  /** Output aspect (9/16 → 0.5625). Sets how wide the crop window is. */
  targetAspect: number;
  /** Source frame aspect (width/height), e.g. 16/9 → 1.7778. */
  sourceAspect: number;
};

// --- Tunables. Changing these changes how the camera "feels". --------------

/** Detections below this confidence are ignored entirely. */
const MIN_SCORE = 0.45;
/** Ignore target moves smaller than this (fraction of width) — anti-jitter. */
const DEADBAND = 0.018;
/** Ceiling on camera speed (fraction of width per second) — glide, not snap. */
const MAX_PAN_PER_SEC = 0.14;
/** Exponential smoothing time constant, seconds. Higher = lazier camera. */
const SMOOTH_TAU = 0.55;
/** Keep the last known position this long after losing the face. */
const HOLD_S = 1.2;
/** After HOLD_S with no face, ease back to center over this long. */
const RECENTER_TAU = 2.0;
/** A new face must be this much larger to steal focus from the current one. */
const SUBJECT_SWITCH_RATIO = 1.45;
/** ...and must stay larger for this long, so two speakers don't ping-pong. */
const SUBJECT_SWITCH_HOLD_S = 0.8;
/** Total horizontal travel below this ⇒ treat the shot as static. */
const STATIC_RANGE = 0.035;
/** Upper bound on emitted keyframes (ffmpeg expressions get unwieldy). */
const MAX_KEYFRAMES = 48;
/** Drop a keyframe whose value differs from the interpolation by less. */
const KEYFRAME_EPSILON = 0.004;

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/**
 * Half-width of the crop window, normalized to source width.
 *
 * A 9:16 window cut from a 16:9 frame keeps the full height, so its width is
 * (targetAspect / sourceAspect) of the source. Clamped to 1 for sources that
 * are already portrait (nothing to crop — the window is the whole frame).
 */
export function cropHalfWidth(targetAspect: number, sourceAspect: number): number {
  if (!(sourceAspect > 0) || !(targetAspect > 0)) return 0.5;
  return Math.min(1, targetAspect / sourceAspect) / 2;
}

/** Group samples that share a timestamp (multi-face frames). */
function groupByTime(samples: FaceSample[]): Map<number, FaceSample[]> {
  const byTime = new Map<number, FaceSample[]>();
  for (const s of samples) {
    if (!Number.isFinite(s.t) || !Number.isFinite(s.cx)) continue;
    if (s.score < MIN_SCORE) continue;
    const key = Math.round(s.t * 1000) / 1000;
    const bucket = byTime.get(key);
    if (bucket) bucket.push(s);
    else byTime.set(key, [s]);
  }
  return byTime;
}

/**
 * Choose one face per frame, sticky across frames.
 *
 * Without hysteresis a two-person podcast flips the crop between speakers on
 * whichever face the detector liked best that frame. We keep the incumbent
 * unless a rival is clearly bigger for a sustained stretch.
 */
function pickSubjects(byTime: Map<number, FaceSample[]>): FaceSample[] {
  const times = [...byTime.keys()].sort((a, b) => a - b);
  const chosen: FaceSample[] = [];
  let current: FaceSample | null = null;
  let rivalSince: number | null = null;

  for (const t of times) {
    const faces = byTime.get(t) as FaceSample[];
    const anchor: FaceSample | null = current;
    // Nearest to the incumbent wins ties; size breaks the rest.
    const byNearest: FaceSample[] = anchor
      ? [...faces].sort(
          (a, b) => Math.abs(a.cx - anchor.cx) - Math.abs(b.cx - anchor.cx)
        )
      : [...faces].sort((a, b) => b.size - a.size);
    const incumbent: FaceSample = byNearest[0];
    const largest: FaceSample = [...faces].sort((a, b) => b.size - a.size)[0];

    if (!current) {
      current = largest;
      rivalSince = null;
    } else if (
      largest !== incumbent &&
      largest.size > incumbent.size * SUBJECT_SWITCH_RATIO
    ) {
      // A bigger face is on screen — start (or continue) its audition.
      if (rivalSince === null) rivalSince = t;
      if (t - rivalSince >= SUBJECT_SWITCH_HOLD_S) {
        current = largest;
        rivalSince = null;
      } else {
        current = incumbent;
      }
    } else {
      rivalSince = null;
      current = incumbent;
    }
    const picked: FaceSample = current;
    chosen.push({ t, cx: picked.cx, size: picked.size, score: picked.score });
  }
  return chosen;
}

/**
 * Walk a fixed timeline, easing the camera toward the subject.
 *
 * Three behaviours stack: a deadband so micro-movement never moves the frame,
 * exponential smoothing so motion is gradual, and a hard velocity cap so the
 * camera can never lurch. When the face is lost we hold, then drift to center.
 */
function simulate(
  subjects: FaceSample[],
  duration: number,
  half: number
): CropKeyframe[] {
  const center = 0.5;
  const lo = half;
  const hi = 1 - half;
  // Sources narrower than the target window can't pan at all.
  if (hi <= lo) return [{ t: 0, cx: center }];

  const step = 1 / 15; // simulate at 15 Hz; keyframes are thinned afterwards
  const frames: CropKeyframe[] = [];

  let cam = subjects.length > 0 ? clamp(subjects[0].cx, lo, hi) : center;
  let target = cam;
  let idx = 0;
  let lastSeen = subjects.length > 0 ? subjects[0].t : -Infinity;

  for (let t = 0; t <= duration + 1e-9; t += step) {
    // Advance to the most recent detection at or before t.
    while (idx < subjects.length && subjects[idx].t <= t) {
      const s = subjects[idx];
      const desired = clamp(s.cx, lo, hi);
      // Deadband: only re-aim when the subject has genuinely moved.
      if (Math.abs(desired - target) > DEADBAND) target = desired;
      lastSeen = s.t;
      idx++;
    }

    const gap = t - lastSeen;
    let aim = target;
    let tau = SMOOTH_TAU;
    if (gap > HOLD_S) {
      // Face lost for a while: give up gracefully rather than freeze forever.
      aim = center;
      tau = RECENTER_TAU;
    }

    const alpha = 1 - Math.exp(-step / tau);
    let next = cam + (aim - cam) * alpha;

    // Velocity ceiling — the one rule that keeps this from looking robotic.
    const maxDelta = MAX_PAN_PER_SEC * step;
    const delta = next - cam;
    if (Math.abs(delta) > maxDelta) next = cam + Math.sign(delta) * maxDelta;

    cam = clamp(next, lo, hi);
    // Round first, then re-clamp: rounding alone can land a hair outside the
    // legal range, which would hand the renderer a negative crop offset.
    frames.push({
      t: Math.round(t * 1000) / 1000,
      cx: clamp(Math.round(cam * 10000) / 10000, lo, hi),
    });
  }
  return frames;
}

/**
 * Thin a dense frame list to keyframes a renderer can interpolate.
 *
 * Ramer–Douglas–Peucker against linear interpolation: drop any point the
 * renderer would have reconstructed anyway, then enforce a hard cap.
 */
function thin(frames: CropKeyframe[], epsilon: number): CropKeyframe[] {
  if (frames.length <= 2) return frames;
  const keep = new Array<boolean>(frames.length).fill(false);
  keep[0] = true;
  keep[frames.length - 1] = true;

  const stack: [number, number][] = [[0, frames.length - 1]];
  while (stack.length > 0) {
    const [a, b] = stack.pop()!;
    if (b <= a + 1) continue;
    const fa = frames[a];
    const fb = frames[b];
    const span = fb.t - fa.t || 1;
    let worst = -1;
    let worstIdx = -1;
    for (let i = a + 1; i < b; i++) {
      const lerped = fa.cx + ((frames[i].t - fa.t) / span) * (fb.cx - fa.cx);
      const err = Math.abs(frames[i].cx - lerped);
      if (err > worst) {
        worst = err;
        worstIdx = i;
      }
    }
    if (worst > epsilon && worstIdx > 0) {
      keep[worstIdx] = true;
      stack.push([a, worstIdx], [worstIdx, b]);
    }
  }
  return frames.filter((_, i) => keep[i]);
}

/** Build the camera move for one clip. Safe on empty input (returns center). */
export function buildCropTrack(
  samples: FaceSample[],
  options: CropTrackOptions
): CropTrack {
  const duration = Math.max(0.1, options.duration);
  const half = cropHalfWidth(options.targetAspect, options.sourceAspect);

  const byTime = groupByTime(samples);
  const subjects = pickSubjects(byTime);
  const expectedFrames = Math.max(1, Math.round(duration * 2));
  const coverage = Math.min(1, subjects.length / expectedFrames);

  if (subjects.length === 0) {
    return { keyframes: [{ t: 0, cx: 0.5 }], isStatic: true, coverage: 0 };
  }

  const frames = simulate(subjects, duration, half);
  const values = frames.map((f) => f.cx);
  const range = Math.max(...values) - Math.min(...values);

  if (range < STATIC_RANGE) {
    // Barely moved: one fixed, well-placed crop beats a drifting one.
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    return {
      keyframes: [{ t: 0, cx: Math.round(mean * 10000) / 10000 }],
      isStatic: true,
      coverage,
    };
  }

  let keyframes = thin(frames, KEYFRAME_EPSILON);
  // Enforce the cap by relaxing the tolerance until it fits.
  let epsilon = KEYFRAME_EPSILON;
  while (keyframes.length > MAX_KEYFRAMES && epsilon < 0.25) {
    epsilon *= 1.6;
    keyframes = thin(frames, epsilon);
  }
  if (keyframes.length > MAX_KEYFRAMES) {
    const stride = Math.ceil(keyframes.length / MAX_KEYFRAMES);
    keyframes = keyframes.filter((_, i) => i % stride === 0 || i === keyframes.length - 1);
  }

  return { keyframes, isStatic: false, coverage };
}

/** Camera x at time t (linear between keyframes, clamped at the ends). */
export function cropCenterAt(track: CropTrack, t: number): number {
  const ks = track.keyframes;
  if (ks.length === 0) return 0.5;
  if (ks.length === 1 || t <= ks[0].t) return ks[0].cx;
  const last = ks[ks.length - 1];
  if (t >= last.t) return last.cx;
  let lo = 0;
  let hi = ks.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ks[mid].t <= t) lo = mid;
    else hi = mid;
  }
  const a = ks[lo];
  const b = ks[hi];
  const span = b.t - a.t;
  if (span <= 0) return b.cx;
  return a.cx + ((t - a.t) / span) * (b.cx - a.cx);
}
