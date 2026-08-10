// Studio-local helpers: formatting + file probing + shared UI settings type.

import type { RenderOptions } from "@/lib/studio/types";

/** Settings the user picks in the idle view (watermark is added at run time). */
export type StudioUISettings = {
  targetLength: 15 | 30 | 60;
  count: 1 | 2 | 3;
  height: RenderOptions["height"];
};

export const DEFAULT_SETTINGS: StudioUISettings = {
  targetLength: 30,
  count: 2,
  height: 1280,
};

/** Hard cap on source length for the browser beta (seconds). */
export const MAX_SOURCE_SECONDS = 20 * 60;

/** 754 → "12:34"; 3921 → "1:05:21". For mono display. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** 1234567 → "1.2 MB". For mono display. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"] as const;
  let value = bytes;
  let unit: string = "B";
  for (const u of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = u;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

/**
 * Read a video File's duration via a temporary <video> element.
 * Resolves null when the browser can't report a finite duration;
 * rejects when the file isn't decodable as video at all, or when
 * metadata hasn't arrived within 10 s (stalled decoder).
 */
export function probeVideoDuration(file: File): Promise<number | null> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    const timer = window.setTimeout(() => {
      cleanup();
      reject(new Error("Couldn't read that file as a video."));
    }, 10_000);
    const cleanup = () => {
      window.clearTimeout(timer);
      video.onloadedmetadata = null;
      video.onerror = null;
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(url);
    };
    video.onloadedmetadata = () => {
      const d = video.duration;
      cleanup();
      resolve(Number.isFinite(d) && d > 0 ? d : null);
    };
    video.onerror = () => {
      cleanup();
      reject(new Error("Couldn't read that file as a video."));
    };
    video.src = url;
  });
}
