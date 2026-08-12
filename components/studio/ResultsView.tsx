"use client";

// Device done view: the results grid (extracted from StudioApp) plus the
// interactive layer — one EditSession per clip, the ClipEditor overlay, and
// the re-render path. The pipeline's clips and object URLs are never touched
// here: every re-render lands in session.rendered, and only previously-edited
// URLs are ever revoked. A failed re-render keeps the last good file.

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui";
import {
  resolveEdits,
  retimeTrack,
  rewindowWords,
  type ClipEdits,
  type EditContext,
} from "@/lib/studio/edits";
import { renderClip, type RenderEdits } from "@/lib/studio/render";
import type { BrandKit } from "@/lib/studio/brandkit";
import type {
  ClipPlan,
  FinishedClip,
  RenderOptions,
  Transcript,
} from "@/lib/studio/types";
import ClipCard from "./ClipCard";
import ClipEditor, {
  editsEqual,
  newEditSession,
  type EditSession,
} from "./ClipEditor";
import type { StudioClip } from "./useStudioPipeline";

/** "24.3" — durations in chat replies always carry one decimal (edits.ts). */
function fmt1(seconds: number): string {
  return (Math.round(seconds * 10) / 10).toFixed(1);
}

/** Fold a successful re-render into a session. `snapshot` is the edits the
 *  render was started from — edits typed mid-render stay pending (dirty only
 *  clears when the rendered file matches what's on the timeline). The stored
 *  `rendered` carries the EDITED plan's words so every words-derived surface
 *  (the speakers badge) reflects the file actually on the card. */
export function bakeRenderResult(
  s: EditSession,
  snapshot: ClipEdits,
  rendered: NonNullable<EditSession["rendered"]>
): EditSession {
  return {
    ...s,
    rendered,
    rendering: null,
    dirty: !editsEqual(s.edits, snapshot),
    chat: [
      ...s.chat,
      {
        role: "catalyst",
        text: `Baked in. The clip is now ${fmt1(rendered.outputDuration)} s.`,
      },
    ],
  };
}

/** Card view of a clip: once an edit is baked in, the re-rendered file — and
 *  the edited plan's honest metadata (duration via `end`, byte size via
 *  `blob`, words for the speakers badge) — replaces the original. */
export function displayClip(clip: StudioClip, s: EditSession): FinishedClip {
  if (!s.rendered) return clip;
  return {
    ...clip,
    url: s.rendered.url,
    blob: s.rendered.blob,
    extension: s.rendered.extension,
    mimeType: s.rendered.mimeType,
    end: clip.start + s.rendered.outputDuration,
    words: s.rendered.words,
  };
}

