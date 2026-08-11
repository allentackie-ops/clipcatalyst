# Brand kit — build spec

The pricing page promises a brand kit on Starter and above. This makes it
real: a creator's own logo on the clip instead of ours, and their own colour
on the caption highlight, in both renderers.

## What a brand kit is

```ts
type BrandKit = {
  /** Logo drawn bottom-right in place of the ClipCatalyst mark. */
  logo: { dataUrl: string; width: number; height: number } | null;
  /** Caption highlight colour, "#rrggbb". null = the default violet. */
  captionColor: string | null;
  /** Draw the logo at all. Off = a clean corner (paid plans only). */
  showLogo: boolean;
};
```

Nothing else. No fonts (the renderers resolve one family and a webfont would
have to be embedded in the ASS build too), no caption templates, no secondary
colours. Those are separate features and pretending otherwise is how the
pricing page got into trouble in the first place.

## The watermark rule (one rule, both engines)

In precedence order:

1. `watermark_required` on the plan (free / anonymous) → **our** mark, exactly
   as today. A free user's brand kit is stored and previewed but never
   rendered — that is the upsell.
2. Otherwise a brand kit with `showLogo` and a logo → **their** logo.
3. Otherwise → nothing in the corner.

The caption colour is NOT gated the same way: it applies whenever a brand kit
has one and the plan is not `watermark_required`. Same gate, different asset.

**Diarization wins over brand colour.** When a clip has two or more speakers,
`SPEAKER_COLORS` keeps colouring the active word — that colour carries
information the brand colour would destroy. The brand colour replaces the
default violet for unassigned words and single-speaker clips only. Both
engines must agree on this, and it needs a test in each.

## Module 1 — `lib/studio/brandkit.ts` (shared core, node-tested)

Pure and storage-agnostic except for one clearly-marked IO section.

```ts
export const MAX_LOGO_BYTES = 2_000_000;
export const LOGO_TYPES = ["image/png", "image/jpeg", "image/webp", "image/svg+xml"];
export const DEFAULT_CAPTION_COLOR = "#a78bfa";   // == SPEAKER_COLORS[0]
export const LOGO_HEIGHT_RATIO = 0.045;           // of output height
export const LOGO_MAX_WIDTH_RATIO = 0.32;         // of output width
export const LOGO_MARGIN_RATIO = 0.02;

export const EMPTY_KIT: BrandKit;
export function isEmptyKit(kit: BrandKit): boolean;
/** "#abc" → "#aabbcc"; rejects anything else. Returns null when invalid. */
export function normalizeHex(input: string): string | null;
/** Contrast ratio vs the caption's black box, so we can warn on unreadable picks. */
export function contrastOnBlack(hex: string): number;
/** Box the logo fits into for a given output size, aspect preserved. */
export function logoBox(natural: {width: number; height: number}, out: {width: number; height: number}):
  { x: number; y: number; width: number; height: number };
/** Which colour the active word takes — the ONE place both engines agree. */
export function activeWordColor(speaker: number | undefined, speakerCount: number, kit: BrandKit): string;
export function validateLogoFile(file: { type: string; size: number }): string | null; // error message or null
```

`logoBox` is the geometry everything else trusts: height = `out.height *
LOGO_HEIGHT_RATIO`, width from the natural aspect, clamped to
`out.width * LOGO_MAX_WIDTH_RATIO` (re-deriving height so aspect holds), placed
bottom-right inside `LOGO_MARGIN_RATIO * out.height`. Round to whole pixels.

Storage (browser, local-first — the Studio works with no account):
```ts
export async function loadKit(): Promise<BrandKit>;   // never throws; EMPTY_KIT on any failure
export async function saveKit(kit: BrandKit): Promise<void>;
export async function clearKit(): Promise<void>;
```
Logos go in IndexedDB (a 2 MB data URL will not fit comfortably in
localStorage); colours alongside them in the same record. One store, one key.
Any IndexedDB failure degrades to in-memory for the session — a brand kit must
never be the reason a render fails.

Tests (`scripts/brandkit.test.mjs`, npm `test:brandkit`, same tsc→node pattern
as `test:edits`): hex normalization incl. rejects, contrast maths, `logoBox`
for wide/tall/square logos at 960/1280/1920 with the width clamp exercised,
`activeWordColor` across the full matrix (no kit / kit / 1 speaker / 3 speakers
/ unassigned word), and `validateLogoFile` for each reject reason. Every
tunable must fail a test if reverted.

## Module 2 — browser renderer (`lib/studio/render.ts`)

`RenderClipOptions` gains `brandKit?: BrandKit`.

