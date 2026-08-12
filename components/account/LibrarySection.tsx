"use client";

// The Library on /account (LIBRARY.md Part 3): every clip this account has
// kept, newest first, paged on the server's own cursor.
//
// The section says out loud how long clips are kept, and says it from
// /v1/me's `retention_days` rather than from a table of plans copied into the
// frontend — the sentence on screen is then the one the reaper actually
// enforces, and a pricing change cannot leave a stale promise here.
//
// Deleting is the only thing that removes a row. Retention deletes files and
// keeps rows, which is why a card can be "expired" and still complete: the
// grid renders those from the same list, with no player and no broken url.

import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Card } from "@/components/ui";
import {
  AccountError,
  deleteClip,
  effectivePlan,
  listClips,
  type AccountUser,
  type LibraryClip,
} from "@/lib/account";
import { useConnections } from "@/components/publish/ConnectionsProvider";
import { useAccount } from "./AccountProvider";
import LibraryClipCard from "./LibraryClipCard";

/**
 * The retention sentence for this plan. Three distinct states, because
 * /v1/me has three:
 *
 *   a number    — the window, plus what survives it;
 *   null        — kept as long as the account is (enterprise);
 *   undefined   — a server from before the library, which has promised
 *                 nothing, so neither does this.
 */
function retentionLine(
  retentionDays: number | null | undefined,
  planLabel: string
): string {
  if (retentionDays === undefined) return "";
  if (retentionDays === null) {
    return `On ${planLabel}, saved clips are kept for as long as your account is open.`;
  }
  return (
    `On ${planLabel}, a saved clip's video is kept for ${retentionDays} days. ` +
    "After that the file is deleted and the card stays: title, score and " +
    "hooks are permanent."
  );
}

