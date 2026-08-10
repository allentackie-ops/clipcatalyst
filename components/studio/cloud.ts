// Tiny typed client for the ClipCatalyst Cloud API (api/ARCHITECTURE.md).
// Flow: createJob → uploadSource (XHR PUT for real progress) → startJob →
// pollJob until done/failed. toFinishedClips maps the server's clips into
// the FinishedClip view shape the done grid already renders.

import type { FinishedClip } from "@/lib/studio/types";

/** Cloud API origin, e.g. "https://api.clipcatalyst.io". "" = cloud off. */
export const CLOUD_API = (process.env.NEXT_PUBLIC_CLOUD_API ?? "").replace(
  /\/$/,
  ""
);

/** True when a cloud API base is configured at build time. */
export const cloudEnabled = CLOUD_API !== "";

export type JobStatus =
  | "awaiting_upload"
  | "queued"
  | "processing"
  | "done"
  | "failed";

/** Processing sub-stages persisted on the job row (mirrors the browser). */
export type CloudStage =
  | "probe"
  | "transcribe"
  | "diarize"
  | "analyze"
  | "reframe"
  | "render";

export type CreateJobRequest = {
  filename: string;
  size_bytes: number;
  target_length: number;
  count: number;
  height: number;
};

export type CreateJobResponse = {
  job_id: string;
  upload: { mode: "put"; url: string };
};

/** ClipOut from GET /v1/jobs/{id}. Times in seconds; url may be relative. */
export type CloudClip = {
  id: string;
  index: number;
  score: number;
  title: string;
  hooks: string[];
  reason: string;
  tip: string;
  start: number;
  end: number;
  duration: number;
  url: string;
  width: number;
  height: number;
  /**
   * Distinct diarized speakers in the clip (server-computed; 0 = diarization
   * off/failed or one voice). Optional so older servers still parse.
   */
  speaker_count?: number;
};

export type CloudJob = {
  job_id: string;
  status: JobStatus;
  stage: CloudStage | "" | null;
  /** 0..1 within the current stage; -1 = indeterminate. */
  progress: number;
  detail: string;
  error: string | null;
  clips: CloudClip[];
};

/** Resolve a possibly-relative API url (local mode) against the API base. */
function resolveUrl(base: string, url: string): string {
  return /^https?:\/\//.test(url) ? url : `${base}${url}`;
}

function abortError(): DOMException {
  return new DOMException("The cloud run was canceled.", "AbortError");
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new Error(
      "Couldn't reach the ClipCatalyst cloud — check your connection and try again."
    );
  }
  if (!res.ok) {
    // FastAPI errors arrive as {detail: "..."} — surface them when present.
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error body; fall through to the status-code message.
    }
    throw new Error(detail || `Cloud request failed (${res.status}).`);
  }
  return (await res.json()) as T;
}

/** POST /v1/jobs — register the job, get the upload target. */
export function createJob(
  base: string,
  req: CreateJobRequest
): Promise<CreateJobResponse> {
  return requestJson<CreateJobResponse>(`${base}/v1/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

/**
 * PUT the raw video bytes to the job's upload url. XMLHttpRequest instead of
 * fetch because only XHR reports real upload progress. `uploadUrl` is the
 * url from createJob (relative in local mode, presigned S3 in s3 mode);
 * omitted, it defaults to the local-mode path.
 */
export function uploadSource(
  base: string,
  jobId: string,
  file: File,
  onProgress: (fraction: number) => void,
  opts?: { uploadUrl?: string; signal?: AbortSignal }
): Promise<void> {
  return new Promise((resolve, reject) => {
    const signal = opts?.signal;
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    const settle = () => signal?.removeEventListener("abort", onAbort);
    signal?.addEventListener("abort", onAbort, { once: true });

    xhr.open("PUT", resolveUrl(base, opts?.uploadUrl ?? `/v1/uploads/${jobId}`));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      settle();
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(new Error(`Upload failed (${xhr.status}). Try again.`));
      }
    };
    xhr.onerror = () => {
      settle();
      reject(
        new Error("Upload failed — check your connection and try again.")
      );
    };
    xhr.onabort = () => {
      settle();
      reject(abortError());
    };
    xhr.send(file);
  });
}

/** POST /v1/jobs/{id}/start — validate the upload and enqueue the worker. */
export function startJob(
  base: string,
  jobId: string
): Promise<{ job_id: string; status: JobStatus }> {
  return requestJson<{ job_id: string; status: JobStatus }>(
    `${base}/v1/jobs/${jobId}/start`,
    { method: "POST" }
  );
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

const POLL_INTERVAL_MS = 1500;
/** Jobs run for minutes — ride out a few blips before giving up. */
const POLL_MAX_CONSECUTIVE_FAILURES = 4;

/**
 * GET /v1/jobs/{id} every 1.5 s, reporting each snapshot via onUpdate, until
 * the job reaches done or failed (resolves with that final job). Rejects
 * with an AbortError when `signal` aborts.
 */
export async function pollJob(
  base: string,
  jobId: string,
  onUpdate: (job: CloudJob) => void,
  signal?: AbortSignal
): Promise<CloudJob> {
  let failures = 0;
  for (;;) {
    try {
      const job = await requestJson<CloudJob>(`${base}/v1/jobs/${jobId}`, {
        signal,
      });
      failures = 0;
      onUpdate(job);
      if (job.status === "done" || job.status === "failed") return job;
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      failures += 1;
      if (failures >= POLL_MAX_CONSECUTIVE_FAILURES) throw e;
    }
    await sleep(POLL_INTERVAL_MS, signal);
  }
}

/**
 * FinishedClip plus the server-computed speaker count. Cloud clips carry no
 * words, so ClipCard can't derive the count locally — the server reports it.
 */
export type CloudFinishedClip = FinishedClip & { speakerCount?: number };

/**
 * Map server clips into the FinishedClip shape ClipCard renders: absolute
 * url, mp4 container, duration via end - start. The server never reports
 * file size, so `blob` is a size-only stand-in — ClipCard reads nothing but
 * `blob.size`, and formatBytes(NaN) renders "—".
 */
export function toFinishedClips(
  base: string,
  clips: CloudClip[]
): CloudFinishedClip[] {
  return clips.map((clip) => ({
    id: clip.id,
    start: clip.start,
    end: clip.end,
    score: clip.score,
    title: clip.title,
    hooks: clip.hooks,
    reason: clip.reason,
    tip: clip.tip,
    words: [],
    url: resolveUrl(base, clip.url),
    mimeType: "video/mp4",
    extension: "mp4",
    blob: { size: Number.NaN } as unknown as Blob,
    speakerCount: clip.speaker_count,
  }));
}
