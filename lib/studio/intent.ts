// Deterministic chat brain for the interactive layer: parseIntent maps one
// chat message onto the closed EditCommand set from edits.ts. No LLM, no
// clock, no randomness — the same sentence always parses the same way. The
// parser is deliberately dumb about clip STATE: it reads phrasing only (plus
// ctx.words for "when he says X"), and leaves "did anything actually change"
// to applyCommand, whose summary becomes the visible reply. When a message
// doesn't clearly name a supported command the answer is an honest "unknown"
// carrying concrete example commands the user can type — never a guess.
//
// Two sentence-level passes run before the command families: a negation
// guard ("don't remove the pause" is a wish, not a command) and clause
// splitting on and/then/also/plus so "remove the pause and add a zoom at
// 0:10" executes both — while "when he says salt and pepper" stays whole.
//
// Only `import type` below — the compiled JS must have zero runtime imports so
// the node test harness can load .croptrack-build/intent.js directly.

import type { EditCommand, Pause } from "./edits";

export type IntentContext = {
  clipCount: number;
  activeClip: number; // 0-based
  duration: number; // active clip's CURRENT output duration
  pauses: Pause[]; // active clip's current pauses (display time)
  hooks: string[];
  /** Active clip's current words, display time (for "when he says X"). */
  words: { text: string; start: number }[];
};

export type Intent =
  | { kind: "commands"; commands: EditCommand[]; targetClip: number; note?: string }
  | { kind: "undo" }
  | { kind: "help"; reply: string }
  | { kind: "unknown"; reply: string };

// --- Tunables + canned replies. Pinned by scripts/intent.test.mjs. ----------

/** "make it longer/shorter" with no amount moves the end by this much. */
export const DEFAULT_RESIZE_S = 2;

/** Every example below is a real command this parser accepts — the honesty
 *  rule extends to the help text: never advertise a capability we lack. */
const HELP_REPLY = [
  'I edit with plain commands. Pauses: "remove the pause at 0:14" · "remove all the pauses" · "tighten it up".',
  'Pace: "make it more energetic". Zooms: "add a zoom at 0:08" · "punch in when he says funnels" · "remove the zooms".',
  'Trims: "trim the first 2 seconds" · "start 2 seconds later" · "extend the end by 2 seconds".',
  'Also: "captions off" · "use hook 2" · "try another hook" · "undo" · "reset" — and add "clip 2" to aim at another clip.',
].join(" ");

const UNKNOWN_REPLY =
  'I didn\'t catch that — try "remove the pause at 0:14", "make it more energetic", or "trim the first 2 seconds". Say "help" for the full list.';

/** A sentence governed by a negation token must never execute the command it
 *  negates — a wrong parse is far worse than an honest unknown. Normalize
 *  strips apostrophes, so "don't" arrives as "dont". */
const NEGATION_RE = /\bdont\b|\bdo not\b|\bnever\b|\bno need\b|\bstop\s+(?:\w+\s+)?\w+ing\b/;

const NEGATION_REPLY =
  'Sounds like you want that left alone — I only act on direct commands like "remove the pause at 0:14" or "captions off".';

/** Praising or protecting a pause ("keep the pauses") is not a removal
 *  order; nor is the bare noun without a removal verb. */
const KEEP_PAUSE_REPLY =
  'Sounds like you want that kept, so I left it alone — I only cut pauses when asked directly, like "remove the pause at 0:14" or "remove all the pauses".';

const PAUSE_VERB_REPLY =
  'Do you want it cut? Say "remove the pause at 0:14" or "remove all the pauses".';

const CROP_REPLY =
  'I can\'t change the crop or framing yet — for pacing, try "tighten it up" or "remove the pause at 0:14".';

const CAPTION_STYLE_REPLY =
  'I can only turn captions on or off right now — try "captions off" or "captions on".';

/** clearZooms is the only zoom removal we have; when the user scoped the ask
 *  to one zoom the note must admit the real blast radius. */
const ZOOM_CLEAR_ALL_NOTE =
  'I can\'t remove just one zoom yet, so all zooms were cleared — re-add any you want with "add a zoom at 0:08".';

