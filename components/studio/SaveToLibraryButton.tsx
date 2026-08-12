"use client";

// "Save to library" — the only way a clip this browser rendered ever leaves
// the device (LIBRARY.md Part 3).
//
// The rules here are the product's promise, not a preference:
//
//   * It is a CLICK. There is no timer, nothing fires when a render
//     completes, and there is no pre-ticked box anywhere near it. Studio's
//     whole pitch is that the video never leaves the machine, so an upload is
//     always somebody deciding to make an exception.
//   * It only exists for a signed-in account, because there is nowhere to put
//     a clip otherwise — signed out, this renders nothing at all rather than
//     a control that would ask you to sign in mid-celebration.
//   * A failure costs nothing. The rendered file is still in the card's
//     player and still downloadable; the message says so and the button goes
//     back to offering the click.
//
// Cloud clips never see this button: those were rendered on the server, which
// wrote their library rows already (worker.py).

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui";
import { useAccount } from "@/components/account/AccountProvider";
import {
  uploadClip,
  type ClipWord,
  type LibraryClipDetail,
} from "@/lib/account";
import { outputWidth } from "@/lib/studio/render";
import type { FinishedClip, RenderOptions } from "@/lib/studio/types";
import { cloudEnabled } from "./cloud";

/** The server's own ceilings (models.py ClipUploadRequest). Clamped here so a
 *  long title can never cost somebody their clip on a 422. */
const MAX_TITLE = 300;
const MAX_TEXT = 1000;
const MAX_HOOKS = 10;
const MAX_WORDS = 5000;

type Phase = "idle" | "saving" | "saved" | "error";

export default function SaveToLibraryButton({
  clip,
  index,
  height,
  onSaved,
}: {
  clip: FinishedClip;
  index: number;
  /** The render's output height — the honest source of the clip's pixel
   *  dimensions, since a FinishedClip carries only its blob. */
  height: RenderOptions["height"];
  /** The saved row, handed up once. This click is what gives a device clip a
   *  library id, and a library id is the only thing that can be posted to a
   *  channel (PUBLISH.md Part 4) — so the "Post to YouTube" chip on this card
   *  appears the moment the clip is somewhere a post could read it from. */
  onSaved?: (saved: LibraryClipDetail) => void;
}) {
  const { user } = useAccount();
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const deadRef = useRef(false);

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
    };
  }, []);

  const save = useCallback(async () => {
    if (phase === "saving" || phase === "saved") return;
    setError(null);
    setProgress(0);
    setPhase("saving");
    try {
      const words: ClipWord[] = clip.words
        .slice(0, MAX_WORDS)
        .map((word) => ({
          text: word.text,
          start: word.start,
          end: word.end,
          // The server's word carries an explicit null for "unknown"; the
          // browser's carries an absent field. Same fact, two spellings.
          speaker: word.speaker ?? null,
        }));
      const saved = await uploadClip(
        clip.blob,
        `clipcatalyst-${index + 1}.${clip.extension}`,
        {
          title: clip.title.slice(0, MAX_TITLE),
          score: Math.min(100, Math.max(0, Math.round(clip.score))),
          hooks: clip.hooks.slice(0, MAX_HOOKS),
          reason: clip.reason.slice(0, MAX_TEXT),
          tip: clip.tip.slice(0, MAX_TEXT),
          start: clip.start,
          end: clip.end,
          duration: Math.max(0, clip.end - clip.start),
          width: outputWidth(height),
          height,
          // Distinct diarized speakers in this clip — derived from the words
          // the same way the card's badge is.
          speaker_count: new Set(
            clip.words
              .map((word) => word.speaker)
              .filter((speaker): speaker is number => speaker !== undefined)
          ).size,
          clip_index: index,
          words,
        },
        (fraction) => {
          if (!deadRef.current) setProgress(fraction);
        }
      );
      if (deadRef.current) return;
      setPhase("saved");
      onSaved?.(saved);
    } catch (e) {
      if (deadRef.current) return;
      setPhase("error");
      setError(
        e instanceof Error ? e.message : "Saving that clip didn't work."
      );
    }
  }, [clip, height, index, onSaved, phase]);

  // No account, no destination — and with no cloud API configured there is no
  // library at all.
  if (!cloudEnabled || user === null) return null;

  const pct = Math.round(Math.min(Math.max(progress, 0), 1) * 100);

  return (
    <div className="flex w-full flex-col gap-2">
      {phase === "saved" ? (
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="signal">
            <svg
              width="12"
              height="12"
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
            Saved to library
          </Badge>
          <Link
            href="/account"
            className="rounded-lg text-xs text-brand-300 underline-offset-4 transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            View library →
          </Link>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => void save()}
          aria-label={`Save clip ${index + 1} to your library`}
          aria-busy={phase === "saving"}
          className="inline-flex items-center justify-center gap-1.5 self-start rounded-full border border-line-strong bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
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
            <path d="M12 20V8m0 0 4.5 4.5M12 8 7.5 12.5" />
            <path d="M4 4h16" />
          </svg>
          {phase === "saving"
            ? "Saving…"
            : phase === "error"
              ? "Try saving again"
              : "Save to library"}
        </button>
      )}

      {/* Progress lives OUTSIDE the live region: the bar's role already
          carries the value, and a screen reader reading 1…100 aloud is noise. */}
      {phase === "saving" ? (
        <div className="flex flex-col gap-1.5">
          <div
            role="progressbar"
            aria-label={`Saving clip ${index + 1} to your library`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            className="h-1 w-full overflow-hidden rounded-full bg-white/10"
          >
            <div
              className="h-full rounded-full bg-gradient-to-r from-brand-500 to-spark-400 transition-[width] duration-300 ease-out"
              style={{ width: `${pct}%` }}
            />
          </div>
          <p className="font-mono text-[11px] text-zinc-500">
            Uploading — <span className="tabular-nums">{pct}%</span>
          </p>
        </div>
      ) : null}

      {/* Transitions only, so the announcement is the outcome and nothing
          else (the same rule the cloud checklist follows). */}
      <p className="sr-only" aria-live="polite">
        {phase === "saving"
          ? `Saving clip ${index + 1} to your library.`
          : phase === "saved"
            ? `Clip ${index + 1} saved to your library.`
            : ""}
      </p>

      <div className="flex flex-col gap-1 empty:hidden">
        {phase === "saved" ? (
          <p className="text-xs leading-relaxed text-zinc-500">
            The file is on your account now. It stays on this device too —
            nothing here was moved or deleted.
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="text-xs leading-relaxed text-ember-300">
            {error} The clip is untouched on your device — download it, or try
            again.
          </p>
        ) : null}
      </div>
    </div>
  );
}
