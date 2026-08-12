// Regression tests for the Interactive-layer UI logic that lives in
// components/studio/ClipEditor.tsx + ResultsView.tsx:
//
//   1. Cross-clip word-search commands ("punch in when he says funnels in
//      clip 2" typed in another clip's editor) must be parsed against the
//      TARGET clip's context — words, pauses, duration — never the active
//      clip's (parseChatIntent / buildIntentContext).
//   2. A successful re-render must carry the EDITED plan's words onto the
//      card (bakeRenderResult stores them; displayClip serves them), so
//      words-derived UI like the "N speakers" badge stays honest.
//
// Run:  npx tsc -p scripts/tsconfig.interactive.json && node scripts/interactive.test.mjs
//
// The components are compiled by tsc into .croptrack-build/interactive/.
// A module-hook loader maps "@/..." specifiers into that build dir, adds the
// ".js" the compiler leaves off relative imports, and stubs react/react-dom/
// next-facing modules with inert placeholders — the tests exercise only the
// exported pure functions, never a React render.

import { register } from "node:module";
import { pathToFileURL } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const buildURL = pathToFileURL(path.join(repoRoot, ".croptrack-build", "interactive") + path.sep).href;

const stubs = {
  react:
    "export const useCallback=(f)=>f;export const useEffect=()=>{};" +
    "export const useMemo=(f)=>f();export const useRef=(v)=>({current:v});" +
    "export const useState=(v)=>[typeof v==='function'?v():v,()=>{}];" +
    "export default {};",
  "react/jsx-runtime":
    "export const jsx=()=>null;export const jsxs=()=>null;export const Fragment=null;",
  "react-dom": "export const createPortal=()=>null;export default {};",
  "@/components/ui":
    "const C=()=>null;export const Button=C,Badge=C,Card=C,ScoreRing=C," +
    "Container=C,Eyebrow=C,SectionHeading=C,GradientText=C;",
  "@/lib/studio/render":
    "export const renderClip=async()=>{throw new Error('renderClip stub');};",
};

const loaderSource = `
const buildURL = ${JSON.stringify(buildURL)};
const stubs = ${JSON.stringify(stubs)};
export function resolve(specifier, context, nextResolve) {
  if (Object.prototype.hasOwnProperty.call(stubs, specifier)) {
    return {
      url: "data:text/javascript," + encodeURIComponent(stubs[specifier]),
      shortCircuit: true,
    };
  }
  if (specifier.startsWith("@/")) {
    return { url: new URL(specifier.slice(2) + ".js", buildURL).href, shortCircuit: true };
  }
  if (
    (specifier.startsWith("./") || specifier.startsWith("../")) &&
    context.parentURL &&
    context.parentURL.startsWith(buildURL) &&
    !specifier.endsWith(".js")
  ) {
    return { url: new URL(specifier + ".js", context.parentURL).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
`;
register(new URL("data:text/javascript," + encodeURIComponent(loaderSource)));

const clipEditorMod = await import(
  pathToFileURL(path.join(repoRoot, ".croptrack-build", "interactive", "components", "studio", "ClipEditor.js")).href
);
const resultsViewMod = await import(
  pathToFileURL(path.join(repoRoot, ".croptrack-build", "interactive", "components", "studio", "ResultsView.js")).href
);
const editsMod = await import(
  pathToFileURL(path.join(repoRoot, ".croptrack-build", "interactive", "lib", "studio", "edits.js")).href
);

