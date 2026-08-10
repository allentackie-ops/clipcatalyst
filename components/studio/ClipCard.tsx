"use client";

// Done state: one rendered clip — preview, score, hooks, download.

import { Badge, Card, ScoreRing } from "@/components/ui";
import type { FinishedClip } from "@/lib/studio/types";
import { formatBytes, formatDuration } from "./format";

const MARKERS = ["A", "B", "C"] as const;

export default function ClipCard({
  clip,
  index,
}: {
  /** Cloud clips carry a server-computed speakerCount (their words are []). */
  clip: FinishedClip & { speakerCount?: number };
  index: number;
}) {
  const duration = clip.end - clip.start;
  const format = clip.extension === "mp4" ? "MP4" : "WEBM";
  const filename = `clipcatalyst-${index + 1}.${clip.extension}`;
  // Distinct diarized speakers in this clip; the badge only appears for a
  // genuine multi-voice clip (unassigned words don't count). Prefer the
  // server-reported count when present, else derive it from the words.
  const speakerCount =
    clip.speakerCount ??
    new Set(
      clip.words
        .map((w) => w.speaker)
        .filter((s): s is number => s !== undefined)
    ).size;

  return (
    <Card className="flex h-full flex-col gap-5 p-5">
      {/* 9:16 preview, capped ~480px tall */}
      <div className="relative mx-auto aspect-[9/16] w-full max-w-[270px] overflow-hidden rounded-2xl border border-line bg-black">
        <video
          controls
          playsInline
          preload="metadata"
          src={clip.url}
          className="absolute inset-0 h-full w-full"
          aria-label={`Preview of clip ${index + 1}: ${clip.title}`}
        />
      </div>

      <div className="flex items-start gap-4">
        <ScoreRing score={clip.score} size={56} className="shrink-0" />
        <div className="min-w-0 flex-1">
          <h3 className="font-display text-base font-semibold leading-snug text-white">
            {clip.title}
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-zinc-400">
              {formatDuration(duration)}
            </span>
            <Badge tone="neutral" className="font-mono">
              {format}
            </Badge>
            {speakerCount >= 2 ? (
              <Badge tone="neutral">{speakerCount} speakers</Badge>
            ) : null}
            <span className="font-mono text-xs text-zinc-600">
              {formatBytes(clip.blob.size)}
            </span>
          </div>
        </div>
      </div>

      <div className="flex flex-col gap-2 text-sm">
        <p className="leading-relaxed text-zinc-400">{clip.reason}</p>
        <p className="leading-relaxed text-ember-400/90">
          <span className="mr-1.5 font-mono text-xs uppercase tracking-wide text-ember-400">
            Tip
          </span>
          {clip.tip}
        </p>
      </div>

      {clip.hooks.length > 0 ? (
        <div>
          <p className="mb-2.5 font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-zinc-500">
            Hooks
          </p>
          <ul className="flex flex-col gap-2.5">
            {clip.hooks.slice(0, 3).map((hook, i) => (
              <li key={i} className="flex items-start gap-2.5">
                <span
                  className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded border border-line bg-ink-800 font-mono text-[10px] text-brand-300"
                  aria-hidden
                >
                  {MARKERS[i]}
                </span>
                <span className="min-w-0 text-sm leading-snug text-zinc-300">
                  {hook}
                  {i === 0 ? (
                    <Badge tone="signal" className="ml-2 align-middle">
                      Recommended
                    </Badge>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-auto pt-1">
        <a
          href={clip.url}
          download={filename}
          aria-label={`Download clip ${index + 1} as ${format}`}
          className="inline-flex w-full items-center justify-center gap-2 rounded-full bg-gradient-to-r from-brand-600 to-spark-500 px-5 py-2.5 text-sm font-medium text-white shadow-[0_0_24px_rgba(139,92,246,0.35)] transition-all duration-200 hover:brightness-110 hover:shadow-[0_0_36px_rgba(139,92,246,0.55)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <path d="M12 4v12m0 0 4.5-4.5M12 16l-4.5-4.5" />
            <path d="M4 20h16" />
          </svg>
          Download
        </a>
      </div>
    </Card>
  );
}
