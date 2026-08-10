// Browser-only audio utilities for the Studio pipeline.
//
// decodeToMono16k: File → mono 16 kHz Float32 PCM. Decodes with a regular
// AudioContext, then downmixes/resamples through an OfflineAudioContext.
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

const DECODE_ERROR_MESSAGE =
  "Couldn't read this file's audio. The video may use an unsupported codec, " +
  "be DRM-protected, or contain no audio track — try re-exporting it as a " +
  "standard MP4 (H.264 video + AAC audio).";

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
  const arrayBuffer = await file.arrayBuffer();

  // 1) Decode at the context's native sample rate.
  const decodeContext = new AudioContextCtor();
  let decoded: AudioBuffer;
  try {
    decoded = await decodeContext.decodeAudioData(arrayBuffer);
  } catch {
    throw new Error(DECODE_ERROR_MESSAGE);
  } finally {
    // Free the underlying audio device/thread whether or not decode worked.
    await decodeContext.close().catch(() => undefined);
  }

  if (decoded.duration <= 0 || decoded.numberOfChannels === 0) {
    throw new Error(DECODE_ERROR_MESSAGE);
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
