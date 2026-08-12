// Edit math for the interactive layer: every chat command and editor chip
// funnels into applyCommand, and the renderer consumes resolveEdits. Pure and
// deterministic — no DOM, no timers, no LLM. applyCommand NEVER throws: its
// summary sentence is used verbatim as the chat reply, so it must always come
// back truthful, including when the answer is "I refused, and here's why".
//
// Time frames — three exist, and mixing them up is the whole failure mode:
//   absolute  source-file seconds (ctx.words, resolved keeps).
//   original  absolute − ctx.planStart. cuts/zooms are STORED in this frame,
//             anchored to the ORIGINAL plan.start, so bounds can move without
//             re-basing every stored edit.
//   display   original − edits.startDelta — what the user currently sees.
// applyCommand takes user-facing times (cutPause.at, cut, zoom.at) in DISPLAY
// time and adds edits.startDelta itself; neither the intent parser nor the UI
// ever converts. findPauses answers in display time. resolveEdits emits
// absolute keeps plus renderer-relative (start-based) zooms.
//
// Only `import type` below — the compiled JS must have zero runtime imports so
// the node test harness can load .croptrack-build/edits.js directly.

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

export type EditContext = {
  planStart: number; // absolute source seconds (original)
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
  end: number; // absolute adjusted end
  /** Absolute source-time segments to keep. ≥1 entries, each ≥ 0.2 s. */
  keeps: { from: number; to: number }[];
  /** Zoom windows in seconds relative to `start` (renderer clip time). */
  zooms: ZoomEvent[];
  captions: boolean;
  outputDuration: number; // Σ (to − from), round3
};

// --- Tunables. Every one of these is pinned by scripts/edits.test.mjs. ------

/** A command may never leave less clip than this. */
export const MIN_CLIP_S = 3.0;
/** Inter-word gap that counts as a pause. */
export const PAUSE_MIN_S = 0.45;
/** "Tighten" only takes pauses this long — it trims flab, not breathing. */
export const TIGHTEN_MIN_PAUSE_S = 0.5;
/** Breathing room kept on each side of a cut pause — cutting flush against a
 *  word edge clips the consonant and sounds broken. */
export const CUT_PAD_S = 0.12;
/** Cuts shorter than this are dropped; keep fragments shorter than this merge
 *  into the neighbouring cut (a 5-frame sliver reads as a glitch, not video). */
export const MIN_CUT_S = 0.2;
export const ZOOM_DEFAULT_SCALE = 1.28;
export const ZOOM_MIN_SCALE = 1.1;
export const ZOOM_MAX_SCALE = 1.6;
export const ZOOM_DEFAULT_DURATION_S = 1.6;
/** Renderer ease at each zoom edge. Exported here so render.ts shares it. */
export const ZOOM_RAMP_S = 0.25;
export const MAX_ZOOMS = 6;
/** cutPause {at}: how far a pause may sit from the named time and still count. */
const PAUSE_NEAR_S = 1.0;
/** energetic places softer, shorter zooms than a manual "zoom" command. */
const ENERGETIC_ZOOM_SCALE = 1.22;
const ENERGETIC_ZOOM_S = 1.4;
/** energetic never stacks a zoom this close to an existing one. */
const ENERGETIC_ZOOM_GAP_S = 2.0;

export const EMPTY_EDITS: ClipEdits = Object.freeze({
  startDelta: 0,
  endDelta: 0,
  cuts: [],
  zooms: [],
  captions: true,
  hookIndex: 0,
});

export function isEmptyEdits(edits: ClipEdits): boolean {
  return (
    edits.startDelta === 0 &&
    edits.endDelta === 0 &&
    edits.cuts.length === 0 &&
    edits.zooms.length === 0 &&
    edits.captions &&
    edits.hookIndex === 0
  );
}

// --- Small helpers ---------------------------------------------------------

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** "1.2", "24.3" — durations in summaries always carry one decimal. */
function fmt1(seconds: number): string {
  return (Math.round(seconds * 10) / 10).toFixed(1);
}

