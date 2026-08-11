"use client";

// Idle state: dropzone, chosen-file chip, settings, privacy line, generate CTA.

import {
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Badge, Card, Mark } from "@/components/ui";
import {
  formatBytes,
  formatDuration,
  type StudioUISettings,
} from "./format";

const ACCEPT = "video/mp4,video/quicktime,video/webm,video/x-m4v,video/*";

const LENGTHS: StudioUISettings["targetLength"][] = [15, 30, 60];
const COUNTS: StudioUISettings["count"][] = [1, 2, 3];
const QUALITIES: { height: StudioUISettings["height"]; label: string }[] = [
  { height: 960, label: "540×960 · fast" },
  { height: 1280, label: "720×1280 · balanced" },
  { height: 1920, label: "1080×1920 · max" },
];

function Chip({
  selected,
  onClick,
  label,
  children,
}: {
  selected: boolean;
  onClick: () => void;
  label: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      aria-label={label}
      className={`rounded-full border px-4 py-1.5 font-mono text-sm transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
        selected
          ? "border-brand-400/60 bg-brand-500/15 text-white"
          : "border-line text-zinc-400 hover:border-line-strong hover:text-zinc-200"
      }`}
    >
      {children}
    </button>
  );
}

function SettingLabel({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-zinc-500">
      {children}
    </span>
  );
}

