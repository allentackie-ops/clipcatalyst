// Browser face detection for the crop track.
//
// Samples the source video at ~2 Hz, runs face-api's tiny_face_detector on a
// small offscreen canvas, and returns raw `FaceSample`s for `buildCropTrack`
// (lib/studio/croptrack.ts) to turn into a camera move.
//
// Three rules shape everything here:
//  1. face-api is ~1.3 MB. It is loaded with a dynamic `import()` so it never
//     enters the initial bundle — only a run that actually reframes pays for it.
//  2. Reframing is an enhancement. Every failure path (missing weights, no
//     WebGL, an undecodable frame, an unsupported browser) returns `[]`, which
//     `buildCropTrack` turns into a centered track. A clip must never fail to
//     render because detection had a bad day.
//  3. It runs on the main thread, so it must never own the main thread. The
//     per-frame budget scales down when the TF backend has no GPU behind it,
//     and we yield between samples so the page keeps painting either way.
//
// Units follow the shared contract: `cx` and `size` are fractions of the SOURCE
// width (0..1), so the samples are resolution-independent.

import type { FaceSample } from "./croptrack";
import type { ClipPlan } from "./types";

/** Detection rate across the clip, in samples per second. */
const SAMPLE_HZ = 2;
/** Width of the offscreen canvas we detect on — cost scales with pixels. */
const DETECT_WIDTH = 320;
/** tiny_face_detector input size; must be a multiple of 32. */
const DETECT_INPUT_SIZE = 320;
/**
 * Detector confidence floor. croptrack drops anything under 0.45, so keeping
 * this at or above that keeps the two in agreement.
 */
const DETECT_SCORE_THRESHOLD = 0.5;
/** Hard ceiling on seeks, so a pathological clip can't stall the pipeline. */
const MAX_SAMPLES = 240;

// --- Reduced budget for backends without a GPU ------------------------------
// On `cpu` the detector runs its whole graph synchronously on the main thread —
// ~355 ms per frame measured, versus ~15 ms on `webgl`. At the full budget the
// 61 samples of a 30 s clip are ~21 s of blocked main thread, and the page
// visibly locks up. So when we are not accelerated we buy the responsiveness
// back: sample half as often, feed the net a quarter of the pixels (160² rather
// than 320²), and stop sooner on long clips. The track that
// comes out is coarser (with fewer samples, `buildCropTrack`'s coverage gates
// will often lock the frame on the subject's median position instead of panning)
// but it is still a subject-aware crop, and the page stays usable.

/** Backends that run the graph off the main thread. */
const ACCELERATED_BACKENDS = new Set(["webgl", "webgpu"]);
const REDUCED_SAMPLE_HZ = 1;
/** Must stay a multiple of 32 — face-api pads the input to that grid. */
const REDUCED_INPUT_SIZE = 160;
const REDUCED_MAX_SAMPLES = 40;

const METADATA_TIMEOUT_MS = 15_000;
const LOADED_DATA_TIMEOUT_MS = 8_000;
const SEEK_TIMEOUT_MS = 4_000;
/** How long to wait for a frame before giving up on rAF (hidden tabs). */
const YIELD_FRAME_TIMEOUT_MS = 32;

/** Video source: an existing element, an object URL, or a `{ url }` wrapper. */
export type FaceTrackSource = HTMLVideoElement | { url: string } | string;

/** Only the fields detection needs — a full `ClipPlan` satisfies this. */
export type FaceTrackPlan = Pick<ClipPlan, "start" | "end">;

// --- Minimal local typing for the optional dependency -----------------------
// `@vladmandic/face-api` is loaded lazily and may not be installed when this
// file is typechecked, so we describe only the handful of members we touch
// rather than depending on its type declarations.

type FaceApiBox = { x: number; y: number; width: number; height: number };
type FaceApiDetection = { box?: FaceApiBox; score?: number };
type FaceApiTf = {
  setBackend: (name: string) => PromiseLike<boolean>;
  ready: () => PromiseLike<void>;
  getBackend?: () => string;
};
type FaceApiModule = {
  tf?: FaceApiTf;
  nets: { tinyFaceDetector: { loadFromUri: (uri: string) => Promise<void> } };
  TinyFaceDetectorOptions: new (options: {
    inputSize?: number;
    scoreThreshold?: number;
  }) => unknown;
  detectAllFaces: (
    input: HTMLCanvasElement,
    options?: unknown
  ) => PromiseLike<FaceApiDetection[]>;
};