function formatClipTime(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function freshEmpty(): ClipEdits {
  return { startDelta: 0, endDelta: 0, cuts: [], zooms: [], captions: true, hookIndex: 0 };
}

type Bounds = { aStart: number; aEnd: number; oStart: number; oEnd: number };

function boundsOf(edits: ClipEdits, ctx: EditContext): Bounds {
  const oStart = edits.startDelta;
  const oEnd = ctx.planEnd - ctx.planStart + edits.endDelta;
  return { oStart, oEnd, aStart: ctx.planStart + oStart, aEnd: ctx.planStart + oEnd };
}

// --- Normalization ---------------------------------------------------------

/**
 * Restore the ClipEdits invariants after any command: deltas clamped to the
 * source, cuts clipped to the current bounds / sorted / merged / de-slivered,
 * zooms dropped when a cut swallowed them or the bounds moved off them.
 */
function normalizeEdits(edits: ClipEdits, ctx: EditContext): ClipEdits {
  const origLen = ctx.planEnd - ctx.planStart;
  const sdRaw = Number.isFinite(edits.startDelta) ? edits.startDelta : 0;
  const edRaw = Number.isFinite(edits.endDelta) ? edits.endDelta : 0;
  const sd = round3(clamp(sdRaw, -ctx.planStart, ctx.sourceDuration - ctx.planStart));
  const ed = round3(clamp(edRaw, -origLen, ctx.sourceDuration - ctx.planEnd));
  const oStart = sd;
  const oEnd = origLen + ed;

  const cuts: CutRange[] = [];
  const clipped = (edits.cuts ?? [])
    .filter((c) => Number.isFinite(c.start) && Number.isFinite(c.end))
    .map((c) => ({ start: round3(Math.max(c.start, oStart)), end: round3(Math.min(c.end, oEnd)) }))
    .filter((c) => c.end - c.start >= MIN_CUT_S - 1e-9)
    .sort((a, b) => a.start - b.start);
  for (const c of clipped) {
    const prev = cuts[cuts.length - 1];
    if (prev && c.start <= prev.end + 1e-9) prev.end = Math.max(prev.end, c.end);
    else cuts.push({ start: c.start, end: c.end });
  }

  const zooms = (edits.zooms ?? [])
    .filter((z) => Number.isFinite(z.start) && Number.isFinite(z.end) && Number.isFinite(z.scale))
    .map((z) => ({
      start: round3(z.start),
      end: round3(z.end),
      scale: round3(clamp(z.scale, ZOOM_MIN_SCALE, ZOOM_MAX_SCALE)),
    }))
    .filter((z) => z.end - z.start > 1e-3)
    // Entirely off the current bounds, or entirely inside a cut → pointless.
    .filter((z) => !(z.end <= oStart + 1e-9 || z.start >= oEnd - 1e-9))
    .filter((z) => !cuts.some((c) => c.start <= z.start + 1e-9 && c.end >= z.end - 1e-9))
    .sort((a, b) => a.start - b.start);

  const hookMax = Math.max(0, ctx.hooks.length - 1);
  const hookRaw = Number.isFinite(edits.hookIndex) ? Math.round(edits.hookIndex) : 0;
  return {
    startDelta: sd,
    endDelta: ed,
    cuts,
    zooms,
    captions: edits.captions !== false,
    hookIndex: clamp(hookRaw, 0, hookMax),
  };
}

// --- Resolution (bounds − cuts → keeps) ------------------------------------

/** Bounds minus cuts, absolute. May be empty for degenerate input — callers
 *  that need the renderer guarantee (≥1 keep) go through resolveEdits. */
function computeKeeps(
  e: ClipEdits,
  ctx: EditContext
): { keeps: { from: number; to: number }[]; output: number } {
  const start = round3(ctx.planStart + e.startDelta);
  const end = round3(ctx.planEnd + e.endDelta);
  const keeps: { from: number; to: number }[] = [];
  let cursor = start;
  for (const c of e.cuts) {
    const cs = round3(ctx.planStart + c.start);
    const ce = round3(ctx.planStart + c.end);
    if (ce <= cursor) continue;
    if (cs >= end) break;
    // A keep fragment shorter than MIN_CUT_S merges into the cut.
    if (Math.min(cs, end) - cursor >= MIN_CUT_S - 1e-9) {
      keeps.push({ from: round3(cursor), to: round3(Math.min(cs, end)) });
    }
    cursor = Math.max(cursor, ce);
  }
  if (end - cursor >= MIN_CUT_S - 1e-9) keeps.push({ from: round3(cursor), to: round3(end) });
  let output = 0;
  for (const k of keeps) output += k.to - k.from;
  return { keeps, output: round3(output) };
}

export function resolveEdits(edits: ClipEdits, ctx: EditContext): ResolvedEdits {
  const e = normalizeEdits(edits, ctx);
  const boundStart = round3(ctx.planStart + e.startDelta);
  const boundEnd = round3(ctx.planEnd + e.endDelta);
  const { keeps } = computeKeeps(e, ctx);
  // Renderer safety: never hand out zero keeps, whatever the caller stored.
  if (keeps.length === 0) keeps.push({ from: boundStart, to: boundEnd });
  // Fold the bounds onto the keeps: computeKeeps drops a cut that abuts a
  // bound (and absorbs sub-MIN_CUT_S keep slivers into a cut), so the trim
  // bounds can overhang the kept video. Reporting the overhang as start/end
  // would make the renderer record a supposedly-cut head or tail. Invariant:
  // keeps[0].from === start and the last keep's to === end, for EVERY input.
  const start = keeps[0].from;
  const end = keeps[keeps.length - 1].to;

  const len = end - start;
  // Zooms are stored in original clip time (absolute − planStart). Re-base
  // them against the FOLDED start — which differs from startDelta exactly
  // when a bound was folded — and re-clip them to the folded window.
  const offset = start - ctx.planStart;
  const zooms: ZoomEvent[] = [];
  for (const z of e.zooms) {
    const zs = Math.max(0, z.start - offset);
    const ze = Math.min(len, z.end - offset);
    if (ze - zs > 1e-3) zooms.push({ start: round3(zs), end: round3(ze), scale: z.scale });
  }

  let total = 0;
  for (const k of keeps) total += k.to - k.from;
  return { start, end, keeps, zooms, captions: e.captions, outputDuration: round3(total) };
}

/** Output seconds elapsed at an absolute source time (renderer progress). */
export function outputTimeAt(resolved: ResolvedEdits, absoluteT: number): number {
  let acc = 0;
  for (const k of resolved.keeps) {
    if (absoluteT >= k.to) {
      acc += k.to - k.from;
      continue;
    }
    if (absoluteT > k.from) acc += absoluteT - k.from;
    break;
  }
  return round3(Math.min(acc, resolved.outputDuration));
}

/** Absolute source time at an output time — inverse of outputTimeAt. */
function absoluteAtOutput(resolved: ResolvedEdits, outT: number): number {
  let remaining = Math.max(0, outT);
  for (const k of resolved.keeps) {
    const len = k.to - k.from;
    if (remaining <= len) return k.from + remaining;
    remaining -= len;
  }
  return resolved.end;
}

// --- Pauses ----------------------------------------------------------------

/**
 * Inter-word gaps ≥ minLen strictly inside the current bounds, display time.
 * A gap touching a bound is the lead-in or the tail, not a pause — trimming
 * owns those. A gap whose padded core is already inside a cut is spent.
 */
function pausesAbove(edits: ClipEdits, ctx: EditContext, minLen: number): Pause[] {
  const { aStart, aEnd } = boundsOf(edits, ctx);
  const out: Pause[] = [];
  const words = ctx.words;
  for (let i = 0; i + 1 < words.length; i++) {
    const gs = words[i].end;
    const ge = words[i + 1].start;
    if (!(ge - gs >= minLen)) continue;
    if (gs <= aStart + 1e-6 || ge >= aEnd - 1e-6) continue;
    // "Already cut" = an existing cut covers the padded core a pause cut
    // would produce; the actionable part of the gap is gone.
    const coreS = gs - ctx.planStart + CUT_PAD_S;
    const coreE = ge - ctx.planStart - CUT_PAD_S;
    if (edits.cuts.some((c) => c.start <= coreS + 1e-6 && c.end >= coreE - 1e-6)) continue;
    out.push({
      start: round3(gs - ctx.planStart - edits.startDelta),
      end: round3(ge - ctx.planStart - edits.startDelta),
      length: round3(ge - gs),
    });
  }
  return out;
}

export function findPauses(edits: ClipEdits, ctx: EditContext): Pause[] {
  return pausesAbove(normalizeEdits(edits, ctx), ctx, PAUSE_MIN_S);
}

// --- Command helpers -------------------------------------------------------

type Applied = { edits: ClipEdits; summary: string };

/** Latest absolute start that still leaves MIN_CLIP_S of output. */
function latestStartFor(keeps: { from: number; to: number }[], aStart: number): number {
  let acc = 0;
  for (let i = keeps.length - 1; i >= 0; i--) {
    const len = keeps[i].to - keeps[i].from;
    if (acc + len >= MIN_CLIP_S - 1e-9) return keeps[i].to - (MIN_CLIP_S - acc);
    acc += len;
  }
  return aStart; // already at (or under) the floor: no shortening allowed
}

/** Earliest absolute end that still leaves MIN_CLIP_S of output. */
function earliestEndFor(keeps: { from: number; to: number }[], aEnd: number): number {
  let acc = 0;
  for (const k of keeps) {
    const len = k.to - k.from;
    if (acc + len >= MIN_CLIP_S - 1e-9) return k.from + (MIN_CLIP_S - acc);
    acc += len;
  }
  return aEnd;
}

function applyTrim(base: ClipEdits, ctx: EditContext, edge: "start" | "end", seconds: number): Applied {
  if (!Number.isFinite(seconds)) {
    return { edits: base, summary: "That number didn't parse, so nothing changed." };
  }
  const { aStart, aEnd } = boundsOf(base, ctx);
  const { keeps, output: before } = computeKeeps(base, ctx);
  let next = base;
  let moved = 0;
  if (edge === "start") {
    const target = round3(clamp(aStart + seconds, 0, latestStartFor(keeps, aStart)));
    moved = round3(target - aStart);
    next = normalizeEdits({ ...base, startDelta: round3(base.startDelta + moved) }, ctx);
  } else {
    const target = round3(clamp(aEnd - seconds, earliestEndFor(keeps, aEnd), ctx.sourceDuration));
    moved = round3(target - aEnd);
    next = normalizeEdits({ ...base, endDelta: round3(base.endDelta + moved) }, ctx);
  }
  if (Math.abs(moved) < 5e-4) {
    if (seconds === 0) return { edits: base, summary: "A 0 s trim changes nothing." };
    if (seconds > 0) {
      return { edits: base, summary: `That would leave less than ${MIN_CLIP_S} s of clip, so I left it alone.` };
    }
    return {
      edits: base,
      summary:
        edge === "start"
          ? "The source starts there, so the clip can't begin any earlier."
          : "The source ends there, so the clip can't run any later.",
    };
  }
  // The clamps above ran on PRE-move keeps: a bound landing within MIN_CUT_S
  // of a cut edge makes computeKeeps drop the sliver, and the output can dip
  // under MIN_CLIP_S. No partial application — refuse the whole trim. (The
  // `after < before` guard keeps extensions legal on an already-short clip:
  // growing the output can never be the thing that broke the floor.)
  const after = computeKeeps(next, ctx).output;
  if (after < MIN_CLIP_S - 1e-6 && after < before) {
    return { edits: base, summary: `That would leave less than ${MIN_CLIP_S} s of clip, so I left it alone.` };
  }
  const out = fmt1(resolveEdits(next, ctx).outputDuration);
  const label = edge === "start" ? "Start" : "End";
  const dir = moved > 0 ? "later" : "earlier";
  return { edits: next, summary: `${label} moved ${fmt1(Math.abs(moved))} s ${dir}. The clip is now ${out} s.` };
}

type CutAttempt = { edits: ClipEdits; removed: number } | { refused: "floor" | "small" };

/** Add one cut (original clip time), guarding MIN_CUT_S and MIN_CLIP_S. */
function addCut(base: ClipEdits, ctx: EditContext, oS: number, oE: number): CutAttempt {
  const { oStart, oEnd } = boundsOf(base, ctx);
  const cs = round3(Math.max(oS, oStart));
  const ce = round3(Math.min(oE, oEnd));
  if (ce - cs < MIN_CUT_S - 1e-9) return { refused: "small" };
  const before = computeKeeps(base, ctx).output;
  const next = normalizeEdits({ ...base, cuts: [...base.cuts, { start: cs, end: ce }] }, ctx);
  const after = computeKeeps(next, ctx).output;
  if (after < MIN_CLIP_S - 1e-6) return { refused: "floor" };
  return { edits: next, removed: round3(before - after) };
}

function cutOnePause(base: ClipEdits, ctx: EditContext, p: Pause): Applied {
  const at = formatClipTime(p.start);
  const r = addCut(base, ctx, p.start + CUT_PAD_S + base.startDelta, p.end - CUT_PAD_S + base.startDelta);
  if ("refused" in r) {
    return {
      edits: base,
      summary:
        r.refused === "small"
          ? `The pause at ${at} is only ${fmt1(p.length)} s, too short to cut cleanly, so I left it.`
          : `Cutting the pause at ${at} would leave less than ${MIN_CLIP_S} s of clip, so I left it alone.`,
    };
  }
  return { edits: r.edits, summary: `Cut the ${fmt1(p.length)} s pause at ${at}.` };
}

/** Cut every pause in the list, skipping any that would break the floor. */
function cutMany(base: ClipEdits, ctx: EditContext, pauses: Pause[], verb: "Cut" | "Tightened"): Applied {
  let cur = base;
  let n = 0;
  let saved = 0;
  let floored = 0;
  for (const p of pauses) {
    const r = addCut(cur, ctx, p.start + CUT_PAD_S + base.startDelta, p.end - CUT_PAD_S + base.startDelta);
    if ("refused" in r) {
      if (r.refused === "floor") floored++;
      continue;
    }
    cur = r.edits;
    n++;
    saved += r.removed;
  }
  if (n === 0) {
    if (floored > 0) {
      return { edits: base, summary: `Cutting those pauses would leave less than ${MIN_CLIP_S} s of clip, so I left them alone.` };
    }
    const min = verb === "Cut" ? PAUSE_MIN_S : TIGHTEN_MIN_PAUSE_S;
    return {
      edits: base,
      summary:
        verb === "Cut"
          ? `I couldn't find a pause of ${min} s or longer to cut.`
          : `No pauses of ${min} s or longer left. It's already tight.`,
    };
  }
  const total = n + floored;
  const head = floored > 0 ? `${verb} ${n} of ${plural(total, "pause")}` : `${verb} ${plural(n, "pause")}`;
  return { edits: cur, summary: `${head}, saving ${fmt1(round3(saved))} s.` };
}

function applyCutPause(base: ClipEdits, ctx: EditContext, which: "longest" | "all" | { at: number }): Applied {
  const pauses = pausesAbove(base, ctx, PAUSE_MIN_S);
  if (which === "all") return cutMany(base, ctx, pauses, "Cut");
  if (which === "longest") {
    if (pauses.length === 0) {
      return { edits: base, summary: `I couldn't find a pause of ${PAUSE_MIN_S} s or longer to cut.` };
    }
    const longest = pauses.reduce((a, b) => (b.length > a.length ? b : a));
    return cutOnePause(base, ctx, longest);
  }
  const at = which.at;
  if (!Number.isFinite(at)) return { edits: base, summary: "That time didn't parse, so nothing changed." };
  let best: Pause | null = null;
  let bestDist = Infinity;
  for (const p of pauses) {
    const d = at < p.start ? p.start - at : at > p.end ? at - p.end : 0;
    if (d < bestDist) {
      best = p;
      bestDist = d;
    }
  }
  if (!best || bestDist > PAUSE_NEAR_S + 1e-9) {
    return { edits: base, summary: `I couldn't find a pause within a second of ${formatClipTime(at)}.` };
  }
  return cutOnePause(base, ctx, best);
}

function applyEnergetic(base: ClipEdits, ctx: EditContext): Applied {
  // Part one: tighten (threshold TIGHTEN_MIN_PAUSE_S), floor-guarded.
  const pauses = pausesAbove(base, ctx, TIGHTEN_MIN_PAUSE_S);
  let cur = base;
  let n = 0;
  for (const p of pauses) {
    const r = addCut(cur, ctx, p.start + CUT_PAD_S + base.startDelta, p.end - CUT_PAD_S + base.startDelta);
    if ("refused" in r) continue;
    cur = r.edits;
    n++;
  }
  // Part two: up to two punch-ins at the start of the first word at/after
  // ⅓ and ⅔ of the post-tighten OUTPUT duration.
  const resolved = resolveEdits(cur, ctx);
  const { oEnd } = boundsOf(cur, ctx);
  let placed = 0;
  for (const frac of [1 / 3, 2 / 3]) {
    const targetAbs = absoluteAtOutput(resolved, resolved.outputDuration * frac);
    const word = ctx.words.find(
      (w) =>
        w.start >= targetAbs - 1e-6 &&
        w.start < resolved.end - 0.05 &&
        !cur.cuts.some((c) => w.start - ctx.planStart > c.start && w.start - ctx.planStart < c.end)
    );
    if (!word) continue;
    const zs = round3(word.start - ctx.planStart);
    const ze = round3(Math.min(zs + ENERGETIC_ZOOM_S, oEnd));
    if (ze - zs < MIN_CUT_S) continue;
    if (cur.zooms.some((z) => zs >= z.start - ENERGETIC_ZOOM_GAP_S && zs <= z.end + ENERGETIC_ZOOM_GAP_S)) continue;
    if (cur.zooms.length >= MAX_ZOOMS) continue;
    cur = normalizeEdits(
      { ...cur, zooms: [...cur.zooms, { start: zs, end: ze, scale: ENERGETIC_ZOOM_SCALE }] },
      ctx
    );
    placed++;
  }
  if (n === 0 && placed === 0) {
    return { edits: base, summary: "It's already tight and already zoomed, so there's nothing more to add." };
  }
  if (n > 0 && placed > 0) {
    return { edits: cur, summary: `Tightened ${plural(n, "pause")} and added ${placed} punch-in ${placed === 1 ? "zoom" : "zooms"}.` };
  }
  if (n > 0) {
    return { edits: cur, summary: `Tightened ${plural(n, "pause")}, but found no fresh spot for a punch-in zoom.` };
  }
  return { edits: cur, summary: `No pauses worth tightening, but added ${placed} punch-in ${placed === 1 ? "zoom" : "zooms"}.` };
}

function applyZoom(base: ClipEdits, ctx: EditContext, cmd: { at: number; duration?: number; scale?: number }): Applied {
  if (!Number.isFinite(cmd.at)) return { edits: base, summary: "That time didn't parse, so nothing changed." };
  const { oStart, oEnd } = boundsOf(base, ctx);
  const d = cmd.duration;
  const dur = typeof d === "number" && Number.isFinite(d) && d > 0 ? d : ZOOM_DEFAULT_DURATION_S;
  const s = cmd.scale;
  const scale = round3(clamp(typeof s === "number" && Number.isFinite(s) ? s : ZOOM_DEFAULT_SCALE, ZOOM_MIN_SCALE, ZOOM_MAX_SCALE));
  const zs = round3(Math.max(cmd.at + base.startDelta, oStart));
  const ze = round3(Math.min(cmd.at + base.startDelta + dur, oEnd));
  if (ze - zs < 0.05) {
    return { edits: base, summary: `${formatClipTime(cmd.at)} is outside the clip, so I didn't add a zoom.` };
  }
  // Fully inside a cut → normalizeEdits would silently drop it (and the
  // replace path below would delete a surviving zoom first). Same containment
  // predicate normalize uses; refuse truthfully before touching anything.
  // base is already normalized by applyCommand, so base.cuts is canonical.
  if (base.cuts.some((c) => c.start <= zs + 1e-9 && c.end >= ze - 1e-9)) {
    return {
      edits: base,
      summary: `${formatClipTime(cmd.at)} is inside a cut. Clear the cut first, or pick another time.`,
    };
  }
  // A new zoom overlapping an old one replaces it.
  const kept = base.zooms.filter((z) => z.end <= zs + 1e-9 || z.start >= ze - 1e-9);
  const replaced = base.zooms.length - kept.length;
  if (kept.length + 1 > MAX_ZOOMS) {
    return { edits: base, summary: `That would make more than ${MAX_ZOOMS} zooms. Remove one first.` };
  }
  const next = normalizeEdits({ ...base, zooms: [...kept, { start: zs, end: ze, scale }] }, ctx);
  return {
    edits: next,
    summary:
      replaced > 0
        ? `Replaced the overlapping zoom with a ${fmt1(ze - zs)} s zoom at ${formatClipTime(cmd.at)}.`
        : `Added a ${fmt1(ze - zs)} s zoom at ${formatClipTime(cmd.at)}.`,
  };
}

function applyCutRange(base: ClipEdits, ctx: EditContext, s: number, e: number): Applied {
  if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) {
    return { edits: base, summary: "That range didn't parse, so nothing changed." };
  }
  const r = addCut(base, ctx, s + base.startDelta, e + base.startDelta);
  if ("refused" in r) {
    return {
      edits: base,
      summary:
        r.refused === "small"
          ? `That's shorter than ${MIN_CUT_S} s, too small to cut.`
          : `That would leave less than ${MIN_CLIP_S} s of clip, so I left it alone.`,
    };
  }
  const out = fmt1(resolveEdits(r.edits, ctx).outputDuration);
  return { edits: r.edits, summary: `Cut ${fmt1(r.removed)} s. The clip is now ${out} s.` };
}