export default function ResultsView({
  clips,
  sourceUrl,
  sourceDuration,
  transcript,
  renderOptions,
  brandKit,
  failedCount,
  onReset,
  renderSaveAction,
  renderPostAction,
}: {
  clips: StudioClip[];
  sourceUrl: string;
  sourceDuration: number;
  transcript: Transcript;
  renderOptions: RenderOptions;
  /** The kit the batch was rendered with — passed back into every re-render
   *  so baking in an edit can't quietly strip a clip's branding. */
  brandKit: BrandKit;
  failedCount: number;
  onReset: () => void;
  /** Builds each card's "Save to library" control, given the file the card is
   *  actually showing (the re-rendered one once an edit is baked in). A
   *  callback rather than an import: uploading needs the account, and this
   *  view is the device pipeline's, which works with no account at all. */
  renderSaveAction?: (clip: FinishedClip, index: number) => ReactNode;
  /** Builds each card's "Post to YouTube" control (PUBLISH.md Part 4), for the
   *  same reason and with the same shape. It usually returns nothing: a device
   *  clip has to be saved to the library before there is anything to post, and
   *  posting needs a connected channel. */
  renderPostAction?: (clip: FinishedClip, index: number) => ReactNode;
}) {
  const [sessions, setSessions] = useState<EditSession[]>(() =>
    clips.map(() => newEditSession())
  );
  // The ref is authoritative and updated eagerly, so async render callbacks
  // and rapid chat commands always compose on the latest sessions.
  const sessionsRef = useRef(sessions);
  // Replaced edited URLs — revoked AFTER React swaps the <video> sources.
  const staleUrlsRef = useRef<string[]>([]);
  const deadRef = useRef(false);
  const [editorIndex, setEditorIndex] = useState<number | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const updateSession = useCallback(
    (i: number, updater: (s: EditSession) => EditSession) => {
      const next = sessionsRef.current.map((s, j) => (j === i ? updater(s) : s));
      sessionsRef.current = next;
      setSessions(next);
    },
    []
  );

  useEffect(() => {
    if (staleUrlsRef.current.length === 0) return;
    const stale = staleUrlsRef.current;
    staleUrlsRef.current = [];
    for (const u of stale) URL.revokeObjectURL(u);
  });

  // Unmount: drop every EDITED object URL. The originals belong to the
  // pipeline (urlsRef in useStudioPipeline) and are never revoked here.
  useEffect(
    () => () => {
      deadRef.current = true;
      for (const s of sessionsRef.current) {
        if (s.rendered) URL.revokeObjectURL(s.rendered.url);
      }
      for (const u of staleUrlsRef.current) URL.revokeObjectURL(u);
    },
    []
  );

  const rerender = useCallback(
    async (i: number) => {
      const clip = clips[i];
      if (sessionsRef.current[i].rendering !== null) return;
      const snapshot = sessionsRef.current[i].edits;
      const ctx: EditContext = {
        planStart: clip.start,
        planEnd: clip.end,
        sourceDuration,
        words: transcript.words,
        hooks: clip.hooks,
      };
      const resolved = resolveEdits(snapshot, ctx);
      // The renderer gets an already-EDITED plan — resolved bounds, words
      // re-windowed to the new start — so its plan-based math keeps working.
      const plan: ClipPlan = {
        id: clip.id,
        start: resolved.start,
        end: resolved.end,
        score: clip.score,
        title: clip.title,
        hooks: clip.hooks,
        reason: clip.reason,
        tip: clip.tip,
        words: rewindowWords(transcript.words, resolved.start, resolved.end),
      };
      const track = clip.track
        ? retimeTrack(clip.track, resolved.start - clip.start)
        : undefined;
      const edits: RenderEdits = {
        keeps: resolved.keeps,
        zooms: resolved.zooms,
        captions: resolved.captions,
        outputDuration: resolved.outputDuration,
      };
      updateSession(i, (s) => ({ ...s, rendering: 0 }));
      try {
        const result = await renderClip(
          { url: sourceUrl },
          plan,
          {
            height: renderOptions.height,
            watermark: renderOptions.watermark,
            track,
            edits,
            brandKit,
          },
          (p) => updateSession(i, (s) => ({ ...s, rendering: p }))
        );
        const url = URL.createObjectURL(result.blob);
        if (deadRef.current) {
          // The view is gone (user reset mid-render) — don't leak the URL.
          URL.revokeObjectURL(url);
          return;
        }
        const prevUrl = sessionsRef.current[i].rendered?.url ?? null;
        updateSession(i, (s) =>
          bakeRenderResult(s, snapshot, {
            url,
            blob: result.blob,
            extension: result.extension,
            mimeType: result.mimeType,
            outputDuration: resolved.outputDuration,
            // The plan the renderer actually drew — rewindowWords output.
            words: plan.words,
          })
        );
        if (prevUrl) staleUrlsRef.current.push(prevUrl);
      } catch (e) {
        const message =
          e instanceof Error ? e.message : "Something went wrong while re-rendering.";
        updateSession(i, (s) => ({
          ...s,
          rendering: null,
          chat: [
            ...s.chat,
            {
              role: "catalyst",
              text: `Re-render failed: ${message} The previous version is untouched.`,
            },
          ],
        }));
      }
    },
    [
      clips,
      sourceUrl,
      sourceDuration,
      transcript,
      renderOptions,
      brandKit,
      updateSession,
    ]
  );

  const openEditor = useCallback((i: number) => {
    // Remember the opener (the card's Edit button) so focus can go home.
    openerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setEditorIndex(i);
  }, []);

  const closeEditor = useCallback(() => {
    setEditorIndex(null);
    const opener = openerRef.current;
    openerRef.current = null;
    requestAnimationFrame(() => opener?.focus());
  }, []);

  return (
    <>
      <div className="mx-auto flex max-w-4xl flex-col items-center gap-4 text-center sm:flex-row sm:items-end sm:justify-between sm:text-left">
        <div>
          <h1
            tabIndex={-1}
            className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
          >
            Your clips are ready
          </h1>
          <p className="mt-3 text-sm text-zinc-400 sm:text-base">
            <span className="font-mono">{clips.length}</span>{" "}
            {clips.length === 1 ? "clip" : "clips"}, scored and captioned on
            your device. Preview, then download.
          </p>
        </div>
        <Button variant="secondary" onClick={onReset}>
          Clip another video
        </Button>
      </div>

      {failedCount > 0 ? (
        <p className="mx-auto mt-8 max-w-4xl rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-3 text-center text-sm text-ember-300">
          <span className="font-mono">{failedCount}</span>{" "}
          {failedCount === 1 ? "clip" : "clips"} didn&apos;t finish rendering.
          Here&apos;s what did.
        </p>
      ) : null}

      <div
        className={`mx-auto mt-10 grid max-w-4xl gap-6 ${
          clips.length > 1 ? "md:grid-cols-2" : "md:max-w-md"
        } ${clips.length > 2 ? "xl:max-w-6xl xl:grid-cols-3" : ""}`}
      >
        {clips.map((clip, i) => {
          const shown = displayClip(clip, sessions[i]);
          return (
            <ClipCard
              key={clip.id}
              clip={shown}
              index={i}
              edited={sessions[i].rendered !== null}
              onEdit={() => openEditor(i)}
              saveAction={renderSaveAction?.(shown, i)}
              postAction={renderPostAction?.(shown, i)}
            />
          );
        })}
      </div>

      <p className="mt-10 text-center font-mono text-xs text-zinc-500">
        {renderOptions.watermark
          ? "Exports include the beta watermark · "
          : ""}
        MP4 vs WebM depends on your browser
      </p>

      {editorIndex !== null ? (
        <ClipEditor
          clips={clips}
          index={editorIndex}
          transcript={transcript}
          sourceDuration={sourceDuration}
          sessions={sessions}
          onUpdateSession={updateSession}
          onRerender={() => void rerender(editorIndex)}
          onClose={closeEditor}
        />
      ) : null}
    </>
  );
}
