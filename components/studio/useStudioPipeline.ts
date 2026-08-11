"use client";

// Orchestrates the in-browser clipping pipeline: decode audio → transcribe
// (worker) → diarize speakers → plan highlights → reframe → render clips.

import { useCallback, useEffect, useRef, useState } from "react";
import { decodeToMono16k, computeAudioFeatures } from "@/lib/studio/audio";
import { EMPTY_KIT, loadKit } from "@/lib/studio/brandkit";
import type { BrandKit } from "@/lib/studio/brandkit";
import { buildCropTrack } from "@/lib/studio/croptrack";
import type { CropTrack } from "@/lib/studio/croptrack";
import { assignSpeakers, buildSpeechSegments } from "@/lib/studio/diarize";
import { detectFaces } from "@/lib/studio/facetrack";
import { planClips } from "@/lib/studio/highlights";
import { renderClip } from "@/lib/studio/render";
import {
  computeMfccFrames,
  segmentEmbeddingsFromFrames,
} from "@/lib/studio/speakerfeats";
import type { MfccFrames } from "@/lib/studio/speakerfeats";
import { MAX_SOURCE_SECONDS, formatDuration } from "./format";
import type {
  ClipPlan,
  FinishedClip,
  HighlightOptions,
  PipelineProgress,
  RenderOptions,
  Transcript,
  TranscriptWord,
  WorkerResponse,
} from "@/lib/studio/types";

export type StudioSettings = HighlightOptions & RenderOptions;

/** A finished clip plus the camera move it was rendered with — editor
 *  re-renders replay the same track so the crop stays on the speaker. */
export type StudioClip = FinishedClip & { track?: CropTrack };

export type StudioState =
  | { status: "idle" }
  | { status: "running"; progress: PipelineProgress }
  | {
      status: "done";
      clips: StudioClip[];
      sourceUrl: string;
      /** Decoded audio duration — the editor's extension hard bound. */
      sourceDuration: number;
      /** Full transcript, absolute times, with diarized speaker labels. */
      transcript: Transcript;
      /** What the clips were rendered with — re-renders must match. */
      renderOptions: RenderOptions;
      /** The brand kit the clips were rendered with, so an edited clip
       *  re-renders with the same corner and caption colour. */
      brandKit: BrandKit;
      /** Planned clips that failed to render (0 normally). */
      failedCount: number;
    }
  | { status: "error"; message: string };

declare global {
  interface Window {
    /** E2E hook: bypasses Whisper with a pre-baked transcript. */
    __CC_TEST_TRANSCRIPT?: Transcript;
  }
}

/** Output aspect of every clip (9:16). Sets how wide the crop window is. */
const TARGET_ASPECT = 9 / 16;
/** Used when the source's intrinsic size can't be read — the common case. */
const DEFAULT_SOURCE_ASPECT = 16 / 9;
const METADATA_TIMEOUT_MS = 15_000;

/**
 * Intrinsic width/height of the source, needed to size the crop window.
 *
 * Resolves to 16:9 on any failure or timeout rather than rejecting: an
 * unreadable aspect must not stop the run, it just means a slightly
 * mis-sized pan range that `renderClip` clamps anyway.
 */
function probeSourceAspect(url: string): Promise<number> {
  return new Promise((resolve) => {
    if (typeof document === "undefined") {
      resolve(DEFAULT_SOURCE_ASPECT);
      return;
    }
    const video = document.createElement("video");
    let settled = false;
    const finish = (aspect: number) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        /* ignore */
      }
      resolve(aspect > 0 && Number.isFinite(aspect) ? aspect : DEFAULT_SOURCE_ASPECT);
    };
    const timer = setTimeout(() => finish(DEFAULT_SOURCE_ASPECT), METADATA_TIMEOUT_MS);
    video.preload = "metadata";
    video.muted = true;
    video.onloadedmetadata = () => finish(video.videoWidth / video.videoHeight);
    video.onerror = () => finish(DEFAULT_SOURCE_ASPECT);
    video.src = url;
  });
}

/**
 * Face-track one clip, degrading to a centered crop on any trouble.
 *
 * `detectFaces` already swallows its own failures (returning `[]`, which
 * `buildCropTrack` turns into a centered track); the try/catch here is the
 * backstop for anything unexpected. Reframing is an enhancement — it must
 * never be the reason a clip doesn't get rendered.
 */
async function trackForClip(
  sourceUrl: string,
  plan: ClipPlan,
  sourceAspect: number,
  onProgress: (progress: number) => void,
  signal: AbortSignal
): Promise<CropTrack | undefined> {
  try {
    const samples = await detectFaces(
      { url: sourceUrl },
      plan,
      onProgress,
      signal
    );
    return buildCropTrack(samples, {
      duration: Math.max(0.1, plan.end - plan.start),
      targetAspect: TARGET_ASPECT,
      sourceAspect,
    });
  } catch {
    return undefined; // undefined ⇒ renderClip keeps its centered crop
  }
}