/** Caption styling words — size, weight, color, font — that we cannot honor. */
const CAPTION_STYLE_RE =
  /\b(?:bigger|smaller|larger|huge|tiny|font|fonts|size|sizes|bold|italic|color|colors|colour|colours|colored|coloured|yellow|red|blue|green|white|black|pink|purple|orange|style|styles|styled|styling|animated?|neon|fancy)\b/;

/** Crop/framing vocabulary — a "tighter" ask about these is not a pause cut. */
const CROP_WORD_RE = /\bcrop(?:s|ped|ping)?\b|\bframing\b|\bframes?\b|\bframed\b|\bshots?\b/;

/** Verbs that protect a pause from removal. */
const PAUSE_PROTECT_RE =
  /\b(?:keep|keeps|keeping|kept|leave|leaves|leaving|left|like|likes|liked|love|loves|loved|work|works|working|worked)\b/;

/** Removal verbs that license cutting a pause. */
const PAUSE_REMOVE_RE =
  /\b(?:remove[ds]?|cut(?:s|ting)?|delete[ds]?|kill(?:s|ed|ing)?|drop(?:s|ped|ping)?|trim(?:s|med|ming)?|clear(?:s|ed|ing)?|tighten(?:s|ed|ing)?|rid)\b/;

// --- Text helpers -----------------------------------------------------------

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/**
 * Lower-case, drop apostrophes ("host's" → "hosts"), turn every other piece
 * of punctuation into whitespace — EXCEPT ":" (0:12), "#" (clip #2) and a dot
 * between digits (1.5). "Zoom At 0:12." and "zoom at 0:12" parse identically.
 */
function normalize(input: string): string {
  return (input || "")
    .toLowerCase()
    .replace(/[’']/g, "")
    .replace(/[–—\-\/]/g, " ")
    .replace(/\.(?!\d)/g, " ")
    .replace(/[^a-z0-9:.#\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const NUMBER_WORDS: Record<string, number> = {
  a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
};

const ORDINAL_WORDS: Record<string, number> = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5,
  sixth: 6, seventh: 7, eighth: 8, ninth: 9, tenth: 10,
};

/** Loose seconds: "0:12", "12", "12s", "1.5 seconds", "half a second",
 *  "a second", "two seconds", "2 minutes" (minutes convert to seconds).
 *  Null when the text carries no number at all. */
function findSeconds(text: string): number | null {
  let m = text.match(/(\d+):(\d{1,2}(?:\.\d+)?)/);
  if (m) return round3(parseInt(m[1], 10) * 60 + parseFloat(m[2]));
  if (/\bhalf\s+(?:an?\s+)?second\b/.test(text)) return 0.5;
  if (/\bhalf\s+(?:a\s+)?min(?:ute)?\b/.test(text)) return 30;
  m = text.match(/\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten)(\s+and\s+a\s+half)?\s+sec(?:ond)?s?\b/);
  if (m) return NUMBER_WORDS[m[1]] + (m[2] ? 0.5 : 0);
  m = text.match(/\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten)(\s+and\s+a\s+half)?\s+min(?:ute)?s?\b/);
  if (m) return round3((NUMBER_WORDS[m[1]] + (m[2] ? 0.5 : 0)) * 60);
  // Minutes MUST be checked before the bare-number fallback: "2 minutes"
  // is 120 s, never 2 s.
  m = text.match(/\b(\d+(?:\.\d+)?)\s*(?:min|mins|minutes?)\b/);
  if (m) return round3(parseFloat(m[1]) * 60);
  m = text.match(/\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)?\b/);
  if (m) return round3(parseFloat(m[1]));
  return null;
}

/** A time the user pointed AT: "at 12" / "at 0:14", or an mm:ss stamp or
 *  unit-bearing number anywhere. A bare count never qualifies — "2 pauses"
 *  must not read as "at 0:02". */
function findStamp(text: string): number | null {
  const at = text.match(/\bat\s+(.+)$/);
  if (at) {
    const t = findSeconds(at[1]);
    if (t !== null) return t;
  }
  let m = text.match(/(\d+):(\d{1,2}(?:\.\d+)?)/);
  if (m) return round3(parseInt(m[1], 10) * 60 + parseFloat(m[2]));
  m = text.match(/\b(\d+(?:\.\d+)?)\s*(?:min|mins|minutes?)\b/);
  if (m) return round3(parseFloat(m[1]) * 60);
  m = text.match(/\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b/);
  if (m) return round3(parseFloat(m[1]));
  return null;
}