/** The module plus the backend it ended up on — the backend sets our budget. */
type LoadedFaceApi = { api: FaceApiModule; backend: string | null };

/** Module-level memo: the weights are fetched at most once per page load. */
let faceApiPromise: Promise<LoadedFaceApi | null> | null = null;
let warned = false;

/** One line in the console, ever — this is a silent-degradation path. */
function warnOnce(error: unknown): void {
  if (warned) return;
  warned = true;
  console.warn(
    "[ClipCatalyst] Face tracking is unavailable, so clips will use a centered crop.",
    error
  );
}

/**
 * Where the committed tiny_face_detector weights live.
 *
 * Under GitHub Pages the site is served from a sub-path, so the model URL has
 * to carry the same prefix (`next.config.ts` exposes it). Empty by default.
 */
function modelUri(): string {
  const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
  return `${basePath}/models`;
}

/**
 * Pick a TensorFlow backend we can actually run.
 *
 * Left to itself, TF.js prefers `wasm` over `cpu` — but its wasm binaries are
 * fetched separately and we deliberately don't ship them, so on a machine
 * without WebGL it 404s and leaves the engine wedged rather than falling
 * through. Naming the two backends we DO have keeps face tracking working on
 * WebGL-less devices (slower, via cpu) instead of silently switching itself
 * off. Failure here is non-fatal: detection still degrades to a centered crop.
 *
 * Returns the backend that actually took, or `null` when we couldn't tell —
 * `detectFaces` sizes its per-frame budget from it, because `cpu` is roughly
 * twenty times more expensive per detection than `webgl`.
 */
async function selectBackend(tf: FaceApiTf | undefined): Promise<string | null> {
  if (!tf?.setBackend) return tf?.getBackend?.() ?? null;
  for (const name of ["webgl", "cpu"]) {
    try {
      if (await tf.setBackend(name)) {
        await tf.ready();
        return tf.getBackend?.() ?? name;
      }
    } catch {
      // Try the next one.
    }
  }
  return tf.getBackend?.() ?? null;
}

/**
 * Lazily import face-api and load the weights, memoized for the session.
 *
 * A failure is memoized too: if the weights 404 on the first clip there is no
 * point re-fetching 1.3 MB for the next one.
 */
function loadFaceApi(): Promise<LoadedFaceApi | null> {
  if (!faceApiPromise) {
    faceApiPromise = (async () => {
      // Optional dependency, resolved at runtime. `@ts-ignore` (not
      // `@ts-expect-error`) because the specifier is perfectly valid once the
      // package is installed, and an unused expect-error is itself an error.
      // The literal specifier is kept so the bundler can code-split it.
      // @ts-ignore -- optional dependency; typed as FaceApiModule above.
      const mod = (await import("@vladmandic/face-api")) as unknown as FaceApiModule;
      const backend = await selectBackend(mod.tf);
      await mod.nets.tinyFaceDetector.loadFromUri(modelUri());
      return { api: mod, backend };
    })().catch((error: unknown) => {
      warnOnce(error);
      return null;
    });
  }
  return faceApiPromise;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/**
 * Wait for an event, resolving early when `ready()` already holds and after
 * `timeoutMs` regardless — detection must never hang the pipeline.
 */
function waitForEvent(
  target: EventTarget,
  event: string,
  timeoutMs: number,
  ready?: () => boolean
): Promise<void> {
  return new Promise((resolve) => {
    if (ready && ready()) {
      resolve();
      return;
    }
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      target.removeEventListener(event, finish);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    target.addEventListener(event, finish, { once: true });
  });
}

/**
 * Hand the main thread back between detections.
 *
 * Awaiting the detector is not enough on its own: on `cpu` the work happens
 * synchronously before its promise ever settles, and that promise then resolves
 * in a microtask — microtasks are drained before the browser gets anywhere near
 * rendering. Only a macrotask boundary gives the event loop a chance to style,
 * lay out and paint, which is the difference between "the page is working" and
 * "the page is frozen".
 *
 * `waitForFrame` asks for a real frame via rAF, which is worth its ~16 ms only
 * when the detection either side of it costs an order of magnitude more. rAF
 * never fires in a background tab, so a timer backstops it — sampling must not
 * stall just because the user switched tabs.
 */
function yieldToBrowser(waitForFrame: boolean): Promise<void> {
  return new Promise((resolve) => {
    if (waitForFrame && typeof requestAnimationFrame === "function") {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        resolve();
      };
      requestAnimationFrame(finish);
      setTimeout(finish, YIELD_FRAME_TIMEOUT_MS);
      return;
    }
    setTimeout(resolve, 0);
  });
}