- Decode the logo ONCE before the recording loop (`new Image()` + `decode()`,
  raced with a 3 s timeout). A logo that fails to decode is dropped and the
  render continues under the watermark rule as if there were no logo — log
  nothing to the user; this is not their problem mid-render.
- In `drawOverlays`, replace the text watermark per the rule above:
  `options.watermark` → today's text exactly; else logo → `drawImage` into
  `logoBox(...)` at `globalAlpha 0.9`; else nothing.
- Caption highlight uses `activeWordColor(word.speaker, speakerCount, kit)`.
  `speakerCount` comes from the distinct speakers in the clip's words.
- **Unedited, unbranded renders must stay byte-identical to today.**

## Module 3 — cloud (`api/`)

### 3a. Storage + entitlement
- `users` gains `brand_logo_path TEXT DEFAULT ''`, `brand_caption_color TEXT
  DEFAULT ''`, `brand_show_logo INTEGER DEFAULT 1` (guarded ALTERs, same
  pattern as the accounts work).
- `plans.py`: `Plan` gains `brand_kit: bool` — false for free, true for
  starter/pro/enterprise. Do NOT infer it from `watermark_required`; they are
  separate promises even if they line up today.
- `PUT /v1/me/brand` (session): multipart or JSON — logo bytes ≤ 2 MB with a
  sniffed content type (do not trust the header), colour validated server-side
  with the same rule as the TS. Stores under `settings.data_dir/brand/{user_id}.{ext}`.
  `DELETE /v1/me/brand` clears it. `GET /v1/me` returns the kit (logo as a
  URL, not bytes). 403 with an honest message when the plan lacks `brand_kit`.
- Path safety: the stored filename derives from the user id and a whitelisted
  extension — never from the upload's filename.

### 3b. Renderer (`pipeline/render.py`, `pipeline/captions.py`)
- `RenderOptions` gains `logo_path: str | None` and `caption_color: str | None`.
- `captions.py`: the ASS `Watermark` dialogue line is written only when our
  mark applies. `build_ass` takes the caption colour and uses it in place of
  `ACTIVE_COLOR` for unassigned words when there are fewer than 2 speakers —
  mirroring `activeWordColor` exactly.
- `render.py`: when a logo applies, add it as a second input and overlay it
  AFTER the scale, so the margin is in output pixels:
  `[1:v]scale=W:H[logo];[0:v]crop…,scale…,subtitles=…[base];[base][logo]overlay=X:Y`
  with W/H/X/Y computed from the same ratios as `logoBox` (port it; add a
  cross-check test against the TS via node subprocess, like croptrack/diarize).
  Escape the logo path exactly like the subtitles path. An unreadable or
  corrupt logo must not fail the render — probe it first and fall back to no
  overlay.
- `worker.py`: resolve the owner's kit at render time from the LIVE plan (same
  place `_watermark_for` and `_height_for` already re-derive), so a downgrade
  takes the brand kit away on the next render rather than at next login.

### 3c. Tests
`api/tests/test_brandkit.py`: entitlement 403 for free, accepted for starter;
oversize and wrong-type uploads rejected; a filename traversal attempt cannot
escape the brand dir; the ASS colour lands for a single-speaker clip and does
NOT override a 2-speaker clip; a real render with a real PNG logo produces a
file and the filtergraph contains one overlay; a corrupt logo still renders;
`logoBox` parity with the TS.

## Module 4 — UI (`components/studio/BrandKitPanel.tsx`)

One component, used in two places: a collapsible panel in the Studio idle view
(works with no account, local-first) and a card on `/account` when signed in.

- Drop or pick a logo, live preview on a 9:16 mock at real proportions.
- Colour: a small swatch row of sensible presets plus a hex field; live caption
  preview showing an active word in the chosen colour. Warn (do not block) when
  `contrastOnBlack` is below 3.
- "Remove logo" and "Reset to default" are always available.
- Free/anonymous: the panel works and previews, with one honest line saying
  the mark stays on free clips — and a link to pricing. This is the upsell and
  it must not be a lie: preview must show OUR mark for a free plan.
- Signed in on a paid plan: saving also PUTs to the server so cloud renders
  match. Failure to sync is surfaced inline; the local kit still applies.
- `useStudioPipeline` threads the kit into `renderClip`, and `ResultsView`
  passes it on re-render so an edited clip keeps the branding.

## Non-negotiables

- A brand kit must never fail a render. Every failure path degrades to the
  current behaviour.
- Free tier renders are unchanged, byte for byte.
- Diarization colours win over the brand colour whenever 2+ speakers exist.
- Nothing about fonts, templates, or multiple logos — this spec is the whole
  feature.
