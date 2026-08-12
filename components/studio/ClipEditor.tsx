"use client";

// Overlay editor for one device clip. The chat and the quick chips both funnel
// into the frozen judgement cores — parseIntent (lib/studio/intent.ts) maps a
// sentence onto EditCommands, applyCommand (lib/studio/edits.ts) does the math
// and hands back the truthful summary the reply is built from. The left side
// shows the last rendered file plus a pure-CSS timeline of the pending cuts
// and zooms. Re-rendering itself lives in ResultsView (it owns the sessions
// and their object URLs); this dialog reads sessions and funnels every change
// through onUpdateSession.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import {
  applyCommand,
  describeEdits,
  EMPTY_EDITS,
  findPauses,
  isEmptyEdits,
  resolveEdits,
  rewindowWords,
  type ClipEdits,
  type EditCommand,
  type EditContext,
} from "@/lib/studio/edits";
import { parseIntent, type Intent, type IntentContext } from "@/lib/studio/intent";
import type { Transcript, TranscriptWord } from "@/lib/studio/types";
import type { StudioClip } from "./useStudioPipeline";
import { formatBytes, formatDuration } from "./format";
import ShareButton from "./ShareButton";

// ---- Per-clip edit session (owned by ResultsView, mutated only here + there)

export type EditSession = {
  edits: ClipEdits;
  /** Undo stack (cap HISTORY_CAP). */
  history: ClipEdits[];
  chat: { role: "user" | "catalyst"; text: string }[];
  rendered: {
    url: string;
    blob: Blob;
    extension: string;
    mimeType: string;
    /** Real duration of the rendered file — keeps duration labels honest. */
    outputDuration: number;
    /** Words of the EDITED plan (rewindowWords output the render was built
     *  from) — keeps words-derived card UI (the speakers badge) honest. */
    words: TranscriptWord[];
  } | null;
  /** 0..1 re-render progress, null = not rendering. */
  rendering: number | null;
  /** Edits changed since the last render — gates the Re-render button. */
  dirty: boolean;
};

export const HISTORY_CAP = 20;

/** Appended to a reply whenever the message actually changed the edits. */
const RERENDER_HINT = "Hit Re-render to bake it in.";

/** Every example is a real command — the honesty rule covers the greeting. */
const GREETING =
  'Tell me what to change. Try "remove all the pauses", "add a zoom at 0:08", or "trim the first 2 seconds". Say "help" for everything I can do.';

export function newEditSession(): EditSession {
  return {
    edits: EMPTY_EDITS,
    history: [],
    chat: [{ role: "catalyst", text: GREETING }],
    rendered: null,
    rendering: null,
    dirty: false,
  };
}

/** Field-by-field ClipEdits equality — applyCommand re-normalizes its input,
 *  so reference identity can't tell a no-op from a real change. */
export function editsEqual(a: ClipEdits, b: ClipEdits): boolean {
  return (
    a.startDelta === b.startDelta &&
    a.endDelta === b.endDelta &&
    a.captions === b.captions &&
    a.hookIndex === b.hookIndex &&
    a.cuts.length === b.cuts.length &&
    a.cuts.every((c, i) => c.start === b.cuts[i].start && c.end === b.cuts[i].end) &&
    a.zooms.length === b.zooms.length &&
    a.zooms.every(
      (z, i) =>
        z.start === b.zooms[i].start &&
        z.end === b.zooms[i].end &&
        z.scale === b.zooms[i].scale
    )
  );
}

/** Build one clip's FULL IntentContext — its CURRENT output duration, live
 *  pauses, hooks, and words re-based to its current start (display time).
 *  One code path for every clip, so a cross-clip parse sees the TARGET clip
 *  exactly the way an active-clip parse sees the active one. */
