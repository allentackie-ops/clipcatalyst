// Browser-only audio utilities for the Studio pipeline.
//
// decodeToMono16k: File → mono 16 kHz Float32 PCM. Decodes with a regular
// AudioContext, then downmixes/resamples through an OfflineAudioContext.
// When the decode fails it re-probes the file with a <video> element so the
// error names the real cause (see "Decode failure taxonomy" below) instead of
// blaming the user's file for the browser's limits.
// computeAudioFeatures: per-hop RMS loudness normalized against the 95th
// percentile, plus silence spans useful for snapping cut points.
//
// These functions are only ever called from a client hook — nothing here
// touches window/AudioContext at module evaluation time, so the module is
// safe to import during SSR.

import type { AudioFeatures } from "./types";

const TARGET_SAMPLE_RATE = 16000;
const HOP_SECONDS = 0.05;
const SILENCE_THRESHOLD = 0.08;
const SILENCE_MIN_SECONDS = 0.35;
const SILENCE_MERGE_GAP_SECONDS = 0.1;

// ---- Decode failure taxonomy ----------------------------------------------
//
// decodeAudioData rejects for at least five materially different reasons, and
// the DOMException it throws is not reliably distinguishable between engines.
// Funnelling all of them into one "unsupported codec / DRM / no audio track"
// sentence blamed the user's FILE for what is usually the BROWSER's limit:
// WebKit (Safari, and therefore every browser on iOS) plays an MP4/MOV
// happily but will not hand that container's audio track to Web Audio at all.
//
// So on failure we ask the platform a second, cheap question with a plain
// <video> element — "can you open this file, and does it report audio?" — and
// let the answer pick the message. Every branch below is best-effort: the
// probe cannot hang (it times out), cannot leak its object URL, and cannot
// throw, because it runs while another error is already on its way out.

/** The browser can't open the file either — a genuine file problem. */
const FILE_UNREADABLE_MESSAGE =
  "Couldn't read this file's audio. The video may use an unsupported codec, " +
  "be DRM-protected, or contain no audio track — try re-exporting it as a " +
  "standard MP4 (H.264 video + AAC audio).";

/** The browser played the file but won't decode its audio (WebKit/iOS). */
const BROWSER_LIMIT_MESSAGE =
  "This browser can't extract audio from video files — Studio's on-device " +
  "engine needs Chrome, Edge, or Firefox on a desktop. Your video is fine: " +
  "this browser opened and played it, it just won't hand the audio track to " +
  "the page.";

/** The file opened and the browser positively reports no audio track. */
const NO_AUDIO_TRACK_MESSAGE =
  "This video plays, but the browser reports no audio track in it — Studio " +
  "needs speech to find clips. If you're sure it has sound, re-export it as " +
  "a standard MP4 (H.264 video + AAC audio) and try again.";

/** The probe itself gave no answer — say so instead of guessing. */
const INCONCLUSIVE_MESSAGE =
  "Couldn't read this file's audio, and this browser wouldn't say why. Try " +
  "re-exporting it as a standard MP4 (H.264 video + AAC audio) — or open " +
  "Studio in Chrome, Edge, or Firefox on a desktop.";

/** The file could not be read off disk at all (moved, or still syncing). */
const FILE_GONE_MESSAGE =
  "Couldn't read that file off disk — it may have been moved, renamed, or " +
  "still be syncing from cloud storage. Pick it again.";

function memoryMessage(bytes: number): string {
  return (
    `This video is ${formatFileSize(bytes)} and the browser ran out of ` +
    "memory decoding its audio. Export a shorter cut or a lower-bitrate " +
    "version (720p is plenty) and try again."
  );
}

/**
 * Above this, an otherwise-unexplained decode failure on a file the browser
 * can play is far more likely to be memory than codec support: decoded PCM
 * runs ~1.4 GB per hour of 48 kHz stereo, on top of the file itself.
 */
const MEMORY_SUSPECT_BYTES = 600_000_000;

/** How long the fallback <video> probe may take before we give up on it. */
const PROBE_TIMEOUT_MS = 4000;