function cleanWord(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]/g, "");
}

/** First transcript word matching `target`, case/punct-insensitive; a prefix
 *  counts so "funnel" finds " Funnels," and "laugh" finds "laughing". */
function findSpokenWord(
  words: IntentContext["words"],
  target: string
): { text: string; start: number } | null {
  const t = cleanWord(target);
  if (!t) return null;
  for (const w of words) {
    const c = cleanWord(w.text);
    if (c === t || c.startsWith(t)) return w;
  }
  return null;
}

// --- Clip targeting ---------------------------------------------------------

type ClipTarget = { text: string; clip: number } | { reply: string };

function stripPhrase(text: string, phrase: string): string {
  return text.replace(phrase, " ").replace(/\s+/g, " ").trim();
}

/**
 * "clip 2" / "clip #2" / "the second clip" / "the last clip" → 0-based index,
 * with the phrase removed so family patterns see clean text. No mention →
 * the active clip. Out of range → an unknown-reply naming the valid range.
 */
function extractTarget(text: string, ctx: IntentContext): ClipTarget {
  const count = Math.max(1, ctx.clipCount);
  const outOfRange = (): ClipTarget => ({
    reply:
      count === 1
        ? 'There\'s only one clip here — drop the clip number and I\'ll edit this one.'
        : `There are only ${count} clips — try "clip 1" through "clip ${count}".`,
  });
  let m = text.match(/\bclip\s*#?\s*(\d+)\b/);
  if (m) {
    const n = parseInt(m[1], 10);
    if (n < 1 || n > count) return outOfRange();
    return { text: stripPhrase(text, m[0]), clip: n - 1 };
  }
  m = text.match(/\b(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+clip\b/);
  if (m) {
    const n = ORDINAL_WORDS[m[1]];
    if (n > count) return outOfRange();
    return { text: stripPhrase(text, m[0]), clip: n - 1 };
  }
  m = text.match(/\b(?:the\s+)?(?:last|final)\s+clip\b/);
  if (m) return { text: stripPhrase(text, m[0]), clip: count - 1 };
  return { text, clip: ctx.activeClip };
}

// --- The parser -------------------------------------------------------------

/** Parse one chat message. Never throws; unknown input never guesses. */
export function parseIntent(input: string, ctx: IntentContext): Intent {
  try {
    return parse(input, ctx);
  } catch {
    return { kind: "unknown", reply: UNKNOWN_REPLY };
  }
}

function parse(input: string, ctx: IntentContext): Intent {
  const raw = normalize(input);
  if (!raw) return { kind: "unknown", reply: UNKNOWN_REPLY };

  // Negation guard — "don't remove the pause" must never remove the pause.
  if (NEGATION_RE.test(raw)) return { kind: "unknown", reply: NEGATION_REPLY };

  const target = extractTarget(raw, ctx);
  if ("reply" in target) return { kind: "unknown", reply: target.reply };
  const text = target.text;

  // Help — BARE help-ish messages only ("help", "help?", "what can you
  // do"); "help me cut the pause at 0:14" falls through so the actionable
  // part still parses.
  if (/^(?:please\s+)?help\s*$/.test(text) || /\bwhat (?:can|do) you do\b/.test(text) || /^commands?$/.test(text)) {
    return { kind: "help", reply: HELP_REPLY };
  }

  // Whole-sentence parse FIRST: word-search phrases legitimately contain
  // "and" ("zoom in when he says salt and pepper") and must stay whole.
  const full = parseClause(text, ctx, target.clip);

  // Conjoined requests: split on and/then/also/plus and prefer the split
  // only when it strictly explains more of the input than the whole-sentence
  // parse did — at least two clauses parse to commands, or one parses where
  // the whole sentence parsed to nothing. Parsed clauses execute in sentence
  // order; unparsed ones are named in the note, never silently dropped.
  const clauses = text
    .split(/\s+(?:and|then|also|plus)\s+/)
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
  if (clauses.length >= 2) {
    const parsed = clauses.map((c) => parseClause(c, ctx, target.clip));
    const hits = parsed.filter((p) => p.kind === "commands").length;
    if (hits >= 2 || (hits === 1 && full.kind === "unknown")) {
      const commands: EditCommand[] = [];
      const notes: string[] = [];
      parsed.forEach((p, i) => {
        if (p.kind === "commands") {
          commands.push(...p.commands);
          if (p.note !== undefined) notes.push(p.note);
        } else {
          notes.push(`I didn't understand "${clauses[i]}" — say "help" for what I can do.`);
        }
      });
      if (notes.length === 0) return { kind: "commands", commands, targetClip: target.clip };
      return { kind: "commands", commands, targetClip: target.clip, note: notes.join(" ") };
    }
  }

  return full;
}

/** Parse one clause (a whole sentence or one side of an "and"/"then" split)
 *  against the command families. Negation, help, and clip targeting are
 *  handled by the caller — this sees clean, target-stripped text. */
function parseClause(text: string, ctx: IntentContext, targetClip: number): Intent {
  const cmd = (commands: EditCommand[], note?: string): Intent =>
    note === undefined
      ? { kind: "commands", commands, targetClip }
      : { kind: "commands", commands, targetClip, note };

  // Reset — before trim, so "start over" never reads as a start move.
  if (/\breset\b|\bstart over\b|\bstart again\b|\bfrom scratch\b/.test(text)) {
    return cmd([{ type: "reset" }]);
  }

  // Undo — "undo the zoom(s)/cut(s)" names a specific layer to clear;
  // bare "undo" pops the UI's history stack.
  if (/\bundo\b/.test(text)) {
    if (/\bzooms?\b/.test(text)) return cmd([{ type: "clearZooms" }]);
    if (/\bcuts?\b/.test(text)) return cmd([{ type: "clearCuts" }]);
    return { kind: "undo" };
  }

  // Captions. Styling asks ("make the captions bigger", "yellow captions")
  // are refused honestly — we can only toggle, never style. Otherwise
  // off-words win when both appear ("turn the captions off").
  if (/\bcaptions?\b|\bsubtitles?\b|\bsubs\b/.test(text)) {
    if (CAPTION_STYLE_RE.test(text)) return { kind: "unknown", reply: CAPTION_STYLE_REPLY };
    if (/\boff\b|\bhide\b|\bhidden\b|\bremove\b|\bwithout\b|\bdisable\b|\bkill\b|\bdrop\b|\brid\b|\bno\b|\bmute\b|\blose\b/.test(text)) {
      return cmd([{ type: "captions", on: false }]);
    }
    if (/\bon\b|\bback\b|\bshow\b|\benable\b|\badd\b|\bbring\b|\brestore\b|\bwith\b/.test(text)) {
      return cmd([{ type: "captions", on: true }]);
    }
    return { kind: "unknown", reply: 'On or off? Try "captions off" or "captions on".' };
  }

  // Hooks: an explicit index ("hook b", "hook 2", "the second hook") beats
  // the cycle words, so "switch to hook 2" picks rather than cycles.
  if (/\bhooks?\b/.test(text)) {
    let m = text.match(/\bhook\s+(?:number\s+)?#?\s*(\d+)\b/);
    if (m) return cmd([{ type: "useHook", index: parseInt(m[1], 10) - 1 }]);
    m = text.match(/\bhook\s+([a-e])\b/);
    if (m) return cmd([{ type: "useHook", index: m[1].charCodeAt(0) - 97 }]);
    m = text.match(/\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+hook\b/);
    if (m) return cmd([{ type: "useHook", index: ORDINAL_WORDS[m[1]] - 1 }]);
    if (/\banother\b|\bdifferent\b|\bnext\b|\bnew\b|\bother\b|\belse\b|\bswap\b|\bswitch\b|\bchange\b|\bcycle\b|\brotate\b/.test(text)) {
      return cmd([{ type: "cycleHook" }]);
    }
    return { kind: "unknown", reply: 'Which hook? Try "use hook 2" or "try another hook".' };
  }

  // Zooms. "punch in" belongs here; bare "punchier"/"more punch" is energy.
  if (/\bzoom(?:s|ed|ing)?\b|\bpunch(?:es)?\s+in\b|\bclose\s+up\b/.test(text)) {
    if (/\bremove\b|\bclear\b|\bdelete\b|\bdrop\b|\bkill\b|\brid\b|\blose\b|\bscrap\b|\boff\b|\bno more\b/.test(text)) {
      // clearZooms is the only removal we have. "remove the zoom at 0:12"
      // scoped the ask to one zoom, so the note must admit that ALL zooms
      // were cleared — never imply just that one went.
      if (findStamp(text) !== null) return cmd([{ type: "clearZooms" }], ZOOM_CLEAR_ALL_NOTE);
      return cmd([{ type: "clearZooms" }]);
    }
    const when = text.match(/\b(?:when|where|as)\b(.+)$/);
    if (when) {
      const clause = when[1];
      // "when he laughs" — laughter is only findable as a SPOKEN word; we
      // have no vision model, and we say so instead of pretending.
      if (/\blaugh/.test(clause)) {
        const w = findSpokenWord(ctx.words, "laugh");
        if (w) return cmd([{ type: "zoom", at: round3(w.start) }]);
        return {
          kind: "unknown",
          reply: 'I can\'t detect laughter on screen yet — give me a moment instead, like "zoom at 0:12".',
        };
      }
      const said = clause.match(/\b(?:says?|said|mentions?|mentioned)\s+(?:the\s+word\s+)?([a-z0-9]+)/);
      if (said) {
        const w = findSpokenWord(ctx.words, said[1]);
        if (w) return cmd([{ type: "zoom", at: round3(w.start) }]);
        return {
          kind: "unknown",
          reply: `I couldn't find "${said[1]}" in this clip's transcript — give me a time instead, like "zoom at 0:12".`,
        };
      }
      return {
        kind: "unknown",
        reply: 'I can only place a zoom at a time or on a spoken word — try "zoom at 0:12" or "zoom when he says funnels".',
      };
    }
    const t = findStamp(text);
    if (t !== null) return cmd([{ type: "zoom", at: t }]);
    return {
      kind: "unknown",
      reply: 'Where should the zoom go? Try "zoom at 0:12" or "punch in when he says funnels".',
    };
  }

  // Pauses / tighten. The parser stays dumb about whether pauses exist —
  // applyCommand answers truthfully either way. But "tighter" about the
  // crop/framing/shot is not a pause cut — we can't do that, and say so.
  if (/\btighten\b|\btighter\b/.test(text)) {
    if (CROP_WORD_RE.test(text)) return { kind: "unknown", reply: CROP_REPLY };
    return cmd([{ type: "tighten" }]);
  }
  if (/\b(?:remove|clear|restore|bring|put)\b/.test(text) && /\bcuts\b/.test(text)) {
    return cmd([{ type: "clearCuts" }]);
  }
  if (/\bpauses?\b|\bsilences?\b|\bdead\s+air\b|\bgaps?\b/.test(text)) {
    if (/\bback\b|\brestore\b/.test(text)) return cmd([{ type: "clearCuts" }]);
    // The bare noun is not an order: "I like the pause at 0:14" praises it,
    // "keep the pauses" protects it. Cutting requires a removal verb.
    if (PAUSE_PROTECT_RE.test(text)) return { kind: "unknown", reply: KEEP_PAUSE_REPLY };
    if (!PAUSE_REMOVE_RE.test(text)) return { kind: "unknown", reply: PAUSE_VERB_REPLY };
    if (/\ball\b|\bevery\b|\bpauses\b|\bsilences\b|\bgaps\b|\bdead\s+air\b/.test(text)) {
      return cmd([{ type: "cutPause", which: "all" }]);
    }
    const t = findStamp(text);
    if (t !== null) return cmd([{ type: "cutPause", which: { at: t } }]);
    return cmd([{ type: "cutPause", which: "longest" }]);
  }

  // Energy.
  if (/\benergetic\b|\benergy\b|\benergi[sz]e\b|\bpunchier\b|\bpunchy\b|\bmore\s+punch\b|\bfaster\b|\bsnappier\b|\bsnappy\b|\bpaced?\b|\bpacier\b|\blively\b|\blivelier\b/.test(text)) {
    return cmd([{ type: "energetic" }]);
  }

  // Trims. Ordered from most to least specific phrasing.
  let m = text.match(/\b(?:cut|remove|delete)\b.*?\bfrom\b(.+?)\b(?:to|until|through)\b(.+)$/);
  if (m) {
    const s = findSeconds(m[1]);
    const e = findSeconds(m[2]);
    if (s !== null && e !== null && e > s) return cmd([{ type: "cut", start: s, end: e }]);
  }
  m = text.match(/\b(?:first|opening)\b(.*)$/);
  if (m && /\b(?:trim|cut|remove|drop|chop|shave|lose|skip|kill)\b/.test(text)) {
    // "cut the first second" carries no digit — the bare unit means 1.
    const amount = findSeconds(m[1]) ?? (/\bsec(?:ond)?s?\b/.test(m[1]) ? 1 : null);
    if (amount !== null) return cmd([{ type: "trim", edge: "start", seconds: amount }]);
  }
  m = text.match(/\b(?:last|final)\b(.*)$/);
  if (m && /\b(?:trim|cut|remove|drop|chop|shave|lose|skip|kill)\b/.test(text)) {
    const amount = findSeconds(m[1]) ?? (/\bsec(?:ond)?s?\b/.test(m[1]) ? 1 : null);
    if (amount !== null) return cmd([{ type: "trim", edge: "end", seconds: amount }]);
  }
  if (/\bextend\b|\blonger\b|\blengthen\b/.test(text)) {
    const edge = /\bstart\b|\bbeginning\b|\bfront\b/.test(text) ? ("start" as const) : ("end" as const);
    const amount = findSeconds(text);
    if (amount !== null) return cmd([{ type: "trim", edge, seconds: -amount }]);
    // The note never asserts the edit was applied — applyCommand may
    // truthfully refuse, and its summary is the authority on what happened.
    return cmd(
      [{ type: "trim", edge, seconds: -DEFAULT_RESIZE_S }],
      `You didn't say how much — going with ${DEFAULT_RESIZE_S} seconds; say e.g. "extend the ${edge} by 4 seconds" for a different amount.`
    );
  }
  if (/\bshorter\b|\bshorten\b/.test(text)) {
    const amount = findSeconds(text);
    if (amount !== null) return cmd([{ type: "trim", edge: "end", seconds: amount }]);
    return cmd(
      [{ type: "trim", edge: "end", seconds: DEFAULT_RESIZE_S }],
      `You didn't say how much — going with ${DEFAULT_RESIZE_S} seconds off the end; say e.g. "make it 4 seconds shorter" for a different amount.`
    );
  }
  if (/\bstart\b|\bbeginning\b|\bintro\b/.test(text) && /\blater\b|\bearlier\b|\bsooner\b|\bforward\b|\bback\b/.test(text)) {
    const amount = findSeconds(text);
    if (amount !== null) {
      const sign = /\blater\b|\bforward\b/.test(text) ? 1 : -1;
      return cmd([{ type: "trim", edge: "start", seconds: sign * amount }]);
    }
  }
  if (/\bend\b|\bending\b|\boutro\b/.test(text) && /\bearlier\b|\bsooner\b|\blater\b/.test(text)) {
    const amount = findSeconds(text);
    if (amount !== null) {
      const sign = /\blater\b/.test(text) ? -1 : 1;
      return cmd([{ type: "trim", edge: "end", seconds: sign * amount }]);
    }
  }
  if (/\b(?:trim|cut|take|shave|chop|drop)\b/.test(text) && /\b(?:start|beginning|top|front)\b/.test(text)) {
    const amount = findSeconds(text);
    if (amount !== null) return cmd([{ type: "trim", edge: "start", seconds: amount }]);
  }
  if (/\b(?:trim|cut|take|shave|chop|drop)\b/.test(text) && /\b(?:end|ending|tail)\b/.test(text)) {
    const amount = findSeconds(text);
    if (amount !== null) return cmd([{ type: "trim", edge: "end", seconds: amount }]);
  }

  return { kind: "unknown", reply: UNKNOWN_REPLY };
}
