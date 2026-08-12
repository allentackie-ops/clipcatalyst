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

// ---- Session auth hook -----------------------------------------------------
// lib/account.ts registers its token reader here at module load, so cloud
// calls carry the signed-in user's bearer (the server enforces quota/height/
// watermark per plan, and user-owned jobs 404 without it) while cloud.ts
// stays ignorant of where tokens live. Never registered → () => null →
// anonymous requests, exactly today's behaviour.

type TokenSource = () => string | null;

let tokenSource: TokenSource = () => null;

/** Register where cloud calls read the session token (see lib/account.ts). */
export function setCloudTokenSource(source: TokenSource): void {
  tokenSource = source;
}

function authHeaders(): Record<string, string> {
  const token = tokenSource();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

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

/**
 * True for a url pointing at an absolute origin — in practice a presigned S3
 * url, i.e. somewhere that is NOT our API. The session bearer must never ride
 * along to one (see uploadSource / toFinishedClips).
 */
function isAbsoluteUrl(url: string): boolean {
  return /^https?:\/\//.test(url);
}

/** Resolve a possibly-relative API url (local mode) against the API base. */
function resolveUrl(base: string, url: string): string {
  return isAbsoluteUrl(url) ? url : `${base}${url}`;
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
    headers: { "Content-Type": "application/json", ...authHeaders() },
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

    const uploadUrl = opts?.uploadUrl ?? `/v1/uploads/${jobId}`;
    xhr.open("PUT", resolveUrl(base, uploadUrl));
    // Only the API's own (relative) upload path takes the session bearer —
    // a presigned S3 url carries its auth in the query string, and adding an
    // Authorization header on top makes S3 reject the request.
    if (!isAbsoluteUrl(uploadUrl)) {
      const token = tokenSource();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }
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
    { method: "POST", headers: authHeaders() }
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
        headers: authHeaders(),
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
 * FinishedClip plus the two things only the server knows about a cloud clip:
 * the speaker count (cloud clips carry no words, so ClipCard can't derive it)
 * and `clipIndex`.
 *
 * `clipIndex` is the clip's index WITHIN ITS JOB, which is not its position in
 * this array — a clip that failed to render is skipped, so a job whose second
 * clip died hands back indexes 0 and 2. It is carried because the library row
 * the worker wrote is keyed by (job, index), and posting a cloud clip means
 * finding that row (lib/account.libraryClipsForJob).
 */
export type CloudFinishedClip = FinishedClip & {
  speakerCount?: number;
  clipIndex: number;
};

/**
 * Map server clips into the FinishedClip shape ClipCard renders: mp4
 * container, duration via end - start.
 *
 * Anonymous: absolute url straight at the server; it never reports file
 * size, so `blob` is a size-only stand-in — ClipCard reads nothing but
 * `blob.size`, and formatBytes(NaN) renders "—".
 *
 * Signed in: `/v1/files/*` of a user-owned job 404s without the session
 * bearer, and <video src> can't send headers — so each file is fetched with
 * the token into a real Blob and served from an object URL (which also gives
 * ClipCard a real size). The caller owns the returned objectUrls and must
 * revoke them when the clips are dropped. A failed fetch falls back to the
 * raw url so one bad download can't sink the batch (founder-token/dev-open
 * servers still play it anonymously).
 *
 * S3 mode: the clip url is an ABSOLUTE presigned GET at the bucket, already
 * authenticated in its query string — it takes no bearer (same guard as
 * uploadSource) and is played straight from that url.
 */
export async function toFinishedClips(
  base: string,
  clips: CloudClip[],
  signal?: AbortSignal
): Promise<{ clips: CloudFinishedClip[]; objectUrls: string[] }> {
  const token = tokenSource();
  const objectUrls: string[] = [];
  const out: CloudFinishedClip[] = [];
  for (const clip of clips) {
    const rawUrl = resolveUrl(base, clip.url);
    let url = rawUrl;
    let blob: Blob = { size: Number.NaN } as unknown as Blob;
    // The bearer is for OUR api only. A presigned S3 clip url is a third
    // party: sending the raw session token there writes it into someone
    // else's request logs (S3 access logs, CloudTrail), and SigV4 rejects a
    // request that carries a second Authorization header anyway — so the
    // fetch would fail and every clip would silently fall back.
    if (token && !isAbsoluteUrl(clip.url)) {
      try {
        const res = await fetch(rawUrl, {
          headers: { Authorization: `Bearer ${token}` },
          signal,
        });
        if (res.ok) {
          blob = await res.blob();
          url = URL.createObjectURL(blob);
          objectUrls.push(url);
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") {
          for (const u of objectUrls) URL.revokeObjectURL(u);
          throw e;
        }
        // Network blip — keep the raw url fallback.
      }
    }
    out.push({
      id: clip.id,
      start: clip.start,
      end: clip.end,
      score: clip.score,
      title: clip.title,
      hooks: clip.hooks,
      reason: clip.reason,
      tip: clip.tip,
      words: [],
      url,
      mimeType: "video/mp4",
      extension: "mp4",
      blob,
      speakerCount: clip.speaker_count,
      clipIndex: clip.index,
    });
  }
  return { clips: out, objectUrls };
}