/** Bytes → "820 KB" / "480 MB" / "1.2 GB", for use inside a sentence. */
function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "an unknown size";
  const kb = bytes / 1024;
  // Never round a real file down to "0 MB" — an OOM on a small file is rare
  // but the sentence still has to be true.
  if (kb < 1024) return `${Math.max(1, Math.round(kb))} KB`;
  const mb = kb / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

const MEMORY_PATTERN =
  /out of memory|allocation failed|allocation size|array buffer allocation|not enough memory|memory limit|quotaexceeded/i;

/** Does this rejection look like an allocation failure rather than a codec? */
function looksLikeMemoryFailure(error: unknown): boolean {
  if (error instanceof RangeError) return true;
  if (!(error instanceof Error)) return false;
  if (error.name === "RangeError" || error.name === "QuotaExceededError") {
    return true;
  }
  return MEMORY_PATTERN.test(`${error.name} ${error.message}`);
}

type SourceProbe = {
  /** true: metadata loaded. false: the element errored. null: no answer. */
  playable: boolean | null;
  /** true/false when the engine exposes a track signal, else null. */
  hasAudio: boolean | null;
};

/** No engine agrees on how to expose "does this file have audio". */
type AudioSignalElement = HTMLVideoElement & {
  audioTracks?: { length: number };
  mozHasAudio?: boolean;
  webkitAudioDecodedByteCount?: number;
};

/**
 * Read whatever audio-presence signal this engine offers at metadata time.
 * WebKit implements AudioTrackList (the case that matters most here), Firefox
 * has mozHasAudio, Chromium exposes only a decoded-byte counter that is still
 * 0 before playback — so Chromium usually answers "unknown", which is honest.
 */
function readAudioSignal(video: HTMLVideoElement): boolean | null {
  const el = video as AudioSignalElement;
  try {
    if (el.audioTracks && typeof el.audioTracks.length === "number") {
      return el.audioTracks.length > 0;
    }
    if (typeof el.mozHasAudio === "boolean") return el.mozHasAudio;
    if (
      typeof el.webkitAudioDecodedByteCount === "number" &&
      el.webkitAudioDecodedByteCount > 0
    ) {
      return true;
    }
  } catch {
    // Touching an exotic property must never outrank the real error.
  }
  return null;
}

/**
 * Ask the media stack whether it can open this file at all. Never rejects;
 * always revokes its object URL; always settles within PROBE_TIMEOUT_MS.
 */
function probeSource(file: File): Promise<SourceProbe> {
  return new Promise<SourceProbe>((resolve) => {
    if (typeof document === "undefined" || typeof URL === "undefined") {
      resolve({ playable: null, hasAudio: null });
      return;
    }
    let settled = false;
    let url = "";
    let timer = 0;
    let video: HTMLVideoElement | null = null;

    const finish = (result: SourceProbe) => {
      if (settled) return;
      settled = true;
      try {
        if (timer) window.clearTimeout(timer);
        if (video) {
          video.onloadedmetadata = null;
          video.onerror = null;
          video.removeAttribute("src");
          video.load();
        }
      } catch {
        // Teardown is best-effort; the URL is revoked either way below.
      } finally {
        if (url) URL.revokeObjectURL(url);
      }
      resolve(result);
    };

    try {
      url = URL.createObjectURL(file);
      video = document.createElement("video");
      video.preload = "metadata";
      video.muted = true;
      // iOS refuses inline media without this and can escalate to fullscreen.
      video.playsInline = true;
      timer = window.setTimeout(
        () => finish({ playable: null, hasAudio: null }),
        PROBE_TIMEOUT_MS
      );
      video.onloadedmetadata = () => {
        finish({
          playable: true,
          hasAudio: video ? readAudioSignal(video) : null,
        });
      };
      video.onerror = () => finish({ playable: false, hasAudio: null });
      video.src = url;
    } catch {
      finish({ playable: null, hasAudio: null });
    }
  });
}

