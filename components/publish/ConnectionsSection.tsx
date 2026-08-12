"use client";

// Connections on /account (PUBLISH.md Part 4): one row per platform this
// product knows about, connected or not.
//
// Every platform is listed, including the ones that cannot work yet, and the
// rule that decides what a row shows is PUBLISH.md's: a platform that cannot
// currently complete a post says so plainly instead of offering a dead button.
// TikTok and Instagram are exactly that — known, authorized by nobody, waiting
// on an audit and an App Review — so their rows are a sentence and no control.
//
// The sentence is the SERVER's (`platform.reason`), not a string kept here:
// whether a channel can be connected depends on a token key, a client id, a
// public redirect URL and a review landing, and none of those are things the
// frontend can observe. When one changes, this section changes with it and
// this file does not.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Badge, Button, Card } from "@/components/ui";
import {
  disconnectConnection,
  startConnection,
  type Connection,
  type PublishPlatform,
} from "@/lib/account";
import { useConnections } from "./ConnectionsProvider";

/** The sentences behind `/account?connect_error=…`. The server picks the code
 *  — it is the only side that knows what happened — and the frontend renders
 *  the words, so a message can be improved without a deploy of the API. */
const CONNECT_ERRORS: Record<string, string> = {
  denied:
    "That connection was cancelled at the platform's consent screen — nothing was connected.",
  refused:
    "The platform refused that connection. Nothing was stored — please try connecting again.",
  unavailable:
    "We couldn't reach the platform just now, so nothing was connected. Try again in a moment.",
  no_account:
    "That account approved us but has no channel to post to yet. Create one on the platform, then connect again.",
};

