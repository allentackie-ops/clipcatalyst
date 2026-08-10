/// <reference lib="webworker" />
// Whisper transcription in a Web Worker via transformers.js.
// Model weights stream from the Hugging Face hub on first use and are
// cached by the browser afterwards.

import { pipeline, env } from "@huggingface/transformers";
import type { Transcript, TranscriptWord, WorkerRequest, WorkerResponse } from "./types";

env.allowLocalModels = false;

const MODEL_ID = "Xenova/whisper-tiny.en";
const SAMPLE_RATE = 16000;
const CHUNK_S = 30;
const STRIDE_S = 5;

const post = (msg: WorkerResponse, transfer?: Transferable[]) =>
  (self as unknown as Worker).postMessage(msg, transfer ?? []);

let asrPromise: Promise<any> | null = null;

const onModelProgress = (p: any) => {
  if (p.status === "progress" && typeof p.progress === "number") {
    post({
      type: "model-progress",
      progress: Math.min(p.progress / 100, 1),
      detail: `Downloading AI model — ${String(p.file ?? "").split("/").pop() ?? "weights"}`,
    });
  }
};

const createPipeline = (device: "webgpu" | "wasm", dtype: "fp32" | "q8") =>
  pipeline("automatic-speech-recognition", MODEL_ID, {
    device,
    dtype,
    progress_callback: onModelProgress,
  });

// navigator.gpu existing is not enough: on many machines (Linux without
// Vulkan, blocklisted GPUs) requestAdapter() resolves null and ORT would
// throw a cryptic error. Only trust WebGPU if an adapter actually appears.
async function hasUsableWebGPU(): Promise<boolean> {
  try {
    const gpu = (self as any).navigator?.gpu;
    if (!gpu) return false;
    return (await gpu.requestAdapter()) != null;
  } catch {
    return false;
  }
}

async function initAsr(): Promise<any> {
  if (await hasUsableWebGPU()) {
    try {
      return await createPipeline("webgpu", "fp32");
    } catch {
      // Adapter probe passed but the WebGPU backend still failed to stand
      // up — fall through and retry once on wasm before surfacing anything.
    }
  }
  return createPipeline("wasm", "q8");
}

async function getAsr() {
  if (!asrPromise) {
    asrPromise = initAsr().catch((e: unknown) => {
      asrPromise = null;
      throw e;
    });
  }
  return asrPromise;
}

function toTranscript(output: any): Transcript {
  const words: TranscriptWord[] = [];
  const chunks = Array.isArray(output?.chunks) ? output.chunks : [];
  let lastEnd = 0;
  for (const c of chunks) {
    const [start, end] = c.timestamp ?? [null, null];
    if (typeof start !== "number") continue;
    const safeEnd = typeof end === "number" ? end : start + 0.4;
    // Guard against occasional non-monotonic word timestamps.
    const s = Math.max(start, lastEnd);
    const e = Math.max(safeEnd, s + 0.05);
    lastEnd = e;
    words.push({ text: String(c.text ?? ""), start: s, end: e });
  }
  return { words, text: String(output?.text ?? words.map((w) => w.text).join("")) };
}

self.onmessage = async (event: MessageEvent<WorkerRequest>) => {
  const msg = event.data;
  if (msg.type !== "transcribe") return;
  try {
    const asr = await getAsr();
    const totalSeconds = msg.audio.length / SAMPLE_RATE;

    // transformers.js reports per-chunk progress through callback_function on
    // the underlying generation; chunk-level progress is enough for UI, so we
    // estimate from processed chunks.
    let processedChunks = 0;
    const totalChunks = Math.max(1, Math.ceil(totalSeconds / (CHUNK_S - STRIDE_S)));

    const output = await asr(msg.audio, {
      chunk_length_s: CHUNK_S,
      stride_length_s: STRIDE_S,
      return_timestamps: "word",
      // Called as each chunk finishes decoding.
      chunk_callback: () => {
        processedChunks += 1;
        const frac = Math.min(processedChunks / totalChunks, 1);
        const mins = (n: number) =>
          `${String(Math.floor(n / 60)).padStart(2, "0")}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
        post({
          type: "transcribe-progress",
          progress: frac,
          detail: `Transcribing ${mins(frac * totalSeconds)} / ${mins(totalSeconds)}`,
        });
      },
    });

    post({ type: "result", transcript: toTranscript(output) });
  } catch (e) {
    post({ type: "error", message: e instanceof Error ? e.message : String(e) });
  }
};