function applyUseHook(base: ClipEdits, ctx: EditContext, index: number): Applied {
  const n = ctx.hooks.length;
  if (n === 0) return { edits: base, summary: "This clip has no saved hooks to switch between." };
  const wanted = Number.isFinite(index) ? Math.round(index) : 0;
  const idx = clamp(wanted, 0, n - 1);
  if (idx === base.hookIndex) return { edits: base, summary: `Already using hook ${idx + 1}.` };
  const next = { ...base, hookIndex: idx };
  if (wanted > n - 1) return { edits: next, summary: `There are only ${n} hooks, so I switched to hook ${idx + 1}.` };
  if (wanted < 0) return { edits: next, summary: `Hook numbers start at 1, so I switched to hook ${idx + 1}.` };
  return { edits: next, summary: `Switched to hook ${idx + 1} of ${n}.` };
}

// --- The command entry point -----------------------------------------------

function dispatch(base: ClipEdits, command: EditCommand, ctx: EditContext): Applied {
  switch (command.type) {
    case "trim":
      return applyTrim(base, ctx, command.edge, command.seconds);
    case "cutPause":
      return applyCutPause(base, ctx, command.which);
    case "cut":
      return applyCutRange(base, ctx, command.start, command.end);
    case "tighten":
      return cutMany(base, ctx, pausesAbove(base, ctx, TIGHTEN_MIN_PAUSE_S), "Tightened");
    case "energetic":
      return applyEnergetic(base, ctx);
    case "zoom":
      return applyZoom(base, ctx, command);
    case "clearZooms": {
      const n = base.zooms.length;
      if (n === 0) return { edits: base, summary: "There are no zooms to remove." };
      return { edits: { ...base, zooms: [] }, summary: `Removed ${plural(n, "zoom")}.` };
    }
    case "clearCuts": {
      const n = base.cuts.length;
      if (n === 0) return { edits: base, summary: "There are no cuts to restore." };
      return { edits: { ...base, cuts: [] }, summary: `Removed ${plural(n, "cut")}. The full clip plays again.` };
    }
    case "captions": {
      const on = command.on !== false;
      if (base.captions === on) return { edits: base, summary: `Captions are already ${on ? "on" : "off"}.` };
      return { edits: { ...base, captions: on }, summary: `Captions are now ${on ? "on" : "off"}.` };
    }
    case "useHook":
      return applyUseHook(base, ctx, command.index);
    case "cycleHook": {
      const n = ctx.hooks.length;
      if (n <= 1) return { edits: base, summary: "There's only one hook for this clip." };
      const idx = (base.hookIndex + 1) % n;
      return { edits: { ...base, hookIndex: idx }, summary: `Switched to hook ${idx + 1} of ${n}.` };
    }
    case "reset": {
      if (isEmptyEdits(base)) return { edits: base, summary: "There are no edits to reset." };
      return { edits: freshEmpty(), summary: "Reset. Back to the original clip." };
    }
    default:
      return { edits: base, summary: "I don't recognize that command, so nothing changed." };
  }
}

