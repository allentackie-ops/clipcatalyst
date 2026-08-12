"use client";

// "Post to YouTube" on one clip (PUBLISH.md Part 4): a chip, a small sheet,
// and the upload it starts.
//
// The rules it is built to keep:
//
//   * It renders NOTHING unless a post could really complete — the platform
//     has an adapter in this build, and this account has a channel connected.
//     PUBLISH.md's non-negotiable is that the UI never offers a platform that
//     cannot finish a post, and the honest way to keep it is to not draw the
//     control rather than to draw one that explains itself afterwards.
//   * The privacy line is the SERVER's sentence, not ours. While Google's
//     review is outstanding every upload is forced to private — by Google —
//     and the sheet says exactly that, from `platform.note`. When the review
//     lands the same field says something else and this file does not change.
//   * The visibility control is built by rendering `privacy_choices`. A list
//     of one renders no control at all, so it is not possible for this sheet
//     to offer an option the server would silently override.
//   * Closing the sheet does not cancel anything. The job lives here, above
//     the sheet, so an upload keeps running (and keeps being polled) while
//     the sheet is shut — and reopening shows where it got to.
//
// The native share sheet on the same card is untouched and stays: it is the
// only path that works on a phone with no connections, and for TikTok and
// Instagram it remains the real answer until their reviews land.

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { Badge } from "@/components/ui";
import { cloudEnabled } from "@/components/studio/cloud";
import {
  AccountError,
  connectionFor,
  getPublish,
  platformFor,
  publishClip,
  publishSettled,
  type PublishJob,
} from "@/lib/account";
import { useConnections } from "./ConnectionsProvider";

/** The platform this control posts to. One adapter today; the component is
 *  written against the catalogue entry rather than against the name, so a
 *  second adapter is a second constant and not a second component. */
const PLATFORM = "youtube";

/** YouTube's own ceilings (publish/youtube.py). The title is clamped at the
 *  input rather than at the server's laxer 300, because 100 is where the
 *  adapter cuts — a creator should see the limit that will really apply
 *  instead of losing the end of a title they were allowed to type. */
const MAX_TITLE = 100;
const MAX_DESCRIPTION = 5000;

/** Poll cadence for an upload in flight — the same 1.5 s the render job poll
 *  uses (components/studio/cloud.ts), because it is the same kind of wait. */
const POLL_INTERVAL_MS = 1500;

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

/** What each visibility means, said in the sheet rather than assumed. */
const PRIVACY_LABELS: Record<string, { label: string; hint: string }> = {
  public: { label: "Public", hint: "Anyone can find and watch it" },
  unlisted: { label: "Unlisted", hint: "Only people with the link" },
  private: { label: "Private", hint: "Only you, until you publish it" },
};

function privacyLabel(value: string): string {
  return PRIVACY_LABELS[value]?.label ?? value;
}

/** The default visibility for a fresh sheet: the most private option the
 *  platform offers. Posting is one click and undoing a public video is not,
 *  so the safe end of the list is where this starts. */
function defaultPrivacy(choices: string[]): string {
  if (choices.length === 0) return "private";
  return choices.includes("private") ? "private" : choices[choices.length - 1];
}

/** The line under a finished post. It reads the privacy the job RESOLVED to
 *  — what really happened — never what the sheet asked for. */
function landedLine(privacy: string): string {
  if (privacy === "private") {
    return "It's on your channel as a private video. Publish it from YouTube whenever you're ready.";
  }
  if (privacy === "unlisted") {
    return "It's on your channel as an unlisted video — anyone with the link can watch it.";
  }
  return "It's live on your channel.";
}

/** The card chip, in the two heights the grids it lives in already use: the
 *  library's action row sits at py-2, Studio's at py-2.5. Written as two whole
 *  class strings rather than a base plus an override, because Tailwind
 *  resolves conflicting utilities by stylesheet order and not by the order
 *  they appear in a className. */
const CHIP_CLS = {
  sm: "inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400",
  md: "inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400",
} as const;

const FIELD_CLS =
  "w-full rounded-2xl border border-line-strong bg-white/5 px-4 py-3 text-sm text-white outline-none transition-colors placeholder:text-zinc-600 focus:border-brand-400/70 focus:ring-2 focus:ring-brand-400/40";

