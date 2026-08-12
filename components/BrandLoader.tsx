"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Mark } from "@/components/ui";
import {
  DEFAULT_TIMING,
  IDLE,
  nextDeadline,
  reduceLoader,
  type LoaderEvent,
  type LoaderState,
} from "@/lib/loader";

/**
 * The branded wait.
 *
 * `BrandLoader` is the animation and nothing else. `DelayedLoader` owns the
 * timing — the 180 ms delay and the 420 ms minimum — so no caller can get it
 * wrong, and `useDelayedVisibility` exposes that same machine to the one other
 * place that needs it (`components/RouteProgress.tsx`).
 *
 * The mark is the real `Mark` geometry from `components/ui.tsx`: imported, not
 * redrawn. Only transforms move, driven by CSS custom properties this file
 * sets on the wrapper and `app/globals.css` reads per `<path>`. A divergent
 * copy of the logo would rot; this one cannot.
 */

/**
 * Client-side clock, in one place so every timestamp the machine sees agrees.
 * Monotonic where it can be — a wall-clock step (NTP, a laptop waking) must
 * not strand a loader on screen or blink it away.
 */
const now = (): number =>
  typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();

/**
 * Drive the pure machine in `lib/loader.ts` from the browser: one piece of
 * state, and at most one live timer at any moment (`nextDeadline` yields a
 * single deadline per phase, so repeated starts cannot stack timers).
 */
export function useDelayedVisibility(
  delayMs: number = DEFAULT_TIMING.delayMs,
  minVisibleMs: number = DEFAULT_TIMING.minVisibleMs,
) {
  const [visible, setVisible] = useState(false);
  const stateRef = useRef<LoaderState>(IDLE);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);

  const dispatch = useCallback(
    function dispatch(type: LoaderEvent["type"]) {
      if (!aliveRef.current) return;
      const timing = { delayMs, minVisibleMs };
      const at = now();
      const next = reduceLoader(stateRef.current, { type, at } as LoaderEvent, timing);
      if (next === stateRef.current) return;
      stateRef.current = next;
      setVisible(next.visible);

      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      const deadline = nextDeadline(next, timing);
      if (deadline !== null) {
        timerRef.current = setTimeout(
          () => {
            timerRef.current = null;
            dispatch("tick");
          },
          Math.max(0, deadline - at),
        );
      }
    },
    [delayMs, minVisibleMs],
  );

  // Unmounting ends the wait for good: kill the pending timer and reset the
  // machine, so nothing fires into a component that is gone and a remount
  // (React's development double-invoke, or a second navigation) starts clean.
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = null;
      stateRef.current = IDLE;
    };
  }, []);

  const start = useCallback(() => dispatch("start"), [dispatch]);
  const finish = useCallback(() => dispatch("finish"), [dispatch]);

  return { visible, start, finish };
}

/**
 * The mark telling its own story: three pieces merged as one rounded
 * rectangle, splitting and sliding out to their resting positions.
 *
 * No timing lives here — render it only when you already know you are waiting,
 * or let `DelayedLoader` decide for you.
 */
export function BrandLoader({
  label = "Loading",
  size = 56,
  className = "",
}: {
  /** sr-only status text. */
  label?: string;
  /** px. */
  size?: number;
  className?: string;
}) {
  // One instance per screen makes a collision unlikely, but the editor can
  // mount a loader while the navbar mark is on screen — so the gradient id is
  // per-instance. React's id contains punctuation that CSS `url()` dislikes,
  // hence the strip.
  const gradientId = `cc-loader-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;

  return (
    <div
      role="status"
      aria-live="polite"
      className={`relative inline-flex items-center justify-center ${className}`.trimEnd()}
      style={{ "--cc-loader-fill": `url(#${gradientId})` } as CSSProperties}
    >
      {/* Paint only. userSpaceOnUse so one gradient spans the whole 24×24
          mark instead of restarting inside each of the three pieces. */}
      <svg
        width="0"
        height="0"
        aria-hidden
        focusable="false"
        className="absolute h-0 w-0 overflow-hidden"
      >
        <defs>
          <linearGradient
            id={gradientId}
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1="0"
            x2="24"
            y2="24"
          >
            <stop offset="0%" stopColor="#a78bfa" />
            <stop offset="55%" stopColor="#c084fc" />
            <stop offset="100%" stopColor="#e879f9" />
          </linearGradient>
        </defs>
      </svg>

      <span
        className="cc-loader-mark block"
        style={{ width: size, height: size }}
      >
        <Mark className="h-full w-full" />
      </span>

      <span className="sr-only">{label}</span>
    </div>
  );
}

/**
 * The timing wrapper. Mount it when a wait begins — as a Suspense fallback
 * (`app/**\/loading.tsx`) that is its own mount.
 *
 * Renders nothing for the first `delayMs`, because most waits in a
 * prerendered export finish inside it and the correct experience for an
 * instant wait is instant. `minVisibleMs` is the floor the shared machine
 * holds once something has been painted: in a Suspense-fallback position the
 * "load finished" signal is this component being unmounted by React, which no
 * child can outlive, so the floor is enforced for callers that can signal
 * completion themselves — `RouteProgress` is one.
 */
export function DelayedLoader({
  delayMs = DEFAULT_TIMING.delayMs,
  minVisibleMs = DEFAULT_TIMING.minVisibleMs,
  children,
}: {
  delayMs?: number;
  minVisibleMs?: number;
  children?: ReactNode;
}) {
  const { visible, start, finish } = useDelayedVisibility(delayMs, minVisibleMs);

  useEffect(() => {
    // Mounting is the wait beginning; unmounting is it ending.
    start();
    return finish;
  }, [start, finish]);

  if (!visible) return null;
  return <>{children ?? <BrandLoader />}</>;
}