/**
 * Apply one command. Never throws; never mutates its inputs; the summary is
 * one truthful sentence used verbatim as the chat reply.
 */
export function applyCommand(
  edits: ClipEdits,
  command: EditCommand,
  ctx: EditContext
): { edits: ClipEdits; summary: string } {
  try {
    return dispatch(normalizeEdits(edits, ctx), command, ctx);
  } catch {
    return { edits, summary: "Something went wrong applying that, so nothing changed." };
  }
}

// --- Renderer + UI helpers -------------------------------------------------

/**
 * Mirror of highlights.ts `windowWords` (re-base to start, round3, carry
 * `speaker`). Duplicated on purpose — highlights.ts stays frozen.
 */
export function rewindowWords(words: TranscriptWord[], start: number, end: number): TranscriptWord[] {
  const out: TranscriptWord[] = [];
  for (const word of words) {
    if (word.end <= start || word.start >= end) continue;
    out.push({
      text: word.text,
      start: round3(Math.max(0, word.start - start)),
      end: round3(Math.max(0, Math.min(end, word.end) - start)),
      // Diarized speaker rides along — the caption renderer colors by it.
      ...(word.speaker !== undefined ? { speaker: word.speaker } : {}),
    });
  }
  return out;
}

/**
 * Shift keyframe times by −startDelta so a track built for the original
 * bounds replays correctly on shifted bounds. `cropCenterAt` holds flat
 * outside the keyframe range, which covers extension. Pure — croptrack.ts
 * itself stays frozen.
 */
