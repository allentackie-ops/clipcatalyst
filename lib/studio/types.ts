// Shared contracts for the in-browser clipping pipeline ("Studio").
// All times are seconds relative to the start of the source video.

export type TranscriptWord = {
  text: string; // includes leading space where whisper provides one
  start: number;
  end: number;
};

export type Transcript = {
  words: TranscriptWord[];
  text: string;
};

export type Sentence = {
  text: string;
  start: number;
  end: number;
  words: TranscriptWord[];
};

/** Per-hop loudness features from the decoded audio. */
export type AudioFeatures = {
  /** RMS energy per hop, normalized 0..1 against the 95th percentile. */
  rms: Float32Array;
  /** Seconds per hop (e.g. 0.05). */
  hopSeconds: number;
  /** Detected silence spans, useful for snapping cut points. */
  silences: { start: number; end: number }[];
};

export type ClipPlan = {
  id: string;
  start: number;
  end: number;
  /** 0-100 virality-style score. */
  score: number;
  /** Short human title derived from the content. */
  title: string;
  /** Up to 5 hook lines extracted from the transcript, strongest first. */
  hooks: string[];
  /** One-line explanation of why this moment scored well. */
  reason: string;
  /** Actionable tip to push the score higher. */
  tip: string;
  /** Words falling inside [start, end], re-based so captions can render. */
  words: TranscriptWord[];
};

export type HighlightOptions = {
  /** Target clip length in seconds (15 | 30 | 60). */
  targetLength: number;
  /** How many clips to produce (1-3). */
  count: number;
};

export type RenderOptions = {
  /** Output height; width is derived for 9:16. 1280 → 720x1280. */
  height: 960 | 1280 | 1920;
  /** Draw the small ClipCatalyst watermark (free tier behavior). */
  watermark: boolean;
};

export type RenderResult = {
  blob: Blob;
  mimeType: string;
  /** File extension matching the container ("mp4" | "webm"). */
  extension: string;
};

export type StageId =
  | "read"
  | "model"
  | "transcribe"
  | "analyze"
  | "render"
  | "done";

export type PipelineProgress = {
  stage: StageId;
  /** 0..1 within the current stage; -1 = indeterminate. */
  progress: number;
  /** Short human-readable status line, e.g. "Transcribing 04:12 / 09:30". */
  detail?: string;
  /** For the render stage: which clip (1-based) of how many. */
  clipIndex?: number;
  clipCount?: number;
};

export type FinishedClip = ClipPlan & RenderResult & { url: string };

// ---- Worker protocol (transcribe.worker.ts) ----

export type WorkerRequest = {
  type: "transcribe";
  /** Mono PCM at 16 kHz. Transferred, not copied. */
  audio: Float32Array;
  language?: string;
};

export type WorkerResponse =
  | { type: "model-progress"; progress: number; detail: string }
  | { type: "transcribe-progress"; progress: number; detail: string }
  | { type: "result"; transcript: Transcript }
  | { type: "error"; message: string };
