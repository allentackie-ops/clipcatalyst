"use client";

// One saved clip on /account (LIBRARY.md Part 2).
//
// The card is built around the one thing that makes this library different
// from a folder: a clip has TWO lifetimes. Its title, score and hooks are
// permanent; its video is kept for the plan's retention window and then
// deleted. So `available` decides the top of the card and nothing else —
// available draws a player, expired draws an honest panel saying the video is
// gone, and a <video> is never rendered at a url that would 404.
//
// The file itself is owner-only and <video src> cannot send an Authorization
// header, so it is fetched with the session on demand (loadClipSource) rather
// than eagerly for every card in the grid: a library page must not download
// twelve videos to show twelve cards.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Badge, Card, ScoreRing } from "@/components/ui";
import { formatBytes, formatDuration } from "@/components/studio/format";
import { loadClipSource, type ClipSource, type LibraryClip } from "@/lib/account";

const MARKERS = ["A", "B", "C"] as const;

/** "11 Aug 2026", in the visitor's own locale; "" when the server sent a date
 *  we can't read (the card carries on without it rather than printing NaN). */
function formatDate(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** The saved name for a downloaded clip. Built from the clip's INDEX and the
 *  container the server actually served — never from its title, which is text
 *  a transcript produced. */
function downloadName(clipIndex: number, mimeType: string): string {
  return `clipcatalyst-${clipIndex + 1}.${
    mimeType.includes("webm") ? "webm" : "mp4"
  }`;
}

const CHIP_CLS =
  "inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";

export default function LibraryClipCard({
  clip,
  deleting,
  deleteError,
  onDelete,
}: {
  clip: LibraryClip;
  /** True while this card's DELETE is in flight (the section serializes them). */
  deleting: boolean;
  /** A failed delete, said on the card that failed. */
  deleteError: string | null;
  onDelete: (id: string) => void;
}) {
  const [source, setSource] = useState<ClipSource | null>(null);
  const [loadingSource, setLoadingSource] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const deadRef = useRef(false);
  /** The object URL this card minted, so unmount can revoke it. */
  const sourceRef = useRef<ClipSource | null>(null);
  /** One fetch per card, shared by Play and Download. */
  const pendingRef = useRef<Promise<ClipSource> | null>(null);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  const fieldId = useId();
  const confirmId = `${fieldId}-confirm`;

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
      const held = sourceRef.current;
      if (held?.objectUrl) URL.revokeObjectURL(held.url);
    };
  }, []);

  const title = clip.title || "Untitled clip";

  const ensureSource = useCallback((): Promise<ClipSource> => {
    if (sourceRef.current !== null) return Promise.resolve(sourceRef.current);
    if (pendingRef.current === null) {
      pendingRef.current = loadClipSource(clip)
        .then((loaded) => {
          if (deadRef.current && loaded.objectUrl) {
            // The card went while the file was in flight — don't leak it.
            URL.revokeObjectURL(loaded.url);
          } else {
            sourceRef.current = loaded;
          }
          return loaded;
        })
        .catch((e: unknown) => {
          pendingRef.current = null; // a blip must not poison the retry
          throw e;
        });
    }
    return pendingRef.current;
  }, [clip]);

  const failure = (e: unknown, fallback: string): string =>
    e instanceof Error ? e.message : fallback;

  // Busy states change the LABEL and guard in the handler rather than
  // disabling the control — the same choice the sign-in form makes, and it
  // keeps focus where the keyboard left it.
  const play = useCallback(async () => {
    if (loadingSource) return;
    setSourceError(null);
    setLoadingSource(true);
    try {
      const loaded = await ensureSource();
      if (deadRef.current) return;
      setSource(loaded);
    } catch (e) {
      if (deadRef.current) return;
      setSourceError(failure(e, "Couldn't load that clip — try again."));
    } finally {
      if (!deadRef.current) setLoadingSource(false);
    }
  }, [ensureSource, loadingSource]);

  const download = useCallback(async () => {
    if (downloading) return;
    setSourceError(null);
    setDownloading(true);
    try {
      const loaded = await ensureSource();
      if (deadRef.current) return;
      // The file lives behind the session, so a plain <a href> would 404 —
      // save the bytes we already fetched instead.
      const anchor = document.createElement("a");
      anchor.href = loaded.url;
      anchor.download = downloadName(clip.clip_index, loaded.mimeType);
      anchor.rel = "noopener";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch (e) {
      if (deadRef.current) return;
      setSourceError(failure(e, "Couldn't download that clip — try again."));
    } finally {
      if (!deadRef.current) setDownloading(false);
    }
  }, [downloading, ensureSource, clip.clip_index]);

  // Opening the confirmation moves focus onto it: the question and its answer
  // must be where the keyboard already is.
  useEffect(() => {
    if (confirming) confirmButtonRef.current?.focus();
  }, [confirming]);

  const cancelDelete = useCallback(() => {
    setConfirming(false);
    // The Delete button is remounted BY this state change, so its ref is
    // still empty right now — hand focus back after React has committed
    // (the same wait the clip editor makes when it closes).
    requestAnimationFrame(() => deleteButtonRef.current?.focus());
  }, []);

  const created = formatDate(clip.created_at);
  const expires = clip.expires_at ? formatDate(clip.expires_at) : "";
  const fileLine = !clip.available
    ? expires
      ? `Video deleted on ${expires} — this card stays.`
      : "Video deleted — this card stays."
    : clip.expires_at === null
      ? "Video kept for as long as your account is."
      : expires
        ? `Video kept until ${expires}.`
        : "";

  return (
    <Card className="flex h-full flex-col gap-4 p-5">
      {/* 9:16 preview — a player only when there is really a file. */}
      <div className="relative mx-auto aspect-[9/16] w-full max-w-[220px] overflow-hidden rounded-2xl border border-line bg-black">
        {!clip.available ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-gradient-to-br from-ink-800 via-ink-850 to-ink-900 px-4 text-center">
            <span
              className="flex h-10 w-10 items-center justify-center rounded-full border border-line-strong bg-white/5 text-zinc-500"
              aria-hidden
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <circle cx="12" cy="12" r="9" />
                <path d="M12 7.5V12l3 2" />
              </svg>
            </span>
            <p className="text-sm font-medium text-zinc-300">Video expired</p>
            <p className="text-xs leading-relaxed text-zinc-500">
              The file was deleted at the end of its retention window. The
              title, score and hooks below are permanent.
            </p>
          </div>
        ) : source !== null ? (
          <video
            controls
            autoPlay
            playsInline
            preload="metadata"
            src={source.url}
            className="absolute inset-0 h-full w-full"
            aria-label={`Preview of ${title}`}
          />
        ) : (
          <button
            type="button"
            onClick={() => void play()}
            aria-label={`Play ${title}`}
            aria-busy={loadingSource}
            className="group absolute inset-0 flex flex-col items-center justify-center gap-2 bg-gradient-to-br from-ink-700 via-ink-800 to-brand-700/30 transition-colors duration-200 hover:from-ink-700 hover:to-brand-600/40 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brand-400"
          >
            <span
              className="flex h-12 w-12 items-center justify-center rounded-full border border-white/20 bg-black/40 text-white transition-transform duration-200 group-hover:scale-105"
              aria-hidden
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5.5v13l11-6.5-11-6.5Z" />
              </svg>
            </span>
            <span className="font-mono text-[11px] text-zinc-400">
              {loadingSource ? "Loading…" : formatDuration(clip.duration)}
            </span>
          </button>
        )}
      </div>

      <div className="flex items-start gap-3.5">
        <ScoreRing score={clip.score} size={48} className="shrink-0" />
        <div className="min-w-0 flex-1">
          <h3 className="font-display text-base font-semibold leading-snug text-white">
            {title}
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-zinc-400">
              {formatDuration(clip.duration)}
            </span>
            {created ? (
              <span className="font-mono text-xs text-zinc-500">{created}</span>
            ) : null}
            <Badge tone="neutral">
              {clip.engine === "browser" ? "Browser" : "Cloud"}
            </Badge>
            {clip.available ? (
              clip.bytes > 0 ? (
                <span className="font-mono text-xs text-zinc-600">
                  {formatBytes(clip.bytes)}
                </span>
              ) : null
            ) : (
              <Badge tone="ember">Expired</Badge>
            )}
            {clip.speaker_count >= 2 ? (
              <Badge tone="neutral">{clip.speaker_count} speakers</Badge>
            ) : null}
          </div>
        </div>
      </div>

      {clip.hooks.length > 0 ? (
        <div>
          <p className="mb-2 font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-zinc-500">
            Hooks
          </p>
          <ul className="flex flex-col gap-2">
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
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {fileLine ? (
        <p className="text-xs leading-relaxed text-zinc-500">{fileLine}</p>
      ) : null}

      <div aria-live="polite" className="flex flex-col gap-1 empty:hidden">
        {sourceError ? (
          <p role="alert" className="text-xs leading-relaxed text-ember-300">
            {sourceError}
          </p>
        ) : null}
        {deleteError ? (
          <p role="alert" className="text-xs leading-relaxed text-ember-300">
            {deleteError}
          </p>
        ) : null}
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
        {confirming ? (
          <div
            role="group"
            aria-labelledby={confirmId}
            className="flex w-full flex-col gap-2.5 rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-3"
          >
            <p id={confirmId} className="text-sm leading-relaxed text-ember-300">
              Delete this clip? The video and its card both go, for good.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                ref={confirmButtonRef}
                type="button"
                aria-busy={deleting}
                onClick={() => onDelete(clip.id)}
                className="inline-flex items-center justify-center rounded-full bg-ember-500/90 px-4 py-2 text-sm font-medium text-ink-950 transition-colors duration-200 hover:bg-ember-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ember-400"
              >
                {deleting ? "Deleting…" : "Yes, delete it"}
              </button>
              <button
                type="button"
                onClick={cancelDelete}
                className="rounded-full px-3 py-2 text-sm text-zinc-300 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
              >
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <>
            {clip.available ? (
              <button
                type="button"
                onClick={() => void download()}
                aria-label={`Download ${title}`}
                aria-busy={downloading}
                className={CHIP_CLS}
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
                  <path d="M12 4v12m0 0 4.5-4.5M12 16l-4.5-4.5" />
                  <path d="M4 20h16" />
                </svg>
                {downloading ? "Preparing…" : "Download"}
              </button>
            ) : null}
            <button
              ref={deleteButtonRef}
              type="button"
              onClick={() => setConfirming(true)}
              aria-label={`Delete ${title}`}
              className="rounded-full px-3 py-2 text-sm text-zinc-400 transition-colors hover:text-ember-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            >
              Delete
            </button>
          </>
        )}
      </div>
    </Card>
  );
}