export function buildIntentContext(
  clipIndex: number,
  clipCount: number,
  edits: ClipEdits,
  ectx: EditContext
): IntentContext {
  const resolved = resolveEdits(edits, ectx);
  return {
    clipCount,
    activeClip: clipIndex,
    duration: resolved.outputDuration,
    pauses: findPauses(edits, ectx),
    hooks: ectx.hooks,
    words: rewindowWords(ectx.words, resolved.start, resolved.end).map((w) => ({
      text: w.text,
      start: w.start,
    })),
  };
}

/** Two-pass parse. Pass 1 uses the ACTIVE clip's context; if it yields
 *  commands aimed at ANOTHER clip, the same input is re-parsed against the
 *  TARGET clip's full context and that second result wins — including a
 *  second-pass unknown, which the chat surfaces verbatim. Without this,
 *  "punch in when he says funnels in clip 2" typed in clip 1's editor runs
 *  the word search over clip 1's words and lands the zoom in clip 2 at
 *  clip 1's timestamp. Deterministic: one re-parse, no heuristics. */
export function parseChatIntent(
  input: string,
  activeClip: number,
  intentCtxFor: (clip: number) => IntentContext
): Intent {
  const first = parseIntent(input, intentCtxFor(activeClip));
  if (first.kind !== "commands" || first.targetClip === activeClip) return first;
  return parseIntent(input, intentCtxFor(first.targetClip));
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select, textarea, video[controls], [tabindex]:not([tabindex="-1"])';

export default function ClipEditor({
  clips,
  index,
  transcript,
  sourceDuration,
  sessions,
  onUpdateSession,
  onRerender,
  onClose,
}: {
  clips: StudioClip[];
  /** Which clip this editor is open on (0-based). */
  index: number;
  transcript: Transcript;
  sourceDuration: number;
  sessions: EditSession[];
  onUpdateSession: (clip: number, updater: (s: EditSession) => EditSession) => void;
  /** Kicks off a re-render of the active clip (ResultsView owns the render). */
  onRerender: () => void;
  onClose: () => void;
}) {
  const clip = clips[index];
  const session = sessions[index];
  const [input, setInput] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  const ctxFor = useCallback(
    (i: number): EditContext => ({
      planStart: clips[i].start,
      planEnd: clips[i].end,
      sourceDuration,
      words: transcript.words,
      hooks: clips[i].hooks,
    }),
    [clips, sourceDuration, transcript]
  );

  /** Full IntentContext for ANY clip's session — the parser must see the
   *  clip it will actually edit (cross-clip word searches included). */
  const intentCtxFor = useCallback(
    (i: number): IntentContext =>
      buildIntentContext(i, clips.length, sessions[i].edits, ctxFor(i)),
    [clips.length, sessions, ctxFor]
  );

  const ctx = useMemo(() => ctxFor(index), [ctxFor, index]);
  const resolved = useMemo(() => resolveEdits(session.edits, ctx), [session.edits, ctx]);
  const chips = useMemo(() => describeEdits(session.edits, ctx), [session.edits, ctx]);
  const pauses = useMemo(() => findPauses(session.edits, ctx), [session.edits, ctx]);

  // The placeholder must suggest a command that actually works on THIS clip:
  // point at a real pause when one exists, else a command that needs no time.
  const placeholder =
    pauses.length > 0
      ? `Try: remove the pause at ${formatDuration(
          pauses.reduce((a, b) => (b.length > a.length ? b : a)).start
        )}`
      : "Try: make it more energetic";

  // Midpoint of the longest kept segment, display time — a spot the quick
  // "Punch-in zoom" chip can always legally zoom at (never inside a cut).
  const zoomSpot = useMemo(() => {
    let best = resolved.keeps[0];
    for (const k of resolved.keeps) {
      if (k.to - k.from > best.to - best.from) best = k;
    }
    return Math.round(((best.from + best.to) / 2 - resolved.start) * 10) / 10;
  }, [resolved]);

  const appendChat = useCallback(
    (i: number, msgs: EditSession["chat"]) => {
      onUpdateSession(i, (s) => ({ ...s, chat: [...s.chat, ...msgs] }));
    },
    [onUpdateSession]
  );

  /** Apply commands to `target`'s session; the reply lands in THIS chat. */
  const runCommands = useCallback(
    (userText: string, commands: EditCommand[], target: number, note?: string) => {
      const tctx = ctxFor(target);
      const before = sessions[target].edits;
      let cur = before;
      const summaries: string[] = [];
      for (const c of commands) {
        const r = applyCommand(cur, c, tctx);
        summaries.push(r.summary);
        cur = r.edits;
      }
      const changed = !editsEqual(cur, before);
      if (changed) {
        const next = cur;
        onUpdateSession(target, (s) => ({
          ...s,
          edits: next,
          history: [...s.history, s.edits].slice(-HISTORY_CAP),
          dirty: true,
        }));
      }
      let reply = summaries.join(" ");
      if (note) reply += ` ${note}`;
      if (target !== index) {
        reply += changed
          ? ` That went to clip ${target + 1}. Open it and hit Re-render to bake it in.`
          : ` That was about clip ${target + 1}.`;
      } else if (changed) {
        reply += ` ${RERENDER_HINT}`;
      }
      appendChat(index, [
        { role: "user", text: userText },
        { role: "catalyst", text: reply },
      ]);
    },
    [ctxFor, sessions, onUpdateSession, appendChat, index]
  );

  /** Bare "undo": pop this clip's history stack (chips clear via commands). */
  const doUndo = useCallback(
    (userText: string) => {
      const s = sessions[index];
      if (s.history.length === 0) {
        appendChat(index, [
          { role: "user", text: userText },
          { role: "catalyst", text: "There's nothing to undo yet." },
        ]);
        return;
      }
      const prev = s.history[s.history.length - 1];
      onUpdateSession(index, (cur) => ({
        ...cur,
        edits: prev,
        history: cur.history.slice(0, -1),
        dirty: true,
      }));
      appendChat(index, [
        { role: "user", text: userText },
        {
          role: "catalyst",
          text: isEmptyEdits(prev)
            ? "Undid the last edit. You're back to the original clip."
            : "Undid the last edit.",
        },
      ]);
    },
    [sessions, index, onUpdateSession, appendChat]
  );

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    // The parser sees each clip as it CURRENTLY stands: output duration,
    // live pauses, and words re-based to the current start (display time).
    // Cross-clip commands are re-parsed against the TARGET clip's context.
    const intent = parseChatIntent(text, index, intentCtxFor);
    if (intent.kind === "help" || intent.kind === "unknown") {
      appendChat(index, [
        { role: "user", text },
        { role: "catalyst", text: intent.reply },
      ]);
    } else if (intent.kind === "undo") {
      doUndo(text);
    } else {
      runCommands(text, intent.commands, intent.targetClip, intent.note);
    }
  };

  // --- Dialog plumbing: focus, scroll lock, Escape, chat autoscroll --------

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [session.chat.length]);

  // Trap Tab inside the dialog — the page behind is inert to the keyboard.
  const trapTab = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Tab") return;
    const root = dialogRef.current;
    if (!root) return;
    const focusables = Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !root.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !root.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  };

  // --- Current file (last render wins) + timeline geometry -----------------

  const previewUrl = session.rendered?.url ?? clip.url;
  const currentBlob = session.rendered?.blob ?? clip.blob;
  const currentExt = session.rendered?.extension ?? clip.extension;
  const currentDuration = session.rendered
    ? session.rendered.outputDuration
    : clip.end - clip.start;
  const filename = `clipcatalyst-${index + 1}.${currentExt}`;

  const span = Math.max(0.001, resolved.end - resolved.start);
  const pct = (v: number) => `${(v / span) * 100}%`;

  const captionsOn = session.edits.captions;
  const quickChips: { label: string; act: () => void }[] = [
    { label: "Tighten pauses", act: () => runCommands("Tighten pauses", [{ type: "tighten" }], index) },
    { label: "Punch-in zoom", act: () => runCommands("Punch-in zoom", [{ type: "zoom", at: zoomSpot }], index) },
    { label: "Trim start 1s", act: () => runCommands("Trim start 1s", [{ type: "trim", edge: "start", seconds: 1 }], index) },
    { label: "Trim end 1s", act: () => runCommands("Trim end 1s", [{ type: "trim", edge: "end", seconds: 1 }], index) },
    {
      label: captionsOn ? "Captions off" : "Captions on",
      act: () =>
        runCommands(captionsOn ? "Captions off" : "Captions on", [{ type: "captions", on: !captionsOn }], index),
    },
    { label: "Undo", act: () => doUndo("Undo") },
    { label: "Reset", act: () => runCommands("Reset", [{ type: "reset" }], index) },
  ];

  const canRerender = session.dirty && session.rendering === null;

  // Portal to <body>: the done view mounts inside an animate-rise wrapper
  // whose settled transform would otherwise become the containing block for
  // position:fixed — the overlay must cover the real viewport.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6">
      {/* Backdrop — clicking it closes, same as Escape. */}
      <div
        aria-hidden
        onMouseDown={onClose}
        className="absolute inset-0 bg-ink-950/80 backdrop-blur-sm"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Edit clip ${index + 1}: ${clip.title}`}
        onKeyDown={trapTab}
        className="relative flex max-h-[92dvh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-line-strong bg-ink-900 shadow-[0_24px_80px_rgba(0,0,0,0.6)]"
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <p className="font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-brand-400">
              Edit clip {index + 1}
            </p>
            <h2 className="truncate font-display text-lg font-semibold tracking-tight text-white">
              {clip.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close editor"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-zinc-400 transition-colors hover:bg-white/5 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <svg
              width="16"
              height="16"
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
        </div>

        {/* Body */}
        <div className="grid min-h-0 flex-1 gap-6 overflow-y-auto p-5 sm:p-6 md:grid-cols-[260px_minmax(0,1fr)] md:overflow-hidden">
          {/* Left: preview + timeline strip */}
          <div className="cc-scroll flex flex-col md:overflow-y-auto md:pr-1">
            <div className="relative mx-auto aspect-[9/16] w-full max-w-[240px] overflow-hidden rounded-2xl border border-line bg-black">
              <video
                key={previewUrl}
                controls
                playsInline
                preload="metadata"
                src={previewUrl}
                className="absolute inset-0 h-full w-full"
                aria-label={`Editor preview of clip ${index + 1}`}
              />
            </div>
            <p className="mt-3 text-center font-mono text-xs text-zinc-500">
              {formatDuration(currentDuration)} · {currentExt.toUpperCase()} ·{" "}
              {formatBytes(currentBlob.size)}
            </p>
            {session.dirty ? (
              <p className="mt-2 text-center text-xs leading-relaxed text-ember-400/90">
                The preview shows the last render. Hit Re-render to apply your
                edits.
              </p>
            ) : null}

            {/* Timeline: kept video is light, cuts are gaps, zooms are violet. */}
            <div className="mt-4">
              <div
                className="relative h-2 w-full overflow-hidden rounded-full bg-white/[0.06]"
                aria-hidden
              >
                {resolved.keeps.map((k, i) => (
                  <div
                    key={`k${i}`}
                    className="absolute inset-y-0 rounded-full bg-white/25"
                    style={{ left: pct(k.from - resolved.start), width: pct(k.to - k.from) }}
                  />
                ))}
                {resolved.zooms.map((z, i) => (
                  <div
                    key={`z${i}`}
                    className="absolute inset-y-0 rounded-full bg-brand-500"
                    style={{ left: pct(z.start), width: pct(z.end - z.start) }}
                  />
                ))}
              </div>
              <div className="mt-1.5 flex justify-between font-mono text-[10px] text-zinc-500">
                <span>0:00</span>
                <span>{formatDuration(span)}</span>
              </div>
              <p className="sr-only">
                {chips.length > 0 ? `Pending edits: ${chips.join(", ")}` : "No edits yet"}
              </p>
            </div>
          </div>

          {/* Right: chat + chips + actions */}
          <div className="flex min-h-0 flex-col">
            <div
              ref={threadRef}
              aria-live="polite"
              className="cc-scroll flex max-h-[38dvh] min-h-32 flex-col gap-3 overflow-y-auto pr-1 md:max-h-none md:flex-1"
            >
              {session.chat.map((m, i) =>
                m.role === "user" ? (
                  <div
                    key={i}
                    className="ml-8 self-end rounded-2xl rounded-br-md border border-brand-500/30 bg-brand-500/10 px-3.5 py-2 text-sm leading-relaxed text-zinc-100"
                  >
                    {m.text}
                  </div>
                ) : (
                  <div
                    key={i}
                    className="mr-8 self-start rounded-2xl rounded-bl-md border border-line bg-ink-800 px-3.5 py-2"
                  >
                    <p className="mb-0.5 font-mono text-[10px] font-medium uppercase tracking-[0.18em] text-brand-400">
                      Catalyst
                    </p>
                    <p className="text-sm leading-relaxed text-zinc-200">{m.text}</p>
                  </div>
                )
              )}
            </div>

            {/* Quick chips */}
            <div className="mt-4 flex flex-wrap gap-1.5">
              {quickChips.map((c) => (
                <button
                  key={c.label}
                  type="button"
                  onClick={c.act}
                  className="rounded-full border border-line px-3 py-1 font-mono text-xs text-zinc-400 transition-colors duration-150 hover:border-line-strong hover:text-zinc-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                >
                  {c.label}
                </button>
              ))}
            </div>

            {/* Chat input */}
            <form onSubmit={handleSubmit} className="mt-3 flex gap-2">
              <input
                ref={inputRef}
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={placeholder}
                aria-label={`Edit command for clip ${index + 1}`}
                className="h-10 min-w-0 flex-1 rounded-full border border-line bg-ink-950 px-4 text-sm text-zinc-200 placeholder:text-zinc-600 transition-colors hover:border-line-strong focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
              />
              <button
                type="submit"
                aria-label="Send edit command"
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-line-strong bg-white/5 text-zinc-300 transition-colors hover:border-brand-400/60 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
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
                  <path d="M5 12h13M13 6l6 6-6 6" />
                </svg>
              </button>
            </form>

            {/* Pending-edit chips from describeEdits */}
            {chips.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {chips.map((c, i) => (
                  <span
                    key={i}
                    className="rounded-full border border-brand-500/30 bg-brand-500/10 px-2.5 py-1 font-mono text-[11px] text-brand-300"
                  >
                    {c}
                  </span>
                ))}
              </div>
            ) : null}

            {/* Actions */}
            <div className="mt-4 border-t border-line pt-4">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={onRerender}
                  disabled={!canRerender}
                  className="inline-flex min-w-0 flex-1 basis-40 items-center justify-center gap-2 rounded-full bg-brand-600 px-5 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:bg-brand-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-brand-600"
                >
                  {session.rendering !== null
                    ? `Re-rendering… ${Math.round(session.rendering * 100)}%`
                    : "Re-render clip"}
                </button>
                <a
                  href={previewUrl}
                  download={filename}
                  aria-label={`Download clip ${index + 1} as ${currentExt.toUpperCase()}`}
                  className="inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                >
                  Download
                </a>
                <ShareButton
                  url={previewUrl}
                  blob={currentBlob}
                  filename={filename}
                  title={clip.title}
                  ariaLabel={`Share clip ${index + 1}`}
                />
              </div>
              {session.rendering !== null ? (
                <div
                  className="mt-3 h-1 w-full overflow-hidden rounded-full bg-white/10"
                  role="progressbar"
                  aria-label="Re-render progress"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Math.round(session.rendering * 100)}
                >
                  <div
                    className="h-full rounded-full bg-brand-500 transition-[width] duration-300 ease-out"
                    style={{ width: `${Math.round(session.rendering * 100)}%` }}
                  />
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}