/**
 * Choose the message for a decode that produced no usable audio.
 *
 * `sizeHeuristic` is on when decodeAudioData actually rejected (a huge file
 * that the browser can otherwise play is a memory story), and off when it
 * "succeeded" and returned an empty buffer — that shape is about the audio
 * track, never about size.
 */
async function describeDecodeFailure(
  file: File,
  error: unknown,
  { sizeHeuristic }: { sizeHeuristic: boolean }
): Promise<string> {
  if (looksLikeMemoryFailure(error)) return memoryMessage(file.size);

  const probe = await probeSource(file);
  if (probe.playable === false) return FILE_UNREADABLE_MESSAGE;
  if (probe.playable === null) return INCONCLUSIVE_MESSAGE;
  if (probe.hasAudio === false) return NO_AUDIO_TRACK_MESSAGE;
  if (sizeHeuristic && file.size >= MEMORY_SUSPECT_BYTES) {
    return memoryMessage(file.size);
  }
  // The browser opened the file, plays it, and (as far as it will say) it has
  // audio — yet Web Audio refused it. That is the browser's limit, not the
  // user's file.
  return BROWSER_LIMIT_MESSAGE;
}

type AudioContextConstructor = new () => AudioContext;

/** Resolve the AudioContext constructor, tolerating old WebKit prefixes. */
function getAudioContextConstructor(): AudioContextConstructor | null {
  if (typeof window === "undefined") return null;
  const w = window as Window &
    typeof globalThis & { webkitAudioContext?: AudioContextConstructor };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/**
 * Decode a video/audio File to mono 16 kHz PCM.
 *
 * Reads the whole file into memory (files this size fit comfortably — the UI
 * caps sources at 20 minutes), decodes the audio track at the browser's
 * native rate, then renders it through an OfflineAudioContext to downmix to
 * one channel and resample to 16 kHz for Whisper.
 */
export async function decodeToMono16k(
  file: File,
  onProgress?: (progress: number) => void
): Promise<{ pcm: Float32Array; duration: number }> {
  const AudioContextCtor = getAudioContextConstructor();
  if (!AudioContextCtor) {
    throw new Error(
      "This browser can't process audio (Web Audio API missing). Try a modern desktop browser like Chrome, Edge, or Firefox."
    );
  }
  if (file.size === 0) {
    throw new Error("That file is empty — pick a video that contains audio.");
  }

  // Reading a File into an ArrayBuffer has no granular progress events worth
  // streaming for; report a single indeterminate tick instead.
  onProgress?.(-1);
  let arrayBuffer: ArrayBuffer;
  try {
    arrayBuffer = await file.arrayBuffer();
  } catch (error) {
    // Reading the bytes is the first thing a huge file kills, and this used
    // to escape raw into the UI (where the "model download" rewriter in
    // StudioApp could mislabel it as a network problem).
    throw new Error(
      looksLikeMemoryFailure(error) ? memoryMessage(file.size) : FILE_GONE_MESSAGE
    );
  }

  // 1) Decode at the context's native sample rate.
  const decodeContext = new AudioContextCtor();
  let decoded: AudioBuffer | undefined;
  let decodeError: unknown = null;
  try {
    decoded = await decodeContext.decodeAudioData(arrayBuffer);
  } catch (error) {
    // Hold the cause rather than throwing here: choosing the message needs an
    // await, and this block still owes the audio device a close().
    decodeError = error ?? new Error("decodeAudioData rejected");
  } finally {
    // Free the underlying audio device/thread whether or not decode worked.
    // A throw in here would REPLACE the real failure on its way out — old
    // prefixed webkitAudioContext builds have no close() at all — so the call
    // is wrapped as well as the promise.
    try {
      await decodeContext.close();
    } catch {
      // Nothing actionable; the context is dropped either way.
    }
  }

  // Falsy covers both a rejected promise and the legacy callback-style
  // decodeAudioData that returns undefined instead of a buffer.
  if (!decoded) {
    throw new Error(
      await describeDecodeFailure(file, decodeError, { sizeHeuristic: true })
    );
  }

  if (decoded.duration <= 0 || decoded.numberOfChannels === 0) {
    // "Succeeded" and handed back nothing: the shape you get from an engine
    // that opens the container but ignores the audio track inside it.
    throw new Error(
      await describeDecodeFailure(file, null, { sizeHeuristic: false })
    );
  }

  // 2) Downmix + resample to mono 16 kHz. OfflineAudioContext handles both
  // (channel interpretation "speakers" mixes N channels down to 1) and needs
  // no explicit close — it is garbage-collected after rendering.
  const targetLength = Math.max(
    1,
    Math.ceil((decoded.length * TARGET_SAMPLE_RATE) / decoded.sampleRate)
  );
  const offline = new OfflineAudioContext(1, targetLength, TARGET_SAMPLE_RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start(0);
  const rendered = await offline.startRendering();
  source.disconnect();

  // Copy out of the AudioBuffer into a plain Float32Array: the caller
  // transfers this buffer to the transcription worker, and buffers owned by
  // an AudioBuffer are not reliably transferable.
  const pcm = new Float32Array(rendered.length);
  rendered.copyFromChannel(pcm, 0);

  onProgress?.(1);
  return { pcm, duration: decoded.duration };
}

/**
 * Loudness features over the decoded PCM: RMS per 0.05 s hop normalized so
 * the 95th percentile maps to 1 (clamped 0..1), and silence spans where the
 * normalized RMS stays below 0.08 for at least 0.35 s (nearby spans merged
 * across gaps of up to 0.1 s).
 */
export function computeAudioFeatures(
  pcm: Float32Array,
  sampleRate: number
): AudioFeatures {
  const hopSize = Math.max(1, Math.round(sampleRate * HOP_SECONDS));
  const hopCount = Math.ceil(pcm.length / hopSize);
  const rms = new Float32Array(hopCount);

  for (let i = 0; i < hopCount; i++) {
    const from = i * hopSize;
    const to = Math.min(from + hopSize, pcm.length);
    let sumSquares = 0;
    for (let j = from; j < to; j++) {
      const s = pcm[j];
      sumSquares += s * s;
    }
    rms[i] = Math.sqrt(sumSquares / Math.max(1, to - from));
  }

  // Normalize so the 95th percentile maps to 1. Fall back to the max (then
  // to 1) for degenerate, near-silent inputs to avoid dividing by zero.
  if (hopCount > 0) {
    const sorted = Array.from(rms).sort((a, b) => a - b);
    const p95 = sorted[Math.floor(0.95 * (sorted.length - 1))];
    const max = sorted[sorted.length - 1];
    const scale = p95 > 0 ? p95 : max > 0 ? max : 1;
    for (let i = 0; i < hopCount; i++) {
      rms[i] = Math.min(1, Math.max(0, rms[i] / scale));
    }
  }

  const totalSeconds = pcm.length / sampleRate;

  // Runs of consecutive below-threshold hops.
  const rawSpans: { start: number; end: number }[] = [];
  let runStart = -1;
  for (let i = 0; i <= hopCount; i++) {
    const quiet = i < hopCount && rms[i] < SILENCE_THRESHOLD;
    if (quiet && runStart === -1) {
      runStart = i;
    } else if (!quiet && runStart !== -1) {
      rawSpans.push({
        start: runStart * HOP_SECONDS,
        end: Math.min(i * HOP_SECONDS, totalSeconds),
      });
      runStart = -1;
    }
  }

  // Merge spans separated by gaps of ≤ 0.1 s, then keep only spans that are
  // sustained for at least 0.35 s.
  const merged: { start: number; end: number }[] = [];
  for (const span of rawSpans) {
    const last = merged[merged.length - 1];
    if (last && span.start - last.end <= SILENCE_MERGE_GAP_SECONDS) {
      last.end = span.end;
    } else {
      merged.push({ ...span });
    }
  }
  const silences = merged.filter(
    (s) => s.end - s.start >= SILENCE_MIN_SECONDS
  );

  return { rms, hopSeconds: HOP_SECONDS, silences };
}
