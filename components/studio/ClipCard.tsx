"use client";

// Done state: one rendered clip — preview, score, hooks, download/share, and
// (device clips only) the door into the chat editor.

import type { ReactNode } from "react";
import { Badge, Card, ScoreRing } from "@/components/ui";
import type { FinishedClip } from "@/lib/studio/types";
import { formatBytes, formatDuration } from "./format";
import ShareButton from "./ShareButton";

const MARKERS = ["A", "B", "C"] as const;

export default function ClipCard({
  clip,
  index,
  edited = false,
  onEdit,
  saveAction,
  postAction,
}: {
  /** Cloud clips carry a server-computed speakerCount (their words are []). */
  clip: FinishedClip & { speakerCount?: number };
  index: number;
  /** True when this card shows a re-rendered (edited) file. */
  edited?: boolean;
  /** Opens the clip editor. Absent (cloud clips) → no Edit button. */
  onEdit?: () => void;
  /** "Save to library", when there is an account to save it to. Passed in
   *  rather than built here: uploading is an account concern, and a card that
   *  imported one would drag it into every grid that shows a clip. */
  saveAction?: ReactNode;
  /** "Post to YouTube", when this clip has a library row and a channel is
   *  connected (PUBLISH.md Part 4). Passed in for the same reason `saveAction`
   *  is, and absent far more often than not — a clip has to be in the library
   *  before there is anything to post. */
  postAction?: ReactNode;
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
            {edited ? <Badge tone="brand">Edited</Badge> : null}
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

      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
        <a
          href={clip.url}
          download={filename}
          aria-label={`Download clip ${index + 1} as ${format}`}
          className="inline-flex min-w-0 flex-1 basis-36 items-center justify-center gap-2 rounded-full bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:bg-brand-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
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
        <ShareButton
          url={clip.url}
          // Cloud clips carry a size-only stand-in, not real bytes — leaving
          // blob undefined makes ShareButton fetch the file on demand.
          blob={clip.blob instanceof Blob ? clip.blob : undefined}
          filename={filename}
          title={clip.title}
          ariaLabel={`Share clip ${index + 1}`}
        />
        {onEdit ? (
          <button
            type="button"
            onClick={onEdit}
            aria-label={`Edit clip ${index + 1}`}
            className="inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="m17 3 4 4L8 20l-5 1 1-5L17 3Z" />
            </svg>
            Edit
          </button>
        ) : null}
        {postAction}
        {saveAction}
      </div>
    </Card>
  );
}
