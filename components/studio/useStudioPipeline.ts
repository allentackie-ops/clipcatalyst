"use client";

// Orchestrates the in-browser clipping pipeline:
// decode audio → transcribe (worker) → plan highlights → render clips.

import { useCallback, useEffect, useRef, useState } from "react";
import { decodeToMono16k, computeAudioFeatures } from "@/lib/studio/audio";
import { planClips } from "@/lib/studio/highlights";
import { renderClip } from "@/lib/studio/render";
import { MAX_SOURCE_SECONDS, formatDuration } from "./format";
import type {
  FinishedClip,
  HighlightOptions,
  PipelineProgress,
  RenderOptions,
  Transcript,
  WorkerResponse,
} from "@/lib/studio/types";

export type StudioSettings = HighlightOptions & RenderOptions;

export type StudioState =
  | { status: "idle" }
  | { status: "running"; progress: PipelineProgress }
  | {
      status: "done";
      clips: FinishedClip[];
      sourceUrl: string;
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

  // Unmount: stop the worker, drop every object URL, and silence any
  // in-flight pipeline (all setStates below check abortedRef).
  useEffect(() => {
    abortedRef.current = false;
    return () => {
      abortedRef.current = true;
      workerRef.current?.terminate();
      workerRef.current = null;
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

        onProgress({ stage: "analyze", progress: -1, detail: "Scoring every moment" });
        const plans = planClips(transcript, features, {
          targetLength: settings.targetLength,
          count: settings.count,
        });
        if (plans.length === 0) {
          throw new Error("No clip-worthy moments found — try a longer or more talkative video.");
        }
        if (abortedRef.current) return;

        // Render each clip independently: one bad render shouldn't sink the batch.
        const clips: FinishedClip[] = [];
        let lastRenderError: unknown = null;
        for (let i = 0; i < plans.length; i++) {
          if (abortedRef.current) return;
          const plan = plans[i];
          try {
            const result = await renderClip(
              { url: sourceUrl },
              plan,
              { height: settings.height, watermark: settings.watermark },
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
            clips.push({ ...plan, ...result, url });
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
          failedCount: plans.length - clips.length,
        });
      } catch (e) {
        safeSetState({
          status: "error",
          message: e instanceof Error ? e.message : "Something went wrong while processing.",
        });
      } finally {
        runningRef.current = false;
      }
    },
    [safeSetState]
  );

  return { state, run, reset };
}