/**
 * Best-effort speaker labels for the transcript, from MFCC frames stashed
 * before the PCM buffer was transferred away. Any failure (or a single voice
 * ruling from the shared core) still returns a valid transcript — worst case
 * every word stays unassigned and captions render exactly like today.
 */
function diarizeTranscript(
  transcript: Transcript,
  mfcc: MfccFrames | null
): Transcript {
  try {
    if (!mfcc) return transcript;
    const segments = buildSpeechSegments(transcript.words);
    const embeddings = segmentEmbeddingsFromFrames(
      mfcc.frames,
      mfcc.dims,
      mfcc.hopSeconds,
      segments
    );
    const { wordSpeakers } = assignSpeakers(transcript.words, segments, embeddings);
    // Spread copies — never mutate the transcript we were handed (it may be
    // the caller-owned __CC_TEST_TRANSCRIPT object).
    return {
      ...transcript,
      words: transcript.words.map((w, i) =>
        wordSpeakers[i] === undefined ? w : { ...w, speaker: wordSpeakers[i] }
      ),
    };
  } catch {
    return transcript;
  }
}

function transcribeInWorker(
  pcm: Float32Array,
  onProgress: (p: PipelineProgress) => void,
  workerRef: { current: Worker | null }
): Promise<Transcript> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL("../../lib/studio/transcribe.worker.ts", import.meta.url)
    );
    workerRef.current = worker;
    const settle = () => {
      worker.terminate();
      if (workerRef.current === worker) workerRef.current = null;
    };
    worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
      const msg = event.data;
      if (msg.type === "model-progress") {
        onProgress({ stage: "model", progress: msg.progress, detail: msg.detail });
      } else if (msg.type === "transcribe-progress") {
        onProgress({ stage: "transcribe", progress: msg.progress, detail: msg.detail });
      } else if (msg.type === "result") {
        settle();
        resolve(msg.transcript);
      } else if (msg.type === "error") {
        settle();
        reject(new Error(msg.message));
      }
    };
    worker.onerror = (e) => {
      settle();
      reject(new Error(e.message || "Transcription worker failed"));
    };
    worker.postMessage({ type: "transcribe", audio: pcm }, [pcm.buffer]);
  });
}

