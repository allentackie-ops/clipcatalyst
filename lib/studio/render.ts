// Canvas clip renderer: plays the source video hidden, cover-crops it onto a
// 9:16 canvas, overlays animated word captions + watermark + progress bar, and
// records canvas.captureStream + silently-captured audio with MediaRecorder.

import { cropCenterAt } from "./croptrack";
import type { CropTrack } from "./croptrack";
import { SPEAKER_COLORS } from "./diarize";
import type {
  ClipPlan,
  RenderOptions,
  RenderResult,
  TranscriptWord,
} from "./types";

/**
 * Render options plus the optional camera move.
 *
 * `RenderOptions` stays the frozen shared contract; the track is renderer-only
 * and optional, so existing callers keep compiling and keep their exact
 * behaviour (a centered crop).
 */
export type RenderClipOptions = RenderOptions & {
  /** Crop track from `buildCropTrack`. Absent → today's centered crop. */
  track?: CropTrack;
};

const MIME_PREFERENCES = [
  'video/mp4;codecs="avc1.42E01E,mp4a.40.2"',
  "video/mp4",
  "video/webm;codecs=vp9,opus",
  "video/webm",
] as const;

const BRAND_VIOLET = "#8b5cf6";
const ACTIVE_WORD_COLOR = "#a78bfa";

/**
 * Highlight color for the active word: the speaker's palette color when the
 * word carries a diarized speaker, else exactly the color used before
 * diarization existed (== SPEAKER_COLORS[0], so speaker 0 is unchanged too).
 */
function activeWordColor(word: CaptionWord): string {
  return word.speaker === undefined
    ? ACTIVE_WORD_COLOR
    : SPEAKER_COLORS[word.speaker % SPEAKER_COLORS.length] ?? ACTIVE_WORD_COLOR;
}
const CAPTION_MAX_WORDS = 4;
const CAPTION_MAX_CHARS = 18;
const PROGRESS_BAR_PX = 4;

const METADATA_TIMEOUT_MS = 15_000;
const SEEK_TIMEOUT_MS = 8_000;
const CANPLAY_TIMEOUT_MS = 8_000;
const STOP_TIMEOUT_MS = 10_000;
const AUDIO_RESUME_TIMEOUT_MS = 1_500;
const FONTS_READY_TIMEOUT_MS = 2_000;
const BACKGROUND_TICK_MS = 250;

type CaptionWord = { text: string; start: number; end: number; speaker?: number };
type CaptionGroup = { words: CaptionWord[]; start: number; end: number };
type LayoutWord = CaptionWord & { width: number };
type LayoutLine = { words: LayoutWord[]; width: number };

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** Group caption words into short strips: ≤ 4 words and ≤ 18 chars each. */
function buildCaptionGroups(words: TranscriptWord[]): CaptionGroup[] {
  const cleaned: CaptionWord[] = [];
  for (const w of words) {
    const text = w.text.trim();
    if (text) cleaned.push({ text, start: w.start, end: w.end, speaker: w.speaker });
  }
  const groups: CaptionGroup[] = [];
  let current: CaptionWord[] = [];
  let chars = 0;
  const flush = () => {
    if (current.length > 0) {
      groups.push({
        words: current,
        start: current[0].start,
        end: current[current.length - 1].end,
      });
      current = [];
      chars = 0;
    }
  };
  for (const w of cleaned) {
    const candidate = current.length === 0 ? w.text.length : chars + 1 + w.text.length;
    if (current.length > 0 && (current.length >= CAPTION_MAX_WORDS || candidate > CAPTION_MAX_CHARS)) {
      flush();
    }
    chars = current.length === 0 ? w.text.length : chars + 1 + w.text.length;
    current.push(w);
  }
  flush();
  return groups;
}

