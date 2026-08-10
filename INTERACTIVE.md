# Interactive layer — build spec

The blueprint promises chat-based editing ("Make clip 2 more energetic",
"Remove the awkward pause in clip 4", "Add a zoom on the host's face when he
laughs"), a built-in editor for fine-tuning, and one-click publish. This spec
adds all three to Studio.

## Scope decisions (binding)

- **Device engine gets the full interactive layer.** The source file, the
  transcript, and the crop tracks all live in the browser after a device run,
  so re-rendering an edited clip is free. This is where the working product
  is.
- **Cloud clips get Share only.** `worker.py` deletes the uploaded source the
  moment a job goes terminal, so a cloud clip *cannot* be re-rendered today.
  Cloud edit parity is **phase 2** and requires an explicit retention change
  (keep sources until the 48 h TTL) plus an edit endpoint + ffmpeg
  segment/zoom rendering. Do NOT half-build it here.
- **"One-click publish" is the Web Share API**, honestly framed. Direct
  TikTok/YouTube/Instagram API publishing needs registered apps + platform
  approvals we don't have. `navigator.share({ files })` on a phone opens the
  native share sheet with those apps as targets — that IS one-tap publish,
  and it works today. Desktop falls back to download guidance. No fake
  "Connect TikTok" buttons.
- **No LLM calls.** The chat is a deterministic intent parser over a closed
  command set, same discipline as the virality engine. It must be honest
  when it doesn't understand ("I didn't catch that — try 'remove the pause
  at 0:14'"), never pretend.

## Architecture — the frozen-core pattern again

```
chat text ──▶ parseIntent (lib/studio/intent.ts) ──▶ EditCommand[]
                                                        │
quick-action chips ────────────────────────────────────▶│
                                                        ▼
                              applyCommand (lib/studio/edits.ts)  ← pure math
                                                        │  ClipEdits state
                                                        ▼
                              resolveEdits → keeps/zooms/captions
                                                        │
                                       ┌────────────────┴───────────────┐
                                       ▼                                ▼
                              renderClip(edits)                 phase 2: ffmpeg
                              (browser canvas)                  (cloud, later)
```

`lib/studio/edits.ts` and `lib/studio/intent.ts` are the shared judgement
core: pure, deterministic, node-tested (`npm run test:edits`,
`npm run test:intent`), mutation-verified. The renderer and UI only consume
their outputs.

## Module 1 — `lib/studio/edits.ts` (frozen core: edit math)

### Types (verbatim)

```ts
import type { TranscriptWord } from "./types";
import type { CropTrack } from "./croptrack";

/** All times below are CLIP-RELATIVE SECONDS anchored to the ORIGINAL
 *  plan.start (a stable anchor that never moves as edits accumulate). */
export type CutRange = { start: number; end: number };
export type ZoomEvent = { start: number; end: number; scale: number };

export type ClipEdits = {
  /** >0 trims into the clip; <0 extends earlier. */
  startDelta: number;
  /** >0 extends later; <0 trims the tail. */
  endDelta: number;
  /** Normalized: sorted, merged, non-overlapping, inside current bounds. */
  cuts: CutRange[];
  /** Sorted, non-overlapping. */
  zooms: ZoomEvent[];
  captions: boolean; // default true
  hookIndex: number; // chosen hook for display; default 0
};

export const EMPTY_EDITS: ClipEdits; // captions: true, hookIndex: 0, rest zero/empty
export function isEmptyEdits(edits: ClipEdits): boolean;

export type EditContext = {
  planStart: number;      // absolute source seconds (original)
  planEnd: number;
  sourceDuration: number; // decoded audio duration — extension hard bound
  /** FULL transcript words, ABSOLUTE times (pauses + re-windowing). */
  words: TranscriptWord[];
  hooks: string[];
};

export type EditCommand =
  | { type: "trim"; edge: "start" | "end"; seconds: number } // + = shorter
  | { type: "cutPause"; which: "longest" | "all" | { at: number } }
  | { type: "cut"; start: number; end: number }
  | { type: "tighten" }
  | { type: "energetic" }
  | { type: "zoom"; at: number; duration?: number; scale?: number }
  | { type: "clearZooms" }
  | { type: "clearCuts" }
  | { type: "captions"; on: boolean }
  | { type: "useHook"; index: number }
  | { type: "cycleHook" }
  | { type: "reset" };

export type Pause = { start: number; end: number; length: number }; // display time

export type ResolvedEdits = {
  start: number; // absolute adjusted start
  end: number;   // absolute adjusted end
  /** Absolute source-time segments to keep. ≥1 entries, each ≥ 0.2 s. */
  keeps: { from: number; to: number }[];
  /** Zoom windows in seconds relative to `start` (renderer clip time). */
  zooms: ZoomEvent[];
  captions: boolean;
  outputDuration: number; // Σ (to − from), round3
};
```

### Time-frame convention (the one subtle thing — get it right)

Three frames exist. (1) **absolute** source seconds; (2) **original clip
time** = absolute − planStart, the frame `cuts`/`zooms` are STORED in;
(3) **display time** = what the user sees on the current clip, = original
clip time − startDelta. `applyCommand` accepts user-facing times
(`cutPause.at`, `cut`, `zoom.at`) in **display time** and converts by adding
`edits.startDelta` internally, so neither the parser nor the UI ever
converts. `findPauses` returns **display time**. `resolveEdits` emits
absolute + renderer-relative. Unit tests must pin this: trim 2 s off the
start, then `zoom at 5` must land at original-clip-time 7.

### Functions

- `applyCommand(edits, command, ctx) -> { edits: ClipEdits; summary: string }`
  Never throws. Clamps everything; `summary` is one truthful human sentence
  used verbatim as the chat reply ("Cut the 1.2 s pause at 0:14.",
  "Start moved 2.0 s later — the clip is now 26.3 s.",
  "That would leave less than 3 s of clip, so I left it alone."). No-ops
  return a truthful summary too. Normalize after every command (merge
  overlapping cuts; drop zooms that fall entirely inside cuts or outside
  bounds; re-sort).
- `findPauses(edits, ctx) -> Pause[]` — inter-word gaps ≥ `PAUSE_MIN_S`
  strictly inside the CURRENT bounds (never the lead-in before the first
  word or the tail), display time, excluding gaps already fully cut.
- `resolveEdits(edits, ctx) -> ResolvedEdits` — adjusted bounds; keeps =
  bounds minus cuts (cut fragments < 0.2 s merge into the cut); zooms
  re-based to `start` and clipped to bounds.
- `outputTimeAt(resolved, absoluteT) -> number` — output seconds elapsed at
  an absolute source time (drives the renderer's progress + progress bar).
- `rewindowWords(words, start, end) -> TranscriptWord[]` — mirror of
  highlights.ts `windowWords` (re-base to start, round3, carry `speaker`).
  Duplicated here on purpose — highlights.ts stays untouched.
- `retimeTrack(track, startDelta) -> CropTrack` — shift keyframe times by
  −startDelta (round3) so a track built for the original bounds replays
  correctly on shifted bounds. `cropCenterAt` holds flat outside the
  keyframe range, which covers extension. Pure; do NOT touch croptrack.ts.
- `describeEdits(edits, ctx) -> string[]` — short chips for the UI, e.g.
  `["Start +2.0s", "2 pauses cut", "Zoom at 0:08", "Captions off"]`.
  Empty array when `isEmptyEdits`.

### Tunables (all mutation-tested — a reverted constant must fail a test)

```
MIN_CLIP_S = 3.0        // a command may never leave less clip than this
PAUSE_MIN_S = 0.45      // inter-word gap that counts as a pause
TIGHTEN_MIN_PAUSE_S = 0.5
CUT_PAD_S = 0.12        // breathing room kept on each side of a cut pause
MIN_CUT_S = 0.2         // cuts shorter than this are dropped
ZOOM_DEFAULT_SCALE = 1.28
ZOOM_MIN_SCALE = 1.1  ZOOM_MAX_SCALE = 1.6
ZOOM_DEFAULT_DURATION_S = 1.6
ZOOM_RAMP_S = 0.25      // renderer ease at each zoom edge (exported here)
MAX_ZOOMS = 6
```

Command semantics that need pinning:
- `trim`: clamps to `MIN_CLIP_S` remaining and to `[0, sourceDuration]`
  absolute. Negative seconds = extend (bounded by the source).
- `cutPause "longest"`: the longest current pause; the cut is
  `[gap.start + CUT_PAD_S, gap.end − CUT_PAD_S]`, dropped if < MIN_CUT_S.
  `"all"`: every current pause. `{at}`: the pause containing (or nearest
  within 1.0 s of) `at`; nothing near → truthful no-op summary.
- `tighten`: cutPause-all but with threshold `TIGHTEN_MIN_PAUSE_S`.
- `energetic`: tighten + up to 2 zooms (`scale 1.22`, `1.4 s`) at the start
  of the first word at/after ⅓ and ⅔ of the post-tighten output duration;
  skip a placement within 2 s of an existing zoom. Summary names both parts.
- `zoom`: at display-time `at`, `duration` centered forward (`[at,
  at+duration]`), clipped to bounds; scale clamped. A new zoom overlapping
  an old one REPLACES it. > MAX_ZOOMS → truthful refusal.
- `cut`: explicit range; refuse (summary) if it would leave < MIN_CLIP_S.
- `useHook`: clamp to hooks.length−1.
- `reset`: EMPTY_EDITS.

## Module 2 — `lib/studio/intent.ts` (frozen core: the chat brain)

```ts
export type IntentContext = {
  clipCount: number;
  activeClip: number; // 0-based
  duration: number;   // active clip's CURRENT output duration
  pauses: Pause[];    // active clip's current pauses (display time)
  hooks: string[];
  /** Active clip's current words, display time (for "when he says X"). */
  words: { text: string; start: number }[];
};

export type Intent =
  | { kind: "commands"; commands: EditCommand[]; targetClip: number; note?: string }
  | { kind: "undo" }
  | { kind: "help"; reply: string }
  | { kind: "unknown"; reply: string };

export function parseIntent(input: string, ctx: IntentContext): Intent;
```

Deterministic, case/punctuation-tolerant English. Families (each tested with
several phrasings, including exactly the three blueprint sentences):

1. **Targeting**: "clip 2" / "clip #2" / "the second clip" / "the last
   clip" → targetClip (0-based). Default: activeClip. Out of range →
   unknown with the valid range in the reply.
2. **Pauses**: "remove the awkward pause (in clip 4)", "cut the pause",
   "remove the pause at 0:14", "remove all the pauses", "cut the dead
   air", "remove the silences", "tighten it up" → cutPause / tighten.
   No pauses currently → still emit the command (applyCommand answers
   truthfully); parser stays dumb about state beyond phrasing.
3. **Energy**: "make it more energetic", "make clip 2 more energetic",
   "punchier", "more punch", "give it more energy", "faster paced" →
   energetic.
4. **Trim**: "trim the first 2 seconds", "cut the first second", "start
   2 seconds later", "start half a second earlier", "end 1.5 seconds
   earlier", "extend the end by 2 seconds", "make it 2 seconds shorter"
   (→ trim end), "make it longer" (→ extend end by 2, note says so).
5. **Zoom**: "add a zoom at 0:08", "zoom in at 12 seconds", "punch in
   when he says funnel" (word search over ctx.words, first
   case/punct-insensitive match → zoom at that word's start), "add a zoom
   on the host's face when he laughs" → word search for laugh/laughs/
   laughing; NOT found → unknown with honest guidance ("I can't detect
   laughter on screen yet — give me a moment, like 'zoom at 0:12'").
   "remove the zoom(s)" → clearZooms.
6. **Captions**: "turn off captions", "captions off", "hide the
   subtitles", "captions on", "bring back captions".
7. **Hooks**: "use hook b" / "use the second hook" → useHook 1; "try
   another hook" → useHook (activeHookIndex+1 mod hooks.length) — parser
   gets hookIndex via ctx? NO: emit `{type:"useHook", index:-1}`?
   Keep it simple: "another/different hook" → note in Intent asking UI to
   cycle is over-clever. Instead: `useHook` with explicit index only;
   "try another hook" → useHook with index = -1 is forbidden. Resolution:
   add `{ type: "cycleHook" }` to EditCommand in edits.ts; applyCommand
   advances `(hookIndex+1) % hooks.length`. Parser emits cycleHook.
8. **Undo/reset**: "undo (that)" → {kind:"undo"}; "reset (the edits)" /
   "start over" → reset command.
9. **Help**: "help", "what can you do" → help reply listing real examples.

Numbers: "0:12", "12s", "12 sec(onds)", "at 12", bare floats; "a second"
= 1, "half a second" = 0.5, "one/two/three/four/five seconds" as words.
Unknown input NEVER guesses: reply with 2–3 concrete example commands.

## Module 3 — `lib/studio/render.ts` (cuts, zooms, captions toggle)

```ts
export type RenderEdits = {
  keeps: { from: number; to: number }[]; // absolute, sorted; keeps[0].from == plan.start
  zooms: ZoomEvent[];                    // relative to plan.start
  captions: boolean;
  outputDuration: number;
};
export type RenderClipOptions = RenderOptions & {
  track?: CropTrack;
  edits?: RenderEdits;  // absent → exactly today's behaviour, byte-identical
};
```

The caller passes an already-EDITED plan (start/end = resolved bounds, words
re-windowed to that start), so all existing plan-based math keeps working.
Changes inside `renderClip`:

- **Segment skipping.** Track `segIndex` starting at 0. Whenever
  `video.currentTime ≥ keeps[segIndex].to − 0.03` and a later segment
  exists: set a `skipping` guard (re-entry from rAF/timeupdate/interval must
  be impossible), `recorder.pause()` if recording, `video.pause()`, seek to
  `keeps[segIndex+1].from`, await `seeked` (2 s timeout — on timeout just
  continue), `segIndex++`, `recorder.resume()`, `video.play()`, clear guard.
  If `recorder.pause` throws, degrade: skip without pausing (a one-frame
  glitch beats a dead render). End condition stays `currentTime ≥ endTarget
  || ended` where endTarget = last keep's `to` (== plan.end).
- **Watchdog**: budget on SOURCE span + cuts: `(plan.end − plan.start) *
  3000 + 30_000 + keeps.length * 2000`.
- **Progress + progress bar** use output time: precompute cumulative keep
  durations; `outT(t)`; report `outT/outputDuration`; the burned-in progress
  bar width uses the same fraction (a clip with cuts must show a bar that
  reaches exactly 1 at the end — no stalls, no jumps back).
- **Zoom** in `drawVideoFrame`: `z = zoomAt(zooms, clipT)` — 1 outside all
  events; inside, ramp linearly from 1 to `scale` over `ZOOM_RAMP_S` at each
  edge (events shorter than 2×ramp: peak at midpoint). Apply: `sw2 = sw/z`,
  `sh2 = sh/z`; horizontal center = the SAME cx the crop would use (track or
  centered) so zoom punches toward the subject; `sx = clamp(cx*vw − sw2/2,
  0, vw−sw2)`, `sy = (vh − sh2)/2`. z == 1 must take the EXACT existing code
  path (no float drift on unedited renders).
- **Captions**: `edits && !edits.captions` → skip `drawCaptions` entirely
  (watermark + progress bar stay).

## Module 4 — UI

### Pipeline state (`useStudioPipeline.ts`)

`done` becomes:

```ts
{ status: "done";
  clips: StudioClip[];            // FinishedClip & { track?: CropTrack }
  sourceUrl: string;
  sourceDuration: number;         // from decodeToMono16k
  transcript: Transcript;         // full, absolute times, post-diarize
  renderOptions: { height: 960|1280|1920; watermark: boolean };
  failedCount: number;
}
```

Push `tracks[i]` onto each finished clip. No other pipeline changes.

### `components/studio/ResultsView.tsx` (new — device done view)

Extracted from StudioApp's device-done branch (StudioApp keeps the cloud
markup inline). Owns per-clip edit sessions:

```ts
type EditSession = {
  edits: ClipEdits;
  history: ClipEdits[];           // undo stack (cap 20)
  chat: { role: "user" | "catalyst"; text: string }[];
  rendered: { url: string; blob: Blob; extension: string; mimeType: string } | null;
  rendering: number | null;       // 0..1 progress, null = not rendering
  dirty: boolean;                 // edits changed since last render
};
```

- Grid of ClipCards exactly as today, plus per-card **Edit** button and
  **Share** button; an "Edited" badge on cards with applied (rendered)
  edits; card preview/download swap to the re-rendered file when present.
- **ClipEditor** (`components/studio/ClipEditor.tsx`): overlay dialog
  (`role="dialog"`, aria-modal, Esc + backdrop close, focus trapped to it,
  focus returns to the Edit button on close) for one clip:
  - Left: the clip preview (last rendered video) + a slim timeline strip
    visualizing current cuts (gaps) and zooms (violet blocks) — pure CSS
    divs from `resolveEdits`, no canvas.
  - Right: chat thread + input ("Try: remove the pause at 0:14"), quick
    chips (Tighten pauses · Punch-in zoom · Trim start 1s · Trim end 1s ·
    Captions on/off · Undo · Reset), edit-chip row from `describeEdits`,
    and the actions: **Re-render clip** (primary; disabled unless dirty;
    inline progress) · Download · Share.
  - Chat flow: user text → `parseIntent` → commands → `applyCommand` each →
    assistant bubble = joined summaries + (when dirty) "Hit Re-render to
    bake it in." Commands targeting ANOTHER clip: apply to that clip's
    session and say so ("Done — that went to clip 2."). undo pops history.
    unknown/help → reply verbatim.
  - Re-render: `resolveEdits` → edited plan (`rewindowWords`) →
    `renderClip` with `retimeTrack`ed track + RenderEdits + the run's
    renderOptions. Failure → assistant bubble with the error, previous
    render kept. Success → swap preview + card, revoke the old edited URL
    (never revoke the original clip URL).
- The three blueprint commands must work end-to-end in this editor.

### `components/studio/ShareButton.tsx` (both engines)

- Device clips: wrap the blob in a `File` (`clipcatalyst-N.mp4/webm`).
  Cloud clips: `fetch(url)` → blob first (button shows "Preparing…").
- `navigator.canShare?.({ files })` → `navigator.share` (button label
  **Share** — the honest one-click publish; AbortError = user closed the
  sheet, not an error). Unsupported → inline hint "Your browser can't open
  a share sheet — download and post from your phone." Never a fake publish.
- ClipCard gains `onEdit?: () => void`, `edited?: boolean`, and renders
  ShareButton next to Download for every clip (cloud passes no onEdit).

## Module 5 — tests (must run here)

- `scripts/edits.test.mjs` + npm `test:edits` (tsc edits.ts like the other
  suites): every command, every clamp, the display-time↔original-time
  conversion, keeps math with exact numbers, pause padding, zoom
  replace/clamp/ramp math, retimeTrack, rewindowWords parity with a copy of
  the highlights fixture, describeEdits. Adversarial: cut spanning the whole
  clip, trim past both bounds, pause at the exact boundary, zoom fully
  inside a cut (dropped on normalize), floating-point accumulation
  (round3 everywhere times are emitted).
- `scripts/intent.test.mjs` + npm `test:intent`: ≥ 40 phrasings across all
  families incl. the three blueprint sentences verbatim, number/time
  parsing table, targeting edge cases, unknown-input honesty (reply contains
  a usable example), help.
- Studio e2e (scratchpad/studio-e2e.mjs): open editor on clip 1 → chat
  "remove all the pauses" → assert an assistant reply appears and Re-render
  enables → click Re-render → wait → assert the edited preview's
  `video.duration` is measurably shorter than the original → chat "turn off
  captions" → assert dirty again. Screenshot the editor.
- All existing suites stay green: 172 Python, 28 croptrack, 20 diarize,
  20 speakerfeats, both builds, landing e2e.

## Non-negotiables

- An unedited clip renders BYTE-IDENTICALLY to today (no `edits` object →
  the old code path; z=1 must not touch the draw math).
- Edits degrade safely: a re-render failure keeps the previous good file;
  the editor can never lose the original clip.
- Chat never lies: every reply states what actually changed (or why
  nothing did). No fabricated capabilities, no silent no-ops.
- Share is real sharing or an honest fallback — no dead "publish" UI.
- highlights.ts, croptrack.ts, diarize.ts, speakerfeats.ts stay untouched.