export function useStudioPipeline() {
  const [state, setState] = useState<StudioState>({ status: "idle" });
  const urlsRef = useRef<string[]>([]);
  const runningRef = useRef(false);
  const abortedRef = useRef(false);
  const workerRef = useRef<Worker | null>(null);
  const detectAbortRef = useRef<AbortController | null>(null);

  // Unmount: stop the worker, cut face detection short, drop every object URL,
  // and silence any in-flight pipeline (all setStates below check abortedRef).
  useEffect(() => {
    abortedRef.current = false;
    return () => {
      abortedRef.current = true;
      workerRef.current?.terminate();
      workerRef.current = null;
      detectAbortRef.current?.abort();
      detectAbortRef.current = null;
      for (const u of urlsRef.current) URL.revokeObjectURL(u);
      urlsRef.current = [];
    };
  }, []);

  const safeSetState = useCallback((next: StudioState) => {
    if (!abortedRef.current) setState(next);
  }, []);

  const reset = useCallback(() => {
    for (const u of urlsRef.current) URL.revokeObjectURL(u);
    urlsRef.current = [];
    safeSetState({ status: "idle" });
  }, [safeSetState]);

  const run = useCallback(
    async (file: File, settings: StudioSettings) => {
      if (runningRef.current) return;
      runningRef.current = true;

      const detectAbort = new AbortController();
      detectAbortRef.current = detectAbort;

      const onProgress = (progress: PipelineProgress) =>
        safeSetState({ status: "running", progress });

      const sourceUrl = URL.createObjectURL(file);
      urlsRef.current.push(sourceUrl);

      try {
        onProgress({ stage: "read", progress: -1, detail: "Decoding audio track" });
        const { pcm, duration } = await decodeToMono16k(file, (p) =>
          onProgress({ stage: "read", progress: p, detail: "Decoding audio track" })
        );
        // Catches files whose pick-time probe couldn't report a duration.
        if (duration > MAX_SOURCE_SECONDS) {
          throw new Error(
            `That video runs ${formatDuration(duration)} — the browser beta caps out at 20:00. Trim it down and try again.`
          );
        }
        if (duration < 5) {
          throw new Error("That video is too short to clip — give it at least a few seconds of speech.");
        }
        const features = computeAudioFeatures(pcm, 16000);
        if (abortedRef.current) return;

        // MFCC frames for diarization must be extracted NOW: transcribing
        // transfers pcm.buffer to the worker, after which the samples are
        // gone. Best-effort — on failure the run continues without speakers.
        let mfcc: MfccFrames | null = null;
        try {
          mfcc = await computeMfccFrames(pcm);
        } catch {
          mfcc = null;
        }
        if (abortedRef.current) return;

        let transcript: Transcript;
        if (typeof window !== "undefined" && window.__CC_TEST_TRANSCRIPT) {
          transcript = window.__CC_TEST_TRANSCRIPT;
        } else {
          onProgress({ stage: "model", progress: -1, detail: "Loading the AI model" });
          transcript = await transcribeInWorker(pcm, onProgress, workerRef);
        }
        if (abortedRef.current) return;
        if (transcript.words.length < 8) {
          throw new Error(
            "Couldn't find enough speech to clip. Studio needs spoken audio — music-only videos won't work yet."
          );
        }

        // --- diarize: best-effort speaker labels ---------------------------
        onProgress({ stage: "diarize", progress: -1, detail: "Listening for speakers" });
        transcript = diarizeTranscript(transcript, mfcc);
        if (abortedRef.current) return;

        onProgress({ stage: "analyze", progress: -1, detail: "Scoring every moment" });
        const plans = planClips(transcript, features, {
          targetLength: settings.targetLength,
          count: settings.count,
        });
        if (plans.length === 0) {
          throw new Error("No clip-worthy moments found — try a longer or more talkative video.");
        }
        if (abortedRef.current) return;

        // --- reframe: one camera move per clip, best-effort ----------------
        // Detection runs for every clip before any rendering starts so the
        // checklist advances once instead of ping-ponging reframe→render.
        onProgress({
          stage: "reframe",
          progress: 0,
          clipIndex: 1,
          clipCount: plans.length,
          detail: `Finding the speaker in clip 1 of ${plans.length}`,
        });
        const sourceAspect = await probeSourceAspect(sourceUrl);
        if (abortedRef.current) return;

        const tracks: (CropTrack | undefined)[] = [];
        for (let i = 0; i < plans.length; i++) {
          if (abortedRef.current) return;
          const base = i / plans.length;
          const detail = `Finding the speaker in clip ${i + 1} of ${plans.length}`;
          onProgress({
            stage: "reframe",
            progress: base,
            clipIndex: i + 1,
            clipCount: plans.length,
            detail,
          });
          tracks.push(
            await trackForClip(
              sourceUrl,
              plans[i],
              sourceAspect,
              (p) =>
                onProgress({
                  stage: "reframe",
                  progress: base + Math.min(1, Math.max(0, p)) / plans.length,
                  clipIndex: i + 1,
                  clipCount: plans.length,
                  detail,
                }),
              detectAbort.signal
            )
          );
        }
        if (abortedRef.current) return;

        // The creator's brand kit, read once here rather than per clip: every
        // clip in a batch carries the same corner. loadKit never throws and
        // never blocks — EMPTY_KIT covers a blocked or absent IndexedDB, and
        // the try/catch is the backstop, because a brand kit must never be
        // the reason a render fails. renderClip applies the plan gate itself
        // (`watermark` wins over the logo), so it is always safe to pass on.
        let brandKit: BrandKit = EMPTY_KIT;
        try {
          brandKit = await loadKit();
        } catch {
          brandKit = EMPTY_KIT;
        }
        if (abortedRef.current) return;

        // Render each clip independently: one bad render shouldn't sink the batch.
        const clips: StudioClip[] = [];
        let lastRenderError: unknown = null;
        for (let i = 0; i < plans.length; i++) {
          if (abortedRef.current) return;
          const plan = plans[i];
          try {
            const result = await renderClip(
              { url: sourceUrl },
              plan,
              {
                height: settings.height,
                watermark: settings.watermark,
                track: tracks[i],
                brandKit,
              },
              (p) =>
                onProgress({
                  stage: "render",
                  progress: p,
                  clipIndex: i + 1,
                  clipCount: plans.length,
                  detail: `Rendering clip ${i + 1} of ${plans.length}`,
                })
            );
            const url = URL.createObjectURL(result.blob);
            urlsRef.current.push(url);
            clips.push({ ...plan, ...result, url, track: tracks[i] });
          } catch (e) {
            lastRenderError = e;
          }
        }
        if (clips.length === 0) {
          throw lastRenderError instanceof Error
            ? lastRenderError
            : new Error("Rendering failed for every clip.");
        }

        safeSetState({
          status: "done",
          clips,
          sourceUrl,
          sourceDuration: duration,
          transcript,
          renderOptions: { height: settings.height, watermark: settings.watermark },
          brandKit,
          failedCount: plans.length - clips.length,
        });
      } catch (e) {
        safeSetState({
          status: "error",
          message: e instanceof Error ? e.message : "Something went wrong while processing.",
        });
      } finally {
        if (detectAbortRef.current === detectAbort) detectAbortRef.current = null;
        runningRef.current = false;
      }
    },
    [safeSetState]
  );

  return { state, run, reset };
}
