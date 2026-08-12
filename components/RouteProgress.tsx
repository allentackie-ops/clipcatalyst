"use client";

import { useEffect, useRef } from "react";
import { useLinkStatus } from "next/link";
import { usePathname } from "next/navigation";
import { useDelayedVisibility } from "@/components/BrandLoader";

/**
 * A 2 px gradient hairline pinned to the top of the viewport while a
 * client-side navigation is in flight.
 *
 * It is never a permanent element: idle renders `null`, so there is no
 * always-on bar sitting at 0 % pretending to be progress. And it obeys the
 * same 180 ms delay as every other branded wait — a navigation that lands
 * inside that window paints nothing at all, which is the honest result for a
 * prerendered export where most of them do.
 *
 * Two signals feed the one machine:
 *
 *  1. `useLinkStatus` (next/link, Next ≥ 15.3 — installed here). This is the
 *     true start-of-navigation signal, but it reads a context that Next's
 *     `<Link>` provides around its own anchor, so it reports `pending: false`
 *     from the layout where this component is mounted. It costs nothing, it
 *     is the documented API, and it lights the bar up for free if this ever
 *     renders inside a `<Link>` subtree.
 *  2. `usePathname`. From the layout this is the only navigation signal in
 *     reach: show on change, clear on the next paint.
 */
export default function RouteProgress() {
  const { pending } = useLinkStatus();
  const pathname = usePathname();
  const { visible, start, finish } = useDelayedVisibility();

  useEffect(() => {
    if (pending) start();
    else finish();
  }, [pending, start, finish]);

  const settled = useRef(false);
  useEffect(() => {
    // Never on the first paint. The page is prerendered — the HTML is already
    // there — so a bar here would be an invented wait, not a branded one.
    if (!settled.current) {
      settled.current = true;
      return;
    }
    start();
    const frame = requestAnimationFrame(() => finish());
    return () => cancelAnimationFrame(frame);
  }, [pathname, start, finish]);

  if (!visible) return null;

  return (
    <div
      aria-hidden
      className="cc-route-progress pointer-events-none fixed inset-x-0 top-0 z-[200] h-[2px] animate-breathe bg-gradient-to-r from-brand-500 via-brand-400 to-spark-400"
    />
  );
}