export default function StudioDropzone({
  file,
  fileDuration,
  probing,
  fileError,
  settings,
  onSettingsChange,
  onFileSelect,
  onClearFile,
  onGenerate,
}: {
  file: File | null;
  fileDuration: number | null;
  probing: boolean;
  fileError: string | null;
  settings: StudioUISettings;
  onSettingsChange: (next: StudioUISettings) => void;
  onFileSelect: (file: File) => void;
  onClearFile: () => void;
  onGenerate: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const openPicker = () => inputRef.current?.click();

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openPicker();
    }
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) onFileSelect(dropped);
  };

  const canGenerate = file !== null && !probing;

  return (
    <div className="mx-auto w-full max-w-2xl">
      {/* Dropzone */}
      <div
        role="button"
        tabIndex={0}
        aria-label="Choose a video file to clip"
        onClick={openPicker}
        onKeyDown={onKeyDown}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`group cursor-pointer rounded-2xl border-2 border-dashed px-6 py-12 text-center transition-all duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 sm:py-16 ${
          dragging
            ? "border-brand-400 bg-brand-500/10 ring-2 ring-brand-400/40"
            : "border-line-strong bg-ink-900/60 hover:border-brand-400/50 hover:bg-ink-900"
        }`}
      >
        {/* pointer-events-none so children never flicker the dragleave state */}
        <div className="pointer-events-none flex flex-col items-center gap-4">
          <span
            className={`flex h-14 w-14 items-center justify-center rounded-2xl border transition-colors duration-200 ${
              dragging
                ? "border-brand-400/60 bg-brand-500/20"
                : "border-line-strong bg-ink-800 group-hover:border-brand-400/40"
            }`}
          >
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-brand-400"
              aria-hidden
            >
              <path d="M12 16V4m0 0 4.5 4.5M12 4 7.5 8.5" />
              <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
            </svg>
          </span>
          <div>
            <p className="font-display text-lg font-semibold text-white">
              {dragging ? "Drop it — let's clip" : "Drop your video here"}
            </p>
            <p className="mt-1 text-sm text-zinc-400">
              or click to browse — MP4, MOV, or WebM
            </p>
          </div>
          <p className="font-mono text-xs text-zinc-500">
            ≤ 20 min · processed on-device
          </p>
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        className="sr-only"
        aria-hidden
        tabIndex={-1}
        onChange={(e) => {
          const picked = e.target.files?.[0];
          if (picked) onFileSelect(picked);
          e.target.value = "";
        }}
      />

      {/* Probe / error feedback */}
      {probing ? (
        <p className="mt-4 text-center font-mono text-xs text-zinc-500" role="status">
          Reading file…
        </p>
      ) : null}
      {fileError ? (
        <p className="mt-4 text-center text-sm text-ember-400" role="alert">
          {fileError}
        </p>
      ) : null}

      {/* Chosen file chip */}
      {file ? (
        <Card className="mt-5 flex items-center gap-3 px-4 py-3">
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-line-strong bg-ink-800"
            aria-hidden
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-brand-300"
            >
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="M3 9h18M8 5v14M16 5v14" />
            </svg>
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-white" title={file.name}>
              {file.name}
            </p>
            <p className="font-mono text-xs text-zinc-500">
              {fileDuration !== null ? formatDuration(fileDuration) : "length unknown"}
              {" · "}
              {formatBytes(file.size)}
            </p>
          </div>
          <Badge tone="signal" className="hidden sm:inline-flex">Ready</Badge>
          <button
            type="button"
            onClick={onClearFile}
            aria-label={`Remove ${file.name}`}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-zinc-500 transition-colors hover:bg-white/5 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              aria-hidden
            >
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </Card>
      ) : null}

      {/* Settings — DOM order matches visual order so tabbing reads left to right. */}
      <div className="mt-8 grid gap-6 sm:grid-cols-[1fr_auto_auto] sm:gap-8">
        <div className="flex flex-col gap-2.5">
          <SettingLabel>Quality</SettingLabel>
          <select
            aria-label="Export quality"
            value={settings.height}
            onChange={(e) =>
              onSettingsChange({
                ...settings,
                height: Number(e.target.value) as StudioUISettings["height"],
              })
            }
            className="h-[38px] cursor-pointer rounded-full border border-line bg-ink-900 px-4 font-mono text-sm text-zinc-200 transition-colors hover:border-line-strong focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            {QUALITIES.map((q) => (
              <option key={q.height} value={q.height}>
                {q.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-2.5">
          <SettingLabel>Clip length</SettingLabel>
          <div className="flex gap-2" role="group" aria-label="Target clip length">
            {LENGTHS.map((len) => (
              <Chip
                key={len}
                selected={settings.targetLength === len}
                onClick={() => onSettingsChange({ ...settings, targetLength: len })}
                label={`${len} second clips`}
              >
                {len}s
              </Chip>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-2.5">
          <SettingLabel>Clips</SettingLabel>
          <div className="flex gap-2" role="group" aria-label="Number of clips">
            {COUNTS.map((n) => (
              <Chip
                key={n}
                selected={settings.count === n}
                onClick={() => onSettingsChange({ ...settings, count: n })}
                label={`${n} ${n === 1 ? "clip" : "clips"}`}
              >
                {n}
              </Chip>
            ))}
          </div>
        </div>
      </div>

      {/* Generate */}
      <div className="mt-8 flex flex-col items-center gap-5">
        <button
          type="button"
          onClick={onGenerate}
          disabled={!canGenerate}
          aria-disabled={!canGenerate}
          className="inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-brand-600 to-spark-500 px-7 py-3.5 text-base font-medium text-white shadow-[0_0_24px_rgba(139,92,246,0.35)] transition-all duration-200 hover:brightness-110 hover:shadow-[0_0_36px_rgba(139,92,246,0.55)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none disabled:hover:brightness-100"
        >
          {/* The mark earns its place here: it draws clips, which is exactly
              what this button returns. */}
          <Mark className="h-4 w-4" />
          Find my clips
        </button>

        <p className="flex items-center justify-center gap-2 text-sm text-zinc-400">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="shrink-0 text-signal-400"
            aria-hidden
          >
            <rect x="4" y="10" width="16" height="10" rx="2" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" />
          </svg>
          Runs 100% in your browser — your video never leaves your device.
        </p>

        <p className="max-w-md text-center text-xs leading-relaxed text-zinc-500">
          Best with talking content up to 15 minutes. First run downloads the
          AI model (~40–150&nbsp;MB depending on your device) — cached
          afterwards.
        </p>
      </div>
    </div>
  );
}