export default function LibrarySection({
  user,
  planLabel,
}: {
  user: AccountUser;
  /** The display name of the EFFECTIVE plan — the account page owns that copy. */
  planLabel: string;
}) {
  const [clips, setClips] = useState<LibraryClip[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteErrors, setDeleteErrors] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState("");

  const { refresh } = useAccount();
  const { connections } = useConnections();
  const deadRef = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
    };
  }, []);

  const message = (e: unknown, fallback: string): string =>
    e instanceof Error ? e.message : fallback;

  /**
   * A 401 means the session went between /v1/me and this call. There is
   * nothing to say about the library at that point — re-reading /v1/me settles
   * the whole page to signed-out, which is the honest state, rather than
   * leaving an error card on a dashboard that is no longer signed in.
   */
  const expired = useCallback(
    (e: unknown): boolean => {
      if (!(e instanceof AccountError) || e.status !== 401) return false;
      void refresh();
      return true;
    },
    [refresh]
  );

  /** The first page — on mount, and again behind "Try again". */
  const loadFirstPage = useCallback(async () => {
    setPhase("loading");
    setError(null);
    try {
      const page = await listClips();
      if (deadRef.current) return;
      setClips(page.clips);
      setCursor(page.next_before);
      setPhase("ready");
    } catch (e) {
      if (deadRef.current || expired(e)) return;
      setError(message(e, "Couldn't load your library. Try again."));
      setPhase("error");
    }
  }, [expired]);

  useEffect(() => {
    void loadFirstPage();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (cursor === null || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await listClips(cursor);
      if (deadRef.current) return;
      // Append by id, not by concatenation alone: a clip deleted between two
      // pages shifts the window, and a repeat would otherwise duplicate a key.
      setClips((current) => {
        const seen = new Set(current.map((clip) => clip.id));
        return [...current, ...page.clips.filter((clip) => !seen.has(clip.id))];
      });
      setCursor(page.next_before);
      setAnnouncement(
        page.clips.length === 1
          ? "1 more clip loaded."
          : `${page.clips.length} more clips loaded.`
      );
    } catch (e) {
      if (deadRef.current || expired(e)) return;
      setError(message(e, "Couldn't load more clips. Try again."));
    } finally {
      if (!deadRef.current) setLoadingMore(false);
    }
  }, [cursor, expired, loadingMore]);

  const handleDelete = useCallback(
    async (id: string) => {
      if (deletingId !== null) return;
      setDeletingId(id);
      setDeleteErrors((current) => {
        if (!(id in current)) return current;
        const next = { ...current };
        delete next[id];
        return next;
      });
      try {
        await deleteClip(id);
        if (deadRef.current) return;
        setClips((current) => current.filter((clip) => clip.id !== id));
        setAnnouncement("Clip deleted.");
        // The card that held focus has just gone — put it somewhere real
        // instead of letting it fall back to the top of the document.
        headingRef.current?.focus();
      } catch (e) {
        if (deadRef.current || expired(e)) return;
        setDeleteErrors((current) => ({
          ...current,
          [id]: message(e, "Couldn't delete that clip. Try again."),
        }));
      } finally {
        if (!deadRef.current) setDeletingId(null);
      }
    },
    [deletingId, expired]
  );

  // A platform this build can really post to, that nothing is connected to
  // yet. Cards carry no Post button in that state — there is nowhere for a
  // post to go — so the section says where one comes from instead. A link to
  // the section below, never a button that would fail (PUBLISH.md).
  const unconnected =
    connections?.platforms.filter(
      (platform) =>
        platform.publishable &&
        platform.connectable &&
        !connections.connections.some((c) => c.platform === platform.platform)
    ) ?? [];

  const retention = retentionLine(user.entitlements.retention_days, planLabel);
  const lapsedNote =
    effectivePlan(user) === "free" && user.plan !== "free"
      ? " Clips saved on your old plan keep the window they were saved with, because a plan change never shortens one."
      : "";

  return (
    <section aria-labelledby="library-heading">
      <div className="flex flex-col gap-1">
        <h2
          id="library-heading"
          ref={headingRef}
          tabIndex={-1}
          className="font-display text-lg font-semibold tracking-tight text-white outline-none"
        >
          Library
        </h2>
        <p className="text-sm leading-relaxed text-zinc-400">
          Every clip you&rsquo;ve kept. Cloud renders land here on their own,
          and browser clips arrive when you press “Save to library” in Studio.
        </p>
        {retention ? (
          <p className="mt-1 text-xs leading-relaxed text-zinc-500">
            {retention}
            {lapsedNote}
          </p>
        ) : null}
        {unconnected.length > 0 ? (
          <p className="mt-1 text-xs leading-relaxed text-zinc-500">
            {unconnected.map((platform) => platform.label).join(" and ")} can
            take a clip straight from here, so{" "}
            <a
              href="#connections-heading"
              className="rounded text-brand-300 underline-offset-4 transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            >
              connect a channel below
            </a>
            .
          </p>
        ) : null}
      </div>

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {phase === "loading" ? (
        <p
          role="status"
          className="mt-6 animate-pulse-soft text-sm text-zinc-500"
        >
          Loading your library…
        </p>
      ) : phase === "error" ? (
        <Card className="mt-6 flex flex-col items-start gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <p role="alert" className="text-sm leading-relaxed text-ember-300">
            {error}
          </p>
          <Button
            variant="secondary"
            className="shrink-0"
            onClick={() => void loadFirstPage()}
          >
            Try again
          </Button>
        </Card>
      ) : clips.length === 0 ? (
        <Card className="mt-6 flex flex-col items-start gap-4 p-6 sm:p-7">
          <p className="text-sm leading-relaxed text-zinc-400">
            Nothing saved yet. Clip a video in Studio: cloud clips are kept
            here automatically, and a browser clip is one click away once
            it&rsquo;s rendered.
          </p>
          <Button href="/studio" variant="secondary">
            Open Studio
          </Button>
        </Card>
      ) : (
        <>
          <div className="mt-6 grid gap-5 sm:grid-cols-2">
            {clips.map((clip) => (
              <LibraryClipCard
                key={clip.id}
                clip={clip}
                deleting={deletingId === clip.id}
                deleteError={deleteErrors[clip.id] ?? null}
                onDelete={(id) => void handleDelete(id)}
              />
            ))}
          </div>

          <div aria-live="polite" className="mt-4 empty:hidden">
            {error ? (
              <p role="alert" className="text-sm leading-relaxed text-ember-300">
                {error}
              </p>
            ) : null}
          </div>

          {cursor !== null ? (
            <div className="mt-6 flex justify-center">
              <Button variant="secondary" onClick={() => void loadMore()}>
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          ) : (
            <p className="mt-6 text-center font-mono text-xs text-zinc-600">
              {clips.length === 1 ? "1 clip" : `${clips.length} clips`}
            </p>
          )}
        </>
      )}
    </section>
  );
}