/** Wrap a group's words into measured lines no wider than maxWidth. */
function layoutGroup(
  ctx: CanvasRenderingContext2D,
  group: CaptionGroup,
  maxWidth: number
): LayoutLine[] {
  const spaceWidth = ctx.measureText(" ").width;
  const lines: LayoutLine[] = [];
  let line: LayoutWord[] = [];
  let lineWidth = 0;
  const flush = () => {
    if (line.length > 0) {
      lines.push({ words: line, width: lineWidth });
      line = [];
      lineWidth = 0;
    }
  };
  for (const w of group.words) {
    const width = ctx.measureText(w.text).width;
    const nextWidth = line.length === 0 ? width : lineWidth + spaceWidth + width;
    if (line.length > 0 && nextWidth > maxWidth) flush();
    lineWidth = line.length === 0 ? width : lineWidth + spaceWidth + width;
    line.push({ ...w, width });
  }
  flush();
  return lines;
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number
): void {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

/**
 * Wait for an event on the target, resolving early if `ready()` is already
 * true and resolving anyway after `timeoutMs` (never hangs the pipeline).
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

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder.isTypeSupported === "function") {
    for (const mime of MIME_PREFERENCES) {
      if (MediaRecorder.isTypeSupported(mime)) return mime;
    }
  }
  return undefined;
}

export async function renderClip(
  source: { url: string },
  plan: ClipPlan,
  options: RenderClipOptions,
  onProgress?: (progress: number) => void
): Promise<RenderResult> {
  if (typeof MediaRecorder === "undefined") {
    throw new Error(
      "Your browser can't record video (MediaRecorder is missing). Try a recent Chrome, Edge, or Firefox."
    );
  }
  const AudioContextCtor: typeof AudioContext | undefined =
    typeof AudioContext !== "undefined"
      ? AudioContext
      : (globalThis as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextCtor) {
    throw new Error(
      "Your browser doesn't support the Web Audio tools Studio needs. Try a recent Chrome, Edge, or Firefox."
    );
  }

  const clipDuration = Math.max(0.1, plan.end - plan.start);
  const height = options.height;
  const width = Math.round((height * 9) / 16 / 2) * 2; // nearest even 9:16 width

  // --- Hidden video element -------------------------------------------------
  const video = document.createElement("video");
  video.src = source.url;
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

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d", { alpha: false });

  let audioCtx: AudioContext | null = null;
  let sourceNode: MediaElementAudioSourceNode | null = null;
  let destNode: MediaStreamAudioDestinationNode | null = null;
  let canvasStream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let rafId = 0;
  let tickIntervalId: ReturnType<typeof setInterval> | undefined;
  let stopRequested = false;

  const cleanup = () => {
    cancelAnimationFrame(rafId);
    if (tickIntervalId !== undefined) {
      clearInterval(tickIntervalId);
      tickIntervalId = undefined;
    }
    try {
      video.pause();
    } catch {
      /* ignore */
    }
    if (canvasStream) {
      for (const track of canvasStream.getTracks()) track.stop();
    }
    if (destNode) {
      for (const track of destNode.stream.getTracks()) track.stop();
    }
    try {
      sourceNode?.disconnect();
    } catch {
      /* ignore */
    }
    try {
      destNode?.disconnect();
    } catch {
      /* ignore */
    }
    if (audioCtx && audioCtx.state !== "closed") {
      void audioCtx.close().catch(() => undefined);
    }
    video.removeAttribute("src");
    try {
      video.load();
    } catch {
      /* ignore */
    }
    video.remove();
  };

  try {
    if (!ctx) {
      throw new Error("Couldn't create a canvas drawing context for rendering.");
    }

    // --- Load metadata + seek to the clip start -----------------------------
    let mediaFailed = false;
    video.addEventListener("error", () => {
      mediaFailed = true;
    });

    await waitForEvent(video, "loadedmetadata", METADATA_TIMEOUT_MS, () => video.readyState >= 1);
    if (mediaFailed || video.readyState < 1) {
      throw new Error(
        "Couldn't load this video for rendering — the format may not be playable in your browser."
      );
    }

    const sourceDuration = Number.isFinite(video.duration) ? video.duration : plan.end;
    const seekTarget = Math.min(Math.max(0, plan.start), Math.max(0, sourceDuration - 0.05));
    const endTarget = Math.min(plan.end, sourceDuration > 0 ? sourceDuration : plan.end);

    video.currentTime = seekTarget;
    await waitForEvent(video, "seeked", SEEK_TIMEOUT_MS);
    await waitForEvent(video, "canplay", CANPLAY_TIMEOUT_MS, () => video.readyState >= 3);
    if (mediaFailed) {
      throw new Error("Video playback failed while preparing the render.");
    }

    // --- Silent audio capture (never routed to the speakers) ----------------
    audioCtx = new AudioContextCtor();
    if (audioCtx.state === "suspended") {
      // Some browsers (Safari especially) leave resume() pending forever
      // without a user gesture — race it so the pipeline can never hang here.
      await Promise.race([
        audioCtx.resume().catch(() => undefined),
        new Promise<void>((resolve) => setTimeout(resolve, AUDIO_RESUME_TIMEOUT_MS)),
      ]);
    }
    if (audioCtx.state !== "running") {
      throw new Error(
        "Your browser paused audio processing, so the clip's sound can't be captured. Click again to retry — or use Chrome, Edge, or Firefox."
      );
    }
    try {
      sourceNode = audioCtx.createMediaElementSource(video);
    } catch {
      throw new Error("Couldn't capture the video's audio for rendering.");
    }
    destNode = audioCtx.createMediaStreamDestination();
    sourceNode.connect(destNode); // NOT audioCtx.destination — stays silent

    // --- Recorder -----------------------------------------------------------
    canvasStream = canvas.captureStream(30);
    const combined = new MediaStream([
      ...canvasStream.getVideoTracks(),
      ...destNode.stream.getAudioTracks(),
    ]);
    const chosenMime = pickMimeType();
    const videoBitsPerSecond = height >= 1280 ? 6_000_000 : 3_500_000;
    try {
      recorder = new MediaRecorder(
        combined,
        chosenMime ? { mimeType: chosenMime, videoBitsPerSecond } : { videoBitsPerSecond }
      );
    } catch {
      throw new Error(
        "Your browser couldn't start a video recorder for this quality — try a lower quality setting."
      );
    }

    const chunks: Blob[] = [];
    let stopDoneResolve: () => void = () => undefined;
    const stopDone = new Promise<void>((resolve) => {
      stopDoneResolve = resolve;
    });
    let recorderFail: (err: Error) => void = () => undefined;
    const recorderFailure = new Promise<never>((_, reject) => {
      recorderFail = reject;
    });
    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };
    recorder.onstop = () => stopDoneResolve();
    recorder.onerror = (event: Event) => {
      const err = (event as { error?: DOMException }).error;
      recorderFail(
        new Error(`Recording failed${err && err.message ? `: ${err.message}` : ""}. Try a lower quality setting.`)
      );
    };

    // --- Drawing ------------------------------------------------------------
    const captionGroups = buildCaptionGroups(plan.words);
    const layoutCache = new Map<number, LayoutLine[]>();
    const fontSize = height * 0.042;
    // next/font registers Inter under a hashed family name, so a literal
    // "Inter" would fall back to system fonts on the canvas. Resolve the real
    // family the page uses once and reuse it for every canvas font.
    let fontFamily = "";
    try {
      fontFamily = getComputedStyle(document.body).fontFamily.trim();
    } catch {
      /* ignore — fall back below */
    }
    if (!fontFamily) fontFamily = "Inter, sans-serif";
    const captionFont = `700 ${fontSize}px ${fontFamily}`;
    const watermarkFont = `600 ${height * 0.021}px ${fontFamily}`;
    const padX = fontSize * 0.62;
    const padY = fontSize * 0.42;
    const lineHeight = fontSize * 1.3;
    const maxTextWidth = width * 0.86 - padX * 2;

    const track = options.track;

    const drawVideoFrame = (t: number) => {
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (vw > 0 && vh > 0) {
        // Cover-crop: scale to fill the 9:16 frame, crop the overflow.
        const scale = Math.max(width / vw, height / vh);
        const sw = width / scale;
        const sh = height / scale;
        // Horizontal: follow the crop track when there is one, else centered.
        // The track's cx is the subject center as a fraction of source width.
        const sx = track
          ? clamp(cropCenterAt(track, t) * vw - sw / 2, 0, Math.max(0, vw - sw))
          : (vw - sw) / 2;
        const sy = (vh - sh) / 2;
        ctx.drawImage(video, sx, sy, sw, sh, 0, 0, width, height);
      } else {
        ctx.fillStyle = "#000000";
        ctx.fillRect(0, 0, width, height);
      }
    };

    const drawCaptions = (t: number) => {
      let groupIndex = -1;
      for (let i = 0; i < captionGroups.length; i++) {
        const g = captionGroups[i];
        if (t >= g.start && t <= g.end) {
          groupIndex = i;
          break;
        }
        if (g.start > t) break;
      }
      if (groupIndex < 0) return; // silent stretch — no caption strip

      ctx.font = captionFont;
      let lines = layoutCache.get(groupIndex);
      if (!lines) {
        lines = layoutGroup(ctx, captionGroups[groupIndex], maxTextWidth);
        layoutCache.set(groupIndex, lines);
      }
      if (lines.length === 0) return;

      const spaceWidth = ctx.measureText(" ").width;
      const maxLineWidth = Math.max(...lines.map((l) => l.width));
      const stripW = maxLineWidth + padX * 2;
      const stripH = lines.length * lineHeight + padY * 2;
      const stripX = (width - stripW) / 2;
      const stripY = height * 0.72 - stripH / 2;

      ctx.fillStyle = "rgba(0,0,0,0.55)";
      roundedRectPath(ctx, stripX, stripY, stripW, stripH, 12);
      ctx.fill();

      // Active word: the latest word in the group whose start has passed.
      const groupWords = captionGroups[groupIndex].words;
      let activeIdx = 0;
      for (let i = 0; i < groupWords.length; i++) {
        if (groupWords[i].start <= t) activeIdx = i;
        else break;
      }

      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      let flatIdx = 0;
      for (let li = 0; li < lines.length; li++) {
        const line = lines[li];
        let x = (width - line.width) / 2;
        const y = stripY + padY + lineHeight * (li + 0.5);
        for (const word of line.words) {
          ctx.fillStyle = flatIdx === activeIdx ? activeWordColor(word) : "#ffffff";
          ctx.fillText(word.text, x, y);
          x += word.width + spaceWidth;
          flatIdx++;
        }
      }
    };

    const drawOverlays = (t: number) => {
      drawCaptions(t);

      if (options.watermark) {
        const margin = Math.round(height * 0.02);
        ctx.font = watermarkFont;
        ctx.fillStyle = "rgba(255,255,255,0.55)";
        ctx.textAlign = "right";
        ctx.textBaseline = "bottom";
        ctx.fillText("⚡ ClipCatalyst", width - margin, height - margin);
      }

      ctx.fillStyle = BRAND_VIOLET;
      ctx.fillRect(0, height - PROGRESS_BAR_PX, width * clamp01(t / clipDuration), PROGRESS_BAR_PX);
    };

    // Let webfonts finish loading before the first recorded frame, raced with
    // a short timeout so a stuck FontFaceSet can never hang the render.
    if (typeof document.fonts !== "undefined") {
      await Promise.race([
        document.fonts.ready.then(() => undefined).catch(() => undefined),
        new Promise<void>((resolve) => setTimeout(resolve, FONTS_READY_TIMEOUT_MS)),
      ]);
    }

    // Paint the first frame before recording starts so clips never open black.
    drawVideoFrame(0);
    drawOverlays(0);
    onProgress?.(0);

    // --- Record: play → rAF draw loop → stop at plan.end --------------------
    // A 'waiting' (network/decode stall) is fine: the loop keeps running and
    // simply repaints the last decoded frame until playback resumes.
    video.addEventListener("waiting", () => undefined);

    recorder.start(200);

    const playbackLoop = new Promise<void>((resolve, reject) => {
      let finished = false;
      const watchdog = setTimeout(() => {
        finish(new Error("Rendering timed out — the video stopped playing back."));
      }, clipDuration * 3000 + 30_000);
      const reachedEnd = () => video.currentTime >= endTarget || video.ended;
      const drawNow = () => {
        const t = video.currentTime - plan.start;
        try {
          drawVideoFrame(t);
          drawOverlays(t);
        } catch {
          // Stalled decoder — keep the last painted frame and carry on.
        }
        onProgress?.(clamp01(t / clipDuration));
      };
      const finish = (err?: Error) => {
        if (finished || stopRequested) return;
        finished = true;
        clearTimeout(watchdog);
        cancelAnimationFrame(rafId);
        if (tickIntervalId !== undefined) {
          clearInterval(tickIntervalId);
          tickIntervalId = undefined;
        }
        video.removeEventListener("timeupdate", onTimeUpdate);
        if (err) reject(err);
        else resolve();
      };
      // rAF freezes in hidden tabs while the video + recorder keep running, so
      // the stop condition can't live in the rAF loop alone. Media events keep
      // firing when hidden — end the clip from 'timeupdate' too.
      const onTimeUpdate = () => {
        if (finished || stopRequested) return;
        if (reachedEnd()) {
          drawNow();
          finish();
        }
      };
      video.addEventListener("timeupdate", onTimeUpdate);
      // Belt-and-braces fallback for hidden tabs: coarse timer keeps painting
      // frames and checking the stop condition (cleared in finish + cleanup).
      tickIntervalId = setInterval(() => {
        if (finished || stopRequested) return;
        drawNow();
        if (reachedEnd()) finish();
      }, BACKGROUND_TICK_MS);
      video.addEventListener("error", () =>
        finish(new Error("Video playback failed during rendering."))
      );
      const tick = () => {
        if (finished) return;
        drawNow();
        if (reachedEnd()) {
          finish();
          return;
        }
        rafId = requestAnimationFrame(tick);
      };
      video.play().then(
        () => {
          rafId = requestAnimationFrame(tick);
        },
        () => {
          finish(
            new Error(
              "Your browser blocked the silent playback rendering needs. Click into the page and try again."
            )
          );
        }
      );
    });

    await Promise.race([playbackLoop, recorderFailure]);

    // --- Stop + collect (guarded against double-stop) -----------------------
    try {
      video.pause();
    } catch {
      /* ignore */
    }
    if (!stopRequested) {
      stopRequested = true;
      if (recorder.state !== "inactive") {
        try {
          recorder.requestData();
        } catch {
          /* ignore */
        }
        recorder.stop();
      } else {
        stopDoneResolve();
      }
    }
    await Promise.race([
      stopDone,
      recorderFailure,
      new Promise<never>((_, reject) =>
        setTimeout(
          () => reject(new Error("The recorder never finished writing the clip. Please try again.")),
          STOP_TIMEOUT_MS
        )
      ),
    ]);

    const mimeType = recorder.mimeType || chosenMime || "video/webm";
    const blob = new Blob(chunks, { type: mimeType });
    if (blob.size === 0) {
      throw new Error(
        "Rendering produced an empty file — your browser may not support recording at this quality. Try a lower quality or a Chromium-based browser."
      );
    }
    onProgress?.(1);
    const extension = mimeType.includes("mp4") ? "mp4" : "webm";
    return { blob, mimeType, extension };
  } finally {
    cleanup();
  }
}