/** "11 Aug 2026", in the visitor's own locale; "" for a date we can't read. */
function formatDate(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function PlatformRow({
  platform,
  connection,
  busy,
  error,
  onConnect,
  onDisconnect,
}: {
  platform: PublishPlatform;
  /** This account's channel on it, or null. */
  connection: Connection | null;
  /** True while this row's own request is in flight. */
  busy: boolean;
  /** A failure that belongs to this row, said on this row. */
  error: string | null;
  onConnect: (platform: string) => void;
  onDisconnect: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const disconnectRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const fieldId = useId();
  const confirmId = `${fieldId}-confirm`;

  // Opening the confirmation moves focus onto it: the question and its answer
  // must be where the keyboard already is.
  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  const cancel = useCallback(() => {
    setConfirming(false);
    // The Disconnect button is remounted BY this state change, so hand focus
    // back after React has committed.
    requestAnimationFrame(() => disconnectRef.current?.focus());
  }, []);

  const connectedAt = connection ? formatDate(connection.created_at) : "";

  return (
    <li className="flex flex-col gap-3 py-4 last:pb-0">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="flex flex-wrap items-baseline gap-2">
            <span className="font-display text-base font-semibold tracking-tight text-white">
              {platform.label}
            </span>
            {connection ? (
              <Badge tone="signal">Connected</Badge>
            ) : platform.connectable ? null : (
              <Badge tone="neutral">Not available yet</Badge>
            )}
          </p>
          {connection ? (
            <p className="mt-1 truncate text-sm text-zinc-300">
              {connection.account_name || "Your channel"}
            </p>
          ) : (
            // The honest sentence for a platform that cannot be connected —
            // and, when it can, what connecting one is FOR.
            <p className="mt-1 text-sm leading-relaxed text-zinc-400">
              {platform.reason ||
                `Post clips straight to ${platform.label} from your library and from Studio.`}
            </p>
          )}
          {connection && connectedAt ? (
            <p className="mt-1 font-mono text-xs text-zinc-600">
              Connected {connectedAt}
            </p>
          ) : null}
        </div>

        {/* No control at all for a platform that cannot complete a post —
            PUBLISH.md's rule, kept by not drawing the button. */}
        {connection ? (
          confirming ? null : (
            <button
              ref={disconnectRef}
              type="button"
              onClick={() => setConfirming(true)}
              aria-label={`Disconnect ${platform.label}`}
              className="shrink-0 self-start rounded-full px-3 py-2 text-sm text-zinc-400 transition-colors hover:text-ember-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 sm:self-auto"
            >
              Disconnect
            </button>
          )
        ) : platform.connectable ? (
          <Button
            variant="secondary"
            className="shrink-0 self-start sm:self-auto"
            onClick={() => onConnect(platform.platform)}
          >
            {busy ? "Opening…" : `Connect ${platform.label}`}
          </Button>
        ) : null}
      </div>

      {/* The standing caveat about posting here, shown whether or not it is
          connected: it is what a post to this platform will really do. */}
      {platform.note && (connection || platform.connectable) ? (
        <p className="text-xs leading-relaxed text-zinc-500">{platform.note}</p>
      ) : null}

      {confirming && connection ? (
        <div
          role="group"
          aria-labelledby={confirmId}
          className="flex flex-col gap-2.5 rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-3"
        >
          <p id={confirmId} className="text-sm leading-relaxed text-ember-300">
            Disconnect {platform.label}? We revoke our access with them and
            delete the stored tokens. Clips already posted stay on your channel.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <button
              ref={confirmRef}
              type="button"
              aria-busy={busy}
              onClick={() => onDisconnect(connection.id)}
              className="inline-flex items-center justify-center rounded-full bg-ember-500/90 px-4 py-2 text-sm font-medium text-ink-950 transition-colors duration-200 hover:bg-ember-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ember-400"
            >
              {busy ? "Disconnecting…" : "Yes, disconnect"}
            </button>
            <button
              type="button"
              onClick={cancel}
              className="rounded-full px-3 py-2 text-sm text-zinc-300 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            >
              Keep it connected
            </button>
          </div>
        </div>
      ) : null}

      <div aria-live="polite" className="empty:hidden">
        {error ? (
          <p role="alert" className="text-xs leading-relaxed text-ember-300">
            {error}
          </p>
        ) : null}
      </div>
    </li>
  );
}

export default function ConnectionsSection({
  /** `?connected=youtube` — the callback landed and stored a channel. */
  connected,
  /** `?connect_error=<code>` — the callback came back without one. */
  connectError,
}: {
  connected: string | null;
  connectError: string | null;
}) {
  const { connections, loading, error, refresh } = useConnections();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [announcement, setAnnouncement] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const deadRef = useRef(false);

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
    };
  }, []);

  const message = (e: unknown, fallback: string): string =>
    e instanceof Error ? e.message : fallback;

  const setRowError = useCallback((key: string, text: string | null) => {
    setRowErrors((current) => {
      if (text === null) {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      }
      return { ...current, [key]: text };
    });
  }, []);

  const handleConnect = useCallback(
    async (platform: string) => {
      if (busyId !== null) return;
      setBusyId(platform);
      setRowError(platform, null);
      try {
        const { authorize_url } = await startConnection(platform);
        // A full navigation, not a popup: the consent screen is a whole page,
        // and phone browsers block popups opened from a fetch callback. The
        // busy state stays set — we're leaving.
        window.location.assign(authorize_url);
      } catch (e) {
        if (deadRef.current) return;
        setBusyId(null);
        setRowError(
          platform,
          message(e, "Couldn't start that connection — try again.")
        );
      }
    },
    [busyId, setRowError]
  );

  const handleDisconnect = useCallback(
    async (id: string) => {
      if (busyId !== null) return;
      setBusyId(id);
      setRowError(id, null);
      try {
        await disconnectConnection(id);
        if (deadRef.current) return;
        await refresh();
        if (deadRef.current) return;
        setAnnouncement("Channel disconnected.");
        // The control that held focus has just gone — put it somewhere real
        // rather than letting it fall back to the top of the document.
        headingRef.current?.focus();
      } catch (e) {
        if (deadRef.current) return;
        // A 502 means the provider couldn't be reached and NOTHING was
        // removed — the server says so, and saying anything softer here would
        // leave somebody believing a channel had been disconnected.
        setRowError(id, message(e, "Couldn't disconnect that channel — try again."));
      } finally {
        if (!deadRef.current) setBusyId(null);
      }
    },
    [busyId, refresh, setRowError]
  );

  const platforms = connections?.platforms ?? [];
  const connectedPlatform = connected
    ? (platforms.find((p) => p.platform === connected)?.label ?? connected)
    : "";

  return (
    <section aria-labelledby="connections-heading">
      <div className="flex flex-col gap-1">
        <h2
          id="connections-heading"
          ref={headingRef}
          tabIndex={-1}
          className="font-display text-lg font-semibold tracking-tight text-white outline-none"
        >
          Connections
        </h2>
        <p className="text-sm leading-relaxed text-zinc-400">
          Connect a channel and a finished clip can go straight to it — from
          your library, or from Studio the moment it renders. Sharing from your
          phone works without any of this, and always will.
        </p>
      </div>

      <div aria-live="polite" className="sr-only">
        {announcement}
      </div>

      {/* The one-shot outcome of an authorization we just came back from. */}
      <div aria-live="polite" className="mt-4 flex flex-col gap-3 empty:hidden">
        {connected ? (
          <div className="rounded-2xl border border-signal-500/30 bg-signal-500/10 px-5 py-4">
            <p className="text-sm font-medium text-white">
              {connectedPlatform} connected.
            </p>
          </div>
        ) : null}
        {connectError ? (
          <div className="rounded-2xl border border-ember-500/30 bg-ember-500/[0.06] px-5 py-4">
            <p role="alert" className="text-sm leading-relaxed text-ember-300">
              {CONNECT_ERRORS[connectError] ??
                "That connection didn't finish, and nothing was stored. Try again."}
            </p>
          </div>
        ) : null}
      </div>

      {loading && connections === null ? (
        <p
          role="status"
          className="mt-6 animate-pulse-soft text-sm text-zinc-500"
        >
          Loading your connections…
        </p>
      ) : error !== null && connections === null ? (
        <Card className="mt-6 flex flex-col items-start gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <p role="alert" className="text-sm leading-relaxed text-ember-300">
            {error}
          </p>
          <Button
            variant="secondary"
            className="shrink-0"
            onClick={() => void refresh()}
          >
            Try again
          </Button>
        </Card>
      ) : platforms.length === 0 ? (
        <Card className="mt-6 p-6 sm:p-7">
          <p className="text-sm leading-relaxed text-zinc-400">
            This server doesn&rsquo;t offer any publishing destinations yet.
            Share a clip from its card to post it from your phone.
          </p>
        </Card>
      ) : (
        <Card className="mt-6 p-6 sm:p-7">
          <ul className="flex flex-col divide-y divide-line">
            {platforms.map((platform) => {
              const connection =
                connections?.connections.find(
                  (c) => c.platform === platform.platform
                ) ?? null;
              const key = connection ? connection.id : platform.platform;
              return (
                <PlatformRow
                  key={platform.platform}
                  platform={platform}
                  connection={connection}
                  busy={busyId === key}
                  error={rowErrors[key] ?? null}
                  onConnect={(id) => void handleConnect(id)}
                  onDisconnect={(id) => void handleDisconnect(id)}
                />
              );
            })}
          </ul>
        </Card>
      )}
    </section>
  );
}