function isVideoElement(source: FaceTrackSource): source is HTMLVideoElement {
  return typeof HTMLVideoElement !== "undefined" && source instanceof HTMLVideoElement;
}

/**
 * Caller-owned elements we are currently sampling.
 *
 * Sampling drives `currentTime`, so two passes over one element would seek each
 * other's frames out from under themselves. Rather than serialize (which would
 * stall the pipeline) we let the second pass fall back to its own element.
 */
const borrowed = new WeakSet<HTMLVideoElement>();

/** A muted, off-screen element we own and tear down when we're done. */
function createHiddenVideo(url: string): HTMLVideoElement {
  const video = document.createElement("video");
  video.src = url;
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.style.position = "fixed";
  video.style.left = "-9999px";
  video.style.top = "0";
  video.style.width = "1px";
  video.style.height = "1px";
  video.style.opacity = "0";
  video.style.pointerEvents = "none";
  document.body.appendChild(video);
  return video;
}

function destroyHiddenVideo(video: HTMLVideoElement): void {
  try {
    video.pause();
  } catch {
    /* ignore */
  }
  video.removeAttribute("src");
  try {
    video.load();
  } catch {
    /* ignore */
  }
  video.remove();
}

/** Seek and wait, treating "already there" as done (no `seeked` would fire). */
async function seekTo(video: HTMLVideoElement, time: number): Promise<void> {
  video.currentTime = time;
  // The browser flips `seeking` synchronously on assignment when it actually
  // has to move; if it didn't, we're already on the requested frame.
  if (!video.seeking && video.readyState >= 2) return;
  await waitForEvent(
    video,
    "seeked",
    SEEK_TIMEOUT_MS,
    () => !video.seeking && video.readyState >= 2
  );
}

/**
 * Detect faces across `[plan.start, plan.end]` of the source video.
 *
 * Returns samples with `t` relative to the clip start, ready for
 * `buildCropTrack`. Returns `[]` — never throws — when detection can't run.
 *
 * @param source     A `{ url }` gets a fresh hidden element, loaded and torn
 *                   down per call. Callers reframing several clips from one
 *                   file should instead pass an `HTMLVideoElement` they own and
 *                   keep for the whole run: the load and metadata round-trip is
 *                   most of the fixed cost here, and this borrows the element
 *                   (pausing it, then restoring position and playback) rather
 *                   than taking it over.
 * @param onProgress 0..1 across the sampled range.
 * @param signal     Aborting stops sampling and returns what was collected.
 */