export function retimeTrack(track: CropTrack, startDelta: number): CropTrack {
  const delta = Number.isFinite(startDelta) ? startDelta : 0;
  return {
    keyframes: track.keyframes.map((k) => ({ t: round3(k.t - delta), cx: k.cx })),
    isStatic: track.isStatic,
    coverage: track.coverage,
  };
}

/** Short chips for the UI, e.g. ["Start +2.0s", "2 cuts", "Zoom at 0:08"]. */
export function describeEdits(edits: ClipEdits, ctx: EditContext): string[] {
  const e = normalizeEdits(edits, ctx);
  if (isEmptyEdits(e)) return [];
  const chips: string[] = [];
  if (e.startDelta !== 0) chips.push(`Start ${e.startDelta > 0 ? "+" : "-"}${fmt1(Math.abs(e.startDelta))}s`);
  if (e.endDelta !== 0) chips.push(`End ${e.endDelta > 0 ? "+" : "-"}${fmt1(Math.abs(e.endDelta))}s`);
  if (e.cuts.length > 0) chips.push(plural(e.cuts.length, "cut"));
  for (const z of e.zooms) chips.push(`Zoom at ${formatClipTime(z.start - e.startDelta)}`);
  if (!e.captions) chips.push("Captions off");
  if (e.hookIndex > 0) chips.push(`Hook ${e.hookIndex + 1}`);
  return chips;
}