const { buildIntentContext, parseChatIntent, newEditSession } = clipEditorMod;
const { bakeRenderResult, displayClip } = resultsViewMod;
const { EMPTY_EDITS } = editsMod;

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ok  ${name}`);
  } catch (e) {
    failed += 1;
    console.error(`FAIL  ${name}`);
    console.error(`      ${e.message}`);
  }
}

// --- Fixtures ---------------------------------------------------------------
// Two clips over one absolute-time transcript. "funnels" is spoken in BOTH
// clips at different display times; "convert" only in clip 1; a >0.45 s gap
// sits inside clip 2 so its pause list is non-empty.

const words = [
  // clip 1 window: 10..20
  { text: " the", start: 10.5, end: 10.8, speaker: 0 },
  { text: " best", start: 11.0, end: 11.3, speaker: 0 },
  { text: " funnels", start: 13.0, end: 13.6, speaker: 0 },
  { text: " convert", start: 14.0, end: 14.5, speaker: 1 },
  { text: " fast", start: 15.0, end: 15.4, speaker: 1 },
  { text: " always", start: 19.0, end: 19.5, speaker: 0 },
  // clip 2 window: 100..110
  { text: " now", start: 100.5, end: 100.9, speaker: 0 },
  { text: " about", start: 101.5, end: 101.9, speaker: 0 },
  { text: " funnels", start: 104.5, end: 105.1, speaker: 0 },
  { text: " they", start: 106.0, end: 106.3, speaker: 0 },
  { text: " scale", start: 107.0, end: 107.5, speaker: 0 },
  { text: " up", start: 109.0, end: 109.2, speaker: 0 },
];

const clipBounds = [
  { planStart: 10, planEnd: 20 },
  { planStart: 100, planEnd: 110 },
];

const editContextFor = (i) => ({
  planStart: clipBounds[i].planStart,
  planEnd: clipBounds[i].planEnd,
  sourceDuration: 300,
  words,
  hooks: [`hook-for-clip-${i + 1}`],
});

/** ctxFor with per-clip session edits, mirroring the component's wiring. */
const makeCtxFor = (editsByClip) => (i) =>
  buildIntentContext(i, 2, editsByClip[i] ?? EMPTY_EDITS, editContextFor(i));

// --- Finding 1: cross-clip word search parses against the TARGET clip ------

test("cross-clip 'when he says X' zooms at the TARGET clip's timestamp", () => {
  const intent = parseChatIntent(
    "punch in when he says funnels in clip 2",
    0,
    makeCtxFor([EMPTY_EDITS, EMPTY_EDITS])
  );
  assert.equal(intent.kind, "commands");
  assert.equal(intent.targetClip, 1);
  const zoom = intent.commands.find((c) => c.type === "zoom");
  assert.ok(zoom, "expected a zoom command");
  // "funnels" in clip 2 starts at absolute 104.5 → display 4.5. The pre-fix
  // single-pass parse searched clip 1's words and produced 3.0 (13.0 − 10).
  assert.equal(zoom.at, 4.5);
});

test("word found only in the ACTIVE clip → honest second-pass unknown, verbatim reply", () => {
  const intent = parseChatIntent(
    "punch in when he says convert in clip 2",
    0,
    makeCtxFor([EMPTY_EDITS, EMPTY_EDITS])
  );
  // Pre-fix this parsed clip 1's words, found "convert" at display 4.0, and
  // confidently zoomed clip 2 at a meaningless time. The fix re-parses with
  // clip 2's context, where the word does not exist — the honest unknown.
  assert.equal(intent.kind, "unknown");
  assert.match(intent.reply, /couldn't find "convert"/);
});

test("target clip's CURRENT session edits shape the re-parse (trimmed start shifts display time)", () => {
  const trimmed = { ...EMPTY_EDITS, startDelta: 2 };
  const intent = parseChatIntent(
    "punch in when he says funnels in clip 2",
    0,
    makeCtxFor([EMPTY_EDITS, trimmed])
  );
  assert.equal(intent.kind, "commands");
  assert.equal(intent.targetClip, 1);
  const zoom = intent.commands.find((c) => c.type === "zoom");
  assert.ok(zoom, "expected a zoom command");
  // Clip 2's start moved to 102 → "funnels" now sits at display 2.5.
  assert.equal(zoom.at, 2.5);
});

test("same-clip word search is untouched (single pass, active words)", () => {
  const intent = parseChatIntent(
    "punch in when he says funnels",
    0,
    makeCtxFor([EMPTY_EDITS, EMPTY_EDITS])
  );
  assert.equal(intent.kind, "commands");
  assert.equal(intent.targetClip, 0);
  const zoom = intent.commands.find((c) => c.type === "zoom");
  assert.ok(zoom, "expected a zoom command");
  assert.equal(zoom.at, 3.0);
});

test("plain cross-clip commands survive the re-parse", () => {
  const intent = parseChatIntent(
    "make clip 2 more energetic",
    0,
    makeCtxFor([EMPTY_EDITS, EMPTY_EDITS])
  );
  assert.equal(intent.kind, "commands");
  assert.equal(intent.targetClip, 1);
  assert.equal(intent.commands.length, 1);
  assert.equal(intent.commands[0].type, "energetic");
});

test("buildIntentContext reports the clip's own duration, pauses, hooks, words", () => {
  const ctx = buildIntentContext(1, 2, EMPTY_EDITS, editContextFor(1));
  assert.equal(ctx.clipCount, 2);
  assert.equal(ctx.activeClip, 1);
  assert.equal(ctx.duration, 10);
  assert.deepEqual(ctx.hooks, ["hook-for-clip-2"]);
  // The 101.9 → 104.5 inter-word gap is a real pause inside clip 2.
  assert.ok(ctx.pauses.length >= 1, "expected at least one pause in clip 2");
  assert.deepEqual(
    ctx.words.map((w) => w.text.trim()),
    ["now", "about", "funnels", "they", "scale", "up"]
  );
  assert.equal(ctx.words[2].start, 4.5);
});

// --- Finding 2: edited words flow onto the card -----------------------------

const originalClip = {
  id: "c1",
  start: 10,
  end: 20,
  score: 80,
  title: "Original",
  hooks: ["h"],
  reason: "r",
  tip: "t",
  words: words.slice(0, 6), // speakers 0 AND 1 → "2 speakers" badge pre-edit
  url: "blob:original",
  blob: new Blob([new Uint8Array(100)]),
  mimeType: "video/webm",
  extension: "webm",
};

// The edited render dropped everything after 13.5 s absolute — speaker 1 is
// gone, so the badge must disappear once the edit is baked in.
const editedWords = [
  { text: " the", start: 0.5, end: 0.8, speaker: 0 },
  { text: " best", start: 1.0, end: 1.3, speaker: 0 },
  { text: " funnels", start: 3.0, end: 3.5, speaker: 0 },
];

const renderedEntry = {
  url: "blob:edited",
  blob: new Blob([new Uint8Array(42)]),
  extension: "mp4",
  mimeType: "video/mp4",
  outputDuration: 3.5,
  words: editedWords,
};

test("bakeRenderResult stores the edited plan's words on the session", () => {
  const s = { ...newEditSession(), edits: EMPTY_EDITS, rendering: 0.4, dirty: true };
  const baked = bakeRenderResult(s, EMPTY_EDITS, renderedEntry);
  assert.ok(baked.rendered, "expected a rendered entry");
  assert.equal(baked.rendered.words, editedWords);
  assert.equal(baked.rendered.outputDuration, 3.5);
  assert.equal(baked.rendering, null);
  assert.equal(baked.dirty, false);
  const last = baked.chat[baked.chat.length - 1];
  assert.equal(last.role, "catalyst");
  assert.equal(last.text, "Baked in. The clip is now 3.5 s.");
});

test("bakeRenderResult keeps mid-render edits dirty", () => {
  const midRenderEdits = { ...EMPTY_EDITS, startDelta: 1 };
  const s = { ...newEditSession(), edits: midRenderEdits, rendering: 0.9, dirty: true };
  const baked = bakeRenderResult(s, EMPTY_EDITS, renderedEntry);
  assert.equal(baked.dirty, true, "edits typed mid-render must stay pending");
});

test("displayClip serves the EDITED words (speakers badge follows the file)", () => {
  const s = { ...newEditSession(), rendered: renderedEntry };
  const shown = displayClip(originalClip, s);
  // Pre-fix, displayClip passed originalClip.words through, so the card kept
  // counting 2 speakers on a file that only contains speaker 0.
  assert.equal(shown.words, editedWords);
  const speakers = new Set(
    shown.words.map((w) => w.speaker).filter((sp) => sp !== undefined)
  );
  assert.equal(speakers.size, 1);
});

test("displayClip already served honest duration + bytes + file identity", () => {
  const s = { ...newEditSession(), rendered: renderedEntry };
  const shown = displayClip(originalClip, s);
  assert.equal(shown.end - shown.start, 3.5); // duration label source
  assert.equal(shown.blob.size, 42); // byte-size label source
  assert.equal(shown.url, "blob:edited");
  assert.equal(shown.extension, "mp4");
  assert.equal(shown.mimeType, "video/mp4");
});

test("displayClip without a render returns the clip untouched", () => {
  const shown = displayClip(originalClip, newEditSession());
  assert.equal(shown, originalClip);
});

// ---------------------------------------------------------------------------

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