export async function detectFaces(
  source: FaceTrackSource,
  plan: FaceTrackPlan,
  onProgress?: (progress: number) => void,
  signal?: AbortSignal
): Promise<FaceSample[]> {
  if (typeof document === "undefined") return [];
  if (signal?.aborted) return [];

  const loaded = await loadFaceApi();
  if (!loaded) return []; // already warned once
  if (signal?.aborted) return [];
  const faceapi = loaded.api;

  // Ask TF live rather than trusting the load-time choice: a lost WebGL context
  // drops the engine back to `cpu` mid-session. Unknown counts as unaccelerated
  // — a coarser track costs the user far less than a frozen tab.
  const live = faceapi.tf?.getBackend?.();
  const backend = (typeof live === "string" && live) || loaded.backend;
  const accelerated = backend !== null && ACCELERATED_BACKENDS.has(backend);

  const samples: FaceSample[] = [];

  // Borrow a caller-owned element when we can; otherwise (no element, or one
  // already being sampled) run on our own, which for an already-fetched URL
  // costs a decode but no network.
  let video: HTMLVideoElement;
  let owned: boolean;
  if (isVideoElement(source) && !borrowed.has(source)) {
    video = source;
    owned = false;
    borrowed.add(video);
  } else {
    const url = isVideoElement(source)
      ? source.currentSrc || source.src
      : typeof source === "string"
        ? source
        : source.url;
    if (!url) return [];
    video = createHiddenVideo(url);
    owned = true;
  }
  const restoreTime = owned ? 0 : video.currentTime;
  // Seeking a playing element would race our own draws — and leave the caller's
  // playhead somewhere it never asked to be.
  const wasPlaying = !owned && !video.paused;

  let mediaFailed = false;
  const onMediaError = () => {
    mediaFailed = true;
  };
  video.addEventListener("error", onMediaError);

  try {
    if (wasPlaying) video.pause();

    await waitForEvent(video, "loadedmetadata", METADATA_TIMEOUT_MS, () => video.readyState >= 1);
    // A decoded frame has to exist before the first seek, or the initial draw
    // would sample an empty video.
    await waitForEvent(video, "loadeddata", LOADED_DATA_TIMEOUT_MS, () => video.readyState >= 2);
    if (mediaFailed || video.readyState < 2) return [];

    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!(vw > 0) || !(vh > 0)) return [];

    const sourceDuration = Number.isFinite(video.duration) ? video.duration : plan.end;
    const start = clamp(plan.start, 0, Math.max(0, sourceDuration - 0.05));
    const end = clamp(plan.end, start, sourceDuration > 0 ? sourceDuration : plan.end);
    const span = Math.max(0, end - start);
    const sampleHz = accelerated ? SAMPLE_HZ : REDUCED_SAMPLE_HZ;
    const maxSamples = accelerated ? MAX_SAMPLES : REDUCED_MAX_SAMPLES;
    const count = clamp(Math.round(span * sampleHz) + 1, 1, maxSamples);

    // Detect on a small copy: face-api's cost is per-pixel, and the crop track
    // only needs face positions as fractions of the width.
    const cw = Math.max(1, Math.min(DETECT_WIDTH, vw));
    const ch = Math.max(1, Math.round((cw * vh) / vw));
    const canvas = document.createElement("canvas");
    canvas.width = cw;
    canvas.height = ch;
    // No `willReadFrequently`: nothing here reads pixels back into JS — the
    // canvas goes straight to the detector. Asking for it would force a
    // software backing store, so every draw of a decoded frame (which lives in
    // GPU memory) would pay a full readback for nothing.
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) return [];

    const detectorOptions = new faceapi.TinyFaceDetectorOptions({
      inputSize: accelerated ? DETECT_INPUT_SIZE : REDUCED_INPUT_SIZE,
      scoreThreshold: DETECT_SCORE_THRESHOLD,
    });

    for (let i = 0; i < count; i++) {
      if (signal?.aborted || mediaFailed) break;
      const rel = count === 1 ? 0 : (i / (count - 1)) * span;
      await seekTo(video, start + rel);
      if (signal?.aborted || mediaFailed) break;

      if (video.readyState >= 2) {
        ctx.drawImage(video, 0, 0, cw, ch);
        const detections = await faceapi.detectAllFaces(canvas, detectorOptions);
        for (const det of detections) {
          const box = det?.box;
          if (!box || !(box.width > 0)) continue;
          samples.push({
            t: Math.round(rel * 1000) / 1000,
            cx: clamp((box.x + box.width / 2) / cw, 0, 1),
            size: clamp(box.width / cw, 0, 1),
            score: clamp(typeof det.score === "number" ? det.score : 1, 0, 1),
          });
        }
      }
      onProgress?.((i + 1) / count);
      // Let the page breathe before the next (potentially very slow) detection.
      if (i + 1 < count) await yieldToBrowser(!accelerated);
    }

    return samples;
  } catch (error) {
    // Anything at all — a tainted canvas, a lost WebGL context, a decode
    // failure — degrades to a centered crop.
    warnOnce(error);
    return [];
  } finally {
    video.removeEventListener("error", onMediaError);
    if (owned) destroyHiddenVideo(video);
    else {
      // Leave a caller-owned element exactly where we found it: same playhead,
      // same playback state, and free for the next clip to borrow.
      borrowed.delete(video);
      try {
        if (Math.abs(video.currentTime - restoreTime) > 1e-3) {
          video.currentTime = restoreTime;
        }
      } catch {
        /* ignore */
      }
      if (wasPlaying) {
        try {
          void video.play().catch(() => {});
        } catch {
          /* ignore */
        }
      }
    }
  }
}
