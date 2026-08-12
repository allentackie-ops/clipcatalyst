# Branded loading — build spec

Every wait in the app is currently unbranded: a blank frame, then content.
This gives the waits a mark of their own.

## The idea

The Splice mark already tells a story — **one wide video cut into three tall
clips, sliding apart**. The loader animates exactly that: three pieces start
merged as a single rounded rectangle, split, and slide out into the mark's
resting positions. It loops. The animation is the logo explaining itself,
which is the only kind of brand animation worth building.

Nothing else spins. No generic spinner anywhere in the app.

## The rule that keeps it from making the site feel slower

A loader shown for 80 ms is a flicker, and a loader shown when nothing is
loading is theatre that costs real time. So:

- **Delay 180 ms before showing.** Most navigations in a static export finish
  inside that, and the user sees no loader at all — which is correct.
- **Once shown, stay 420 ms minimum.** Prevents a flash-and-vanish.
- **Never add an artificial splash to the first paint.** The pages are
  prerendered: the HTML is already there on a hard refresh, so a splash would
  delay content that had arrived. We brand the waits that exist; we do not
  invent one. If a wait is genuinely instant, the right experience is instant.

`components/BrandLoader.tsx` owns this timing so no caller can get it wrong.

## Components

### `components/BrandLoader.tsx`
```tsx
export function BrandLoader(props: {
  label?: string;          // sr-only status text, default "Loading"
  size?: number;           // px, default 56
  className?: string;
}): JSX.Element            // the animation itself, no timing
export function DelayedLoader(props: {  // timing wrapper (client component)
  delayMs?: number;        // default 180
  minVisibleMs?: number;   // default 420
  children?: ReactNode;    // defaults to <BrandLoader/>
}): JSX.Element | null
```

- The mark is the real `Mark` geometry from `components/ui.tsx` — do NOT
  redraw the paths. Animate the three `<path>` elements individually via CSS
  custom properties on a wrapping group, so the shapes stay byte-identical to
  the logo.
- Loop: 1.6 s. Pieces ease from merged (x offsets 0, and the two outer pieces
  aligned to the middle's vertical position) out to their resting transform,
  staggered 90 ms apart, hold, then ease back. `cubic-bezier(.65,0,.35,1)`.
- Colour: the brand gradient on dark. One instance per screen, so a gradient
  `id` collision is unlikely, but generate it with `useId()` anyway since the
  editor may mount one while the navbar mark is on screen.
- **`prefers-reduced-motion`: no movement at all.** The pieces render in their
  resting positions and the whole mark breathes opacity 0.55 → 1 over 1.8 s.
- Accessibility: wrapper is `role="status"` with `aria-live="polite"` and an
  `sr-only` label. The SVG itself is `aria-hidden`.

### `components/RouteProgress.tsx`
A 2 px gradient bar pinned to the top of the viewport during client-side
navigation. Uses `useLinkStatus` if available in the installed Next version;
otherwise falls back to a `useEffect` on `usePathname()` that shows the bar on
change and clears it on the next paint. It must never be a permanent element —
render `null` when idle. Same 180 ms delay rule.

## Where it goes

- `app/loading.tsx` — full-viewport centred loader, `min-h-dvh`, ink ground.
- `app/studio/loading.tsx` and `app/demo/loading.tsx` — these two carry the
  heaviest client bundles, so they are where a real wait actually happens on a
  phone. Same loader, plus one line of context ("Opening Studio…" /
  "Loading the tour…") because a labelled wait feels shorter than a bare one.
- `app/account/loading.tsx` — same, "Opening your account…".
- `RouteProgress` mounts once in `app/layout.tsx`.
- **In-app waits that already have their own progress UI keep it** — the
  Studio stage checklist, the render progress bar, the cloud poll. Do not
  replace an honest per-stage progress display with a logo animation; that
  would be less informative, not more branded.

## Keyframes

Live in `app/globals.css` beside the existing `marquee` / `pulse-soft` /
`rise` definitions, registered the same way through `@theme`. Names:
`--animate-splice` and `--animate-breathe`.

## Tests

`scripts/loader.test.mjs` + npm `test:loader`, compiling the pure timing
helper (extract the delay/min-visible state machine into a pure function in
`lib/loader.ts` so it can be tested off the DOM): shows nothing before the
delay; shows after it; once shown stays for the minimum even if the load
finished at 200 ms; a load finishing at 100 ms shows nothing at all; repeated
starts do not stack timers.

## Non-negotiables

- No loader may delay content that is already available.
- Reduced motion means no motion, not less motion.
- The mark's geometry is imported, never redrawn — a divergent copy would rot.
- Every existing suite stays green.