/** The YouTube glyph, drawn rather than fetched — no external assets, and no
 *  third-party mark reproduced: a rounded screen with a play triangle. */
function PlayGlyph() {
  return (
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
      <rect x="2.5" y="5" width="19" height="14" rx="4" />
      <path d="M10.5 9.25 15 12l-4.5 2.75V9.25Z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export default function PostToYouTube({
  clipId,
  clipTitle,
  topHook = "",
  ariaSuffix,
  size = "md",
}: {
  /** The LIBRARY clip's id — the only thing that can be posted. A Studio clip
   *  that has not been saved has none, and gets no control (its card offers
   *  "Save to library" instead, which is the click that creates one). */
  clipId: string;
  /** Seeds the title field. The server falls back to the clip's own title for
   *  an empty one, so this is a starting point rather than a requirement. */
  clipTitle: string;
  /** The clip's best hook — what the server writes as the description when
   *  the field is left blank, shown here as the placeholder so leaving it
   *  blank is an informed choice rather than a blank space. */
  topHook?: string;
  /** Distinguishes this card's controls in a grid of them ("clip 2"). */
  ariaSuffix?: string;
  /** Which action row this chip is joining — the library's (`sm`) or
   *  Studio's (`md`). Purely the chip's height; nothing else differs. */
  size?: "sm" | "md";
}) {
  const { connections, refresh } = useConnections();
  const platform = platformFor(connections, PLATFORM);
  const connection = connectionFor(connections, PLATFORM);

  const [open, setOpen] = useState(false);
  const [job, setJob] = useState<PublishJob | null>(null);
  const [posting, setPosting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [title, setTitle] = useState(() => clipTitle.slice(0, MAX_TITLE));
  const [description, setDescription] = useState("");
  const [privacy, setPrivacy] = useState("private");

  const deadRef = useRef(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  /** Where focus goes when the sheet closes on a post that has LANDED: the
   *  trigger it was opened from no longer exists by then — it has become the
   *  "Posted to YouTube" badge — and focus falling to the top of the document
   *  is the one outcome closing a dialog must not have. */
  const doneRef = useRef<HTMLSpanElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  const fieldId = useId();
  const titleId = `${fieldId}-title`;
  const descriptionId = `${fieldId}-description`;
  const kickerId = `${fieldId}-kicker`;
  const headingId = `${fieldId}-heading`;
  const privacyId = `${fieldId}-privacy`;

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
    };
  }, []);

  const choices = platform?.privacy_choices ?? [];
  const forced = platform?.forced_privacy ?? "";
  const note = job?.note || platform?.note || "";
  const settled = job !== null && publishSettled(job);
  const uploading = job !== null && !settled;
  const label = ariaSuffix ? ` ${ariaSuffix}` : "";

  // What this post will be given, resolved against the server's list every
  // render rather than copied into state by an effect: a platform whose
  // capability changes under a long-lived page can never leave a stale pick
  // selected, and a forced value cannot be edited away by the radio group.
  const selectedPrivacy =
    forced || (choices.includes(privacy) ? privacy : defaultPrivacy(choices));

  // Poll while an upload is in flight. Independent of the sheet: shutting it
  // must not orphan a job that is still moving bytes. The loop reschedules
  // itself, so a snapshot that changes nothing but `progress` doesn't restart
  // the timer — and a settled job simply stops asking.
  const activeJobId = uploading ? job.id : null;
  useEffect(() => {
    if (activeJobId === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const fresh = await getPublish(activeJobId);
        if (cancelled || deadRef.current) return;
        setJob(fresh);
        if (publishSettled(fresh)) return;
      } catch {
        // A blip mid-upload is not a failed upload — the row on the server is
        // the truth and it is still there. Ask again on the next tick.
        if (cancelled || deadRef.current) return;
      }
      timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    };
    timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [activeJobId]);

  const closeSheet = useCallback(() => {
    setOpen(false);
    // The trigger is remounted by this state change, so hand focus back after
    // React has committed — the same wait the clip editor makes when it closes.
    requestAnimationFrame(() =>
      (triggerRef.current ?? doneRef.current)?.focus()
    );
  }, []);

  const openSheet = useCallback(() => {
    setError(null);
    setOpen(true);
  }, []);

  // Dialog plumbing, exactly as ClipEditor does it: focus in, scroll locked,
  // Escape closes, Tab is trapped.
  useEffect(() => {
    if (!open) return;
    titleRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeSheet();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeSheet]);

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

  const post = useCallback(
    async (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (posting || uploading) return;
      setError(null);
      // Only ever a settled job at this point (the guard above), so this is
      // last time's failure being cleared — not an upload being abandoned.
      setJob(null);
      setPosting(true);
      try {
        const started = await publishClip(clipId, {
          platform: PLATFORM,
          title: title.trim(),
          description: description.trim(),
          privacy: selectedPrivacy,
        });
        if (deadRef.current) return;
        setJob(started);
      } catch (err) {
        if (deadRef.current) return;
        // The server's own sentence: an expired clip, a disconnected channel,
        // a platform this build can't post to. Each says something true that
        // a generic message would throw away.
        setError(
          err instanceof Error
            ? err.message
            : "Couldn't start that upload — try again."
        );
        // 409 is the "something changed underneath you" status — in this
        // route, a channel disconnected somewhere else, or a clip whose file
        // expired. Only that one re-reads the snapshot; refreshing after a
        // network blip would be a request that could not tell us anything.
        if (err instanceof AccountError && err.status === 409) void refresh();
      } finally {
        if (!deadRef.current) setPosting(false);
      }
    },
    [clipId, description, posting, refresh, selectedPrivacy, title, uploading]
  );

  // Nothing to offer: no API, no session, no adapter for this platform in
  // this build, or no channel connected to post to. Each of those means a
  // post cannot complete, and a control that cannot complete is not drawn.
  //
  // A control already in USE is the one exception. If the channel is
  // disconnected in another tab while this sheet is open, the honest thing is
  // to let the sheet finish its sentence — vanishing mid-explanation would
  // take the server's message away with it — and to be gone on the next
  // paint of a card that is not mid-post.
  const offerable =
    platform !== null && platform.publishable && connection !== null;
  if (!cloudEnabled) return null;
  if (!offerable && !open && job === null) return null;

  const pct = Math.round(Math.min(Math.max(job?.progress ?? 0, 0), 1) * 100);
  const videoUrl = job?.video_url ?? "";

  // Portal to <body>: the results grid and the account dashboard both sit
  // inside an animate-rise wrapper whose settled transform would otherwise
  // become the containing block for position:fixed.
  const sheet = !open
    ? null
    : createPortal(
        <div className="fixed inset-0 z-50 flex items-end justify-center p-0 sm:items-center sm:p-6">
          <div
            aria-hidden
            onMouseDown={closeSheet}
            className="absolute inset-0 bg-ink-950/80 backdrop-blur-sm"
          />
          {/* Labelled by BOTH header lines, so the dialog announces what it
              is and which clip it is for: "Post to YouTube, <clip title>". */}
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={`${kickerId} ${headingId}`}
            onKeyDown={trapTab}
            className="relative flex max-h-[92dvh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl border border-line-strong bg-ink-900 shadow-[0_24px_80px_rgba(0,0,0,0.6)] sm:rounded-2xl"
          >
            <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
              <div className="min-w-0">
                <p
                  id={kickerId}
                  className="font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-brand-400"
                >
                  Post to YouTube
                </p>
                <h2
                  id={headingId}
                  className="truncate font-display text-lg font-semibold tracking-tight text-white"
                >
                  {clipTitle || "Untitled clip"}
                </h2>
                <p className="mt-0.5 truncate text-xs text-zinc-500">
                  {connection?.account_name
                    ? `Uploading to ${connection.account_name}`
                    : "Uploading to your connected channel"}
                </p>
              </div>
              <button
                type="button"
                onClick={closeSheet}
                aria-label="Close"
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
                  <path d="M6 6l12 12M18 6 6 18" />
                </svg>
              </button>
            </div>

            <div className="cc-scroll min-h-0 flex-1 overflow-y-auto px-5 py-5">
              {job !== null && job.status === "done" ? (
                <div className="flex flex-col gap-4">
                  <div className="rounded-2xl border border-signal-500/30 bg-signal-500/10 px-4 py-3.5">
                    <p className="text-sm font-medium text-white">
                      Posted to YouTube.
                    </p>
                    <p className="mt-1.5 text-xs leading-relaxed text-zinc-300">
                      {landedLine(job.privacy)}
                    </p>
                  </div>
                  {videoUrl ? (
                    <a
                      href={videoUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center justify-center gap-2 self-start rounded-full bg-gradient-to-r from-brand-600 to-spark-500 px-5 py-2.5 text-sm font-medium text-white shadow-[0_0_24px_rgba(139,92,246,0.35)] transition-all duration-200 hover:brightness-110 hover:shadow-[0_0_36px_rgba(139,92,246,0.55)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                    >
                      Watch on YouTube
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
                        <path d="M7 17 17 7M9 7h8v8" />
                      </svg>
                    </a>
                  ) : null}
                  <p className="text-xs leading-relaxed text-zinc-500">
                    The clip stays in your library — posting copies it, it never
                    moves it.
                  </p>
                </div>
              ) : uploading ? (
                <div className="flex flex-col gap-4">
                  <div className="flex flex-col gap-2">
                    <div
                      role="progressbar"
                      aria-label="Uploading this clip to YouTube"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={pct}
                      className="h-1.5 w-full overflow-hidden rounded-full bg-white/10"
                    >
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-brand-500 to-spark-400 transition-[width] duration-300 ease-out"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <p className="font-mono text-[11px] text-zinc-500">
                      {job.detail || "Uploading"} —{" "}
                      <span className="tabular-nums">{pct}%</span>
                    </p>
                  </div>
                  {note ? (
                    <p className="rounded-xl border border-line-strong bg-white/[0.04] px-4 py-3 text-xs leading-relaxed text-zinc-400">
                      {note}
                    </p>
                  ) : null}
                  <p className="text-xs leading-relaxed text-zinc-500">
                    You can close this — the upload keeps going, and this button
                    picks it back up.
                  </p>
                </div>
              ) : (
                <form onSubmit={(e) => void post(e)} className="flex flex-col gap-5">
                  <div className="flex flex-col gap-2">
                    <div className="flex items-baseline justify-between gap-3">
                      <label
                        htmlFor={titleId}
                        className="text-sm font-medium text-zinc-300"
                      >
                        Title
                      </label>
                      <span className="font-mono text-[11px] tabular-nums text-zinc-600">
                        {title.length}/{MAX_TITLE}
                      </span>
                    </div>
                    <input
                      ref={titleRef}
                      id={titleId}
                      type="text"
                      maxLength={MAX_TITLE}
                      value={title}
                      placeholder={clipTitle || "Your clip's title"}
                      onChange={(e) =>
                        setTitle(e.target.value.slice(0, MAX_TITLE))
                      }
                      className={FIELD_CLS}
                    />
                    <p className="text-xs leading-relaxed text-zinc-500">
                      YouTube&rsquo;s limit is {MAX_TITLE} characters. Leave
                      this blank and the clip&rsquo;s own title is used.
                    </p>
                  </div>

                  <div className="flex flex-col gap-2">
                    <label
                      htmlFor={descriptionId}
                      className="text-sm font-medium text-zinc-300"
                    >
                      Description
                    </label>
                    <textarea
                      id={descriptionId}
                      rows={4}
                      maxLength={MAX_DESCRIPTION}
                      value={description}
                      placeholder={topHook || "Say something about this clip…"}
                      onChange={(e) =>
                        setDescription(e.target.value.slice(0, MAX_DESCRIPTION))
                      }
                      className={`${FIELD_CLS} resize-y`}
                    />
                    {topHook ? (
                      <p className="text-xs leading-relaxed text-zinc-500">
                        Left blank, the description becomes this clip&rsquo;s top
                        hook — “{topHook}”.
                      </p>
                    ) : null}
                  </div>

                  {/* A choice only when there is one. `privacy_choices` with a
                      single entry is the honest state while Google's review is
                      outstanding, and rendering the list is what makes it
                      impossible to show an option the server would override. */}
                  {choices.length > 1 ? (
                    <div className="flex flex-col gap-2">
                      <p
                        id={privacyId}
                        className="text-sm font-medium text-zinc-300"
                      >
                        Visibility
                      </p>
                      {/* Native radios inside the group the rest of the site
                          uses (StudioApp's engine toggle): arrow keys, Space
                          and form submission all come for free, and the
                          labelled group is what a screen reader announces. */}
                      <div
                        role="radiogroup"
                        aria-labelledby={privacyId}
                        className="flex flex-col gap-2"
                      >
                        {choices.map((choice) => {
                          const copy = PRIVACY_LABELS[choice];
                          const selected = selectedPrivacy === choice;
                          return (
                            <label
                              key={choice}
                              className={`flex cursor-pointer items-start gap-3 rounded-2xl border px-4 py-3 transition-colors ${
                                selected
                                  ? "border-brand-400/60 bg-brand-500/10"
                                  : "border-line-strong bg-white/[0.03] hover:border-line-strong hover:bg-white/[0.06]"
                              }`}
                            >
                              <input
                                type="radio"
                                name={`${fieldId}-visibility`}
                                value={choice}
                                checked={selected}
                                onChange={() => setPrivacy(choice)}
                                className="mt-1 h-4 w-4 shrink-0 accent-brand-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                              />
                              <span className="min-w-0">
                                <span className="block text-sm font-medium text-white">
                                  {copy?.label ?? choice}
                                </span>
                                {copy?.hint ? (
                                  <span className="block text-xs leading-relaxed text-zinc-500">
                                    {copy.hint}
                                  </span>
                                ) : null}
                              </span>
                            </label>
                          );
                        })}
                      </div>
                    </div>
                  ) : null}

                  {/* The honest line about what a post here really does today.
                      Server copy, so the day the review lands it changes on its
                      own. */}
                  {note ? (
                    <p className="rounded-xl border border-line-strong bg-white/[0.04] px-4 py-3 text-xs leading-relaxed text-zinc-400">
                      {note}
                    </p>
                  ) : null}

                  <div aria-live="polite" className="empty:hidden">
                    {job !== null && job.status === "failed" ? (
                      <p
                        role="alert"
                        className="text-sm leading-relaxed text-ember-300"
                      >
                        {job.error ||
                          "That upload didn't finish. The clip is untouched in your library."}
                      </p>
                    ) : error ? (
                      <p
                        role="alert"
                        className="text-sm leading-relaxed text-ember-300"
                      >
                        {error}
                      </p>
                    ) : null}
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="submit"
                      aria-busy={posting}
                      className="inline-flex min-w-0 flex-1 basis-40 items-center justify-center gap-2 rounded-full bg-gradient-to-r from-brand-600 to-spark-500 px-5 py-2.5 text-sm font-medium text-white shadow-[0_0_24px_rgba(139,92,246,0.35)] transition-all duration-200 hover:brightness-110 hover:shadow-[0_0_36px_rgba(139,92,246,0.55)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                    >
                      <PlayGlyph />
                      {posting
                        ? "Starting the upload…"
                        : job !== null && job.status === "failed"
                          ? "Post again"
                          : `Post as ${privacyLabel(selectedPrivacy)}`}
                    </button>
                    <button
                      type="button"
                      onClick={closeSheet}
                      className="rounded-full px-3 py-2.5 text-sm text-zinc-300 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </div>
          </div>
        </div>,
        document.body,
      );

  return (
    <>
      {job !== null && job.status === "done" ? (
        <span
          ref={doneRef}
          tabIndex={-1}
          className="inline-flex flex-wrap items-center gap-2 outline-none"
        >
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
            Posted to YouTube
          </Badge>
          {videoUrl ? (
            <a
              href={videoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-lg text-xs text-brand-300 underline-offset-4 transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            >
              Watch on YouTube →
            </a>
          ) : null}
        </span>
      ) : (
        <button
          ref={triggerRef}
          type="button"
          onClick={openSheet}
          aria-haspopup="dialog"
          aria-busy={uploading}
          aria-label={`Post${label} to YouTube`}
          className={CHIP_CLS[size]}
        >
          <PlayGlyph />
          {uploading
            ? `Posting… ${pct}%`
            : job?.status === "failed"
              ? "Posting failed — try again"
              : "Post to YouTube"}
        </button>
      )}

      {/* Progress and outcome are announced once, as transitions — the bar in
          the sheet carries the numbers, and a screen reader counting to 100
          out loud is noise (the same rule "Save to library" follows). */}
      <p className="sr-only" aria-live="polite">
        {job === null
          ? ""
          : job.status === "done"
            ? `Clip posted to YouTube as a ${job.privacy} video.`
            : job.status === "failed"
              ? `Posting to YouTube failed. ${job.error ?? ""}`
              : `Posting this clip to YouTube.`}
      </p>

      {sheet}
    </>
  );
}
