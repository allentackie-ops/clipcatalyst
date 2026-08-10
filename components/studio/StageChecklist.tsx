"use client";

// Running state: the seven-stage pipeline checklist with live progress.

import { useMemo } from "react";
import { Button, Card } from "@/components/ui";
import type { PipelineProgress, StageId } from "@/lib/studio/types";

const STAGES: { id: StageId; label: string }[] = [
  { id: "read", label: "Read audio" },
  { id: "model", label: "Load AI model" },
  { id: "transcribe", label: "Transcribe" },
  { id: "diarize", label: "Identify speakers" },
  { id: "analyze", label: "Score moments" },
  { id: "reframe", label: "Reframe on the speaker" },
  { id: "render", label: "Render clips" },
];

/** Stages that report per-clip progress, so the meta line shows "clip i/n". */
const PER_CLIP_STAGES: ReadonlySet<StageId> = new Set<StageId>(["reframe", "render"]);

/** Screen-reader announcements — one per stage transition, never per-frame. */
const STAGE_ANNOUNCEMENTS: Record<StageId, string> = {
  read: "Reading audio",
  model: "Loading AI model",
  transcribe: "Transcribing",
  diarize: "Identifying speakers",
  analyze: "Scoring moments",
  reframe: "Reframing on the speaker",
  render: "Rendering clips",
  done: "Done",
};

function CheckIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m5 13 4 4L19 7" />
    </svg>
  );
}

function ProgressBar({ progress }: { progress: number }) {
  if (progress < 0) {
    // Indeterminate: sweeping shimmer built on the marquee keyframes.
    return (
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-white/10"
        role="progressbar"
        aria-label="Working"
      >
        <div className="flex h-full w-[200%] animate-marquee [animation-duration:1.4s]">
          <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-500 to-transparent" />
          <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-500 to-transparent" />
        </div>
      </div>
    );
  }
  const pct = Math.round(Math.min(Math.max(progress, 0), 1) * 100);
  return (
    <div
      className="h-1 w-full overflow-hidden rounded-full bg-white/10"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
    >
      <div
        className="h-full rounded-full bg-gradient-to-r from-brand-500 to-spark-400 transition-[width] duration-300 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export default function StageChecklist({
  progress,
  fileName,
}: {
  progress: PipelineProgress;
  fileName?: string;
}) {
  const currentIndex =
    progress.stage === "done"
      ? STAGES.length
      : STAGES.findIndex((s) => s.id === progress.stage);
  const pct =
    progress.progress >= 0
      ? Math.round(Math.min(Math.max(progress.progress, 0), 1) * 100)
      : null;

  // The live region announces stage transitions only — deriving from stage id
  // and clip index (not progress) keeps it silent during per-frame updates.
  const announcement = useMemo(
    () =>
      progress.stage === "render" && progress.clipIndex && progress.clipCount
        ? `Rendering clip ${progress.clipIndex} of ${progress.clipCount}`
        : STAGE_ANNOUNCEMENTS[progress.stage],
    [progress.stage, progress.clipIndex, progress.clipCount]
  );

  return (
    <div className="mx-auto w-full max-w-xl">
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      <div className="text-center">
        <h1
          tabIndex={-1}
          className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
        >
          Finding your clips
        </h1>
        {fileName ? (
          <p className="mt-3 truncate font-mono text-xs text-zinc-500" title={fileName}>
            {fileName}
          </p>
        ) : null}
      </div>

      <Card className="mt-8 p-6 sm:p-8">
        <ol className="flex flex-col gap-6">
          {STAGES.map((stage, i) => {
            const done = i < currentIndex;
            const active = i === currentIndex;
            return (
              <li key={stage.id} className="flex items-start gap-4">
                <span
                  className={`mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-full border font-mono text-xs transition-colors duration-300 ${
                    done
                      ? "border-signal-500/40 bg-signal-500/10 text-signal-400"
                      : active
                        ? "border-brand-400/60 bg-brand-500/15 text-brand-300"
                        : "border-line text-zinc-600"
                  }`}
                  aria-hidden
                >
                  {done ? (
                    <CheckIcon />
                  ) : active ? (
                    <span className="h-2 w-2 animate-pulse-soft rounded-full bg-brand-400" />
                  ) : (
                    `0${i + 1}`
                  )}
                </span>

                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex items-baseline justify-between gap-3">
                    <p
                      className={`text-sm font-medium ${
                        done
                          ? "text-zinc-400"
                          : active
                            ? "text-white"
                            : "text-zinc-600"
                      }`}
                    >
                      {stage.label}
                      {done ? (
                        <span className="sr-only"> — done</span>
                      ) : null}
                    </p>
                    {active ? (
                      <p className="shrink-0 font-mono text-xs text-zinc-400">
                        {PER_CLIP_STAGES.has(stage.id) &&
                        progress.clipIndex &&
                        progress.clipCount
                          ? `clip ${progress.clipIndex}/${progress.clipCount}${
                              pct !== null ? ` · ${pct}%` : ""
                            }`
                          : pct !== null
                            ? `${pct}%`
                            : "…"}
                      </p>
                    ) : null}
                  </div>

                  {active ? (
                    <div className="mt-2.5 flex flex-col gap-2">
                      <ProgressBar progress={progress.progress} />
                      {progress.detail ? (
                        <p className="truncate font-mono text-xs text-zinc-500">
                          {progress.detail}
                        </p>
                      ) : null}
                      {stage.id === "model" ? (
                        <p className="text-xs text-zinc-600">
                          ~40–150 MB depending on your device — cached after.
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      </Card>

      <div className="mt-6 flex flex-col items-center gap-2">
        <Button variant="ghost" onClick={() => window.location.reload()}>
          Cancel
        </Button>
        <p className="text-xs text-zinc-600">
          Everything runs on this device — canceling just reloads the page.
        </p>
      </div>
    </div>
  );
}
