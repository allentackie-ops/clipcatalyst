// Behavioural tests for the shared brand-kit core (BRANDKIT.md, Module 1).
// Every tunable is pinned: revert one to a naive value and something below
// goes red. The IO section is exercised through its node degrade path — with
// no IndexedDB the kit must still round-trip in memory and never throw.
import {
  EMPTY_KIT,
  isEmptyKit,
  normalizeHex,
  contrastOnBlack,
  logoBox,
  activeWordColor,
  validateLogoFile,
  loadKit,
  saveKit,
  clearKit,
  MAX_LOGO_BYTES,
  LOGO_TYPES,
  DEFAULT_CAPTION_COLOR,
  LOGO_HEIGHT_RATIO,
  LOGO_MAX_WIDTH_RATIO,
  LOGO_MARGIN_RATIO,
} from "../.croptrack-build/brandkit.js";
// brandkit.ts mirrors the palette instead of importing it (the compiled file
// must have zero runtime imports). Compiling diarize.ts alongside lets the
// harness check the mirror against the source of truth.
import { SPEAKER_COLORS } from "../.croptrack-build/diarize.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// The three 9:16 export sizes, plus one landscape frame to prove the corner
// margin comes off the HEIGHT on both axes.
const OUT_960 = { width: 540, height: 960 };
const OUT_1280 = { width: 720, height: 1280 };
const OUT_1920 = { width: 1080, height: 1920 };
const OUT_LANDSCAPE = { width: 1920, height: 1080 };

// Natural logo shapes. WIDE is a 4:1 wordmark, which on a 9:16 frame lands
// EXACTLY on the width clamp (0.045 × 4 == 0.32 × 9/16) without tripping it;
// ULTRA is 8:1 and is the shape the clamp actually bites on.
const WIDE = { width: 800, height: 200 };   // 4:1
const ULTRA = { width: 1600, height: 200 }; // 8:1
const TALL = { width: 300, height: 900 };   // 1:3
const SQUARE = { width: 512, height: 512 };

const kit = (captionColor, logo = null, showLogo = true) => ({ logo, captionColor, showLogo });
const LOGO = { dataUrl: "data:image/png;base64,iVBORw0KGgo=", width: 400, height: 100 };

// 1. EMPTY_KIT / isEmptyKit contract.
ok("EMPTY_KIT shape", eq(EMPTY_KIT, { logo: null, captionColor: null, showLogo: true }), JSON.stringify(EMPTY_KIT));
ok("EMPTY_KIT is frozen", Object.isFrozen(EMPTY_KIT));
ok("isEmptyKit(EMPTY_KIT)", isEmptyKit(EMPTY_KIT));
ok("a colour makes it non-empty", !isEmptyKit(kit("#ff0000")));
ok("a logo makes it non-empty", !isEmptyKit(kit(null, LOGO)));
ok("showLogo off makes it non-empty", !isEmptyKit(kit(null, null, false)));

// 2. Tunables pinned directly (behavioural pins follow below).
ok("MAX_LOGO_BYTES = 2_000_000", MAX_LOGO_BYTES === 2_000_000);
ok("LOGO_TYPES exact", eq([...LOGO_TYPES], ["image/png", "image/jpeg", "image/webp", "image/svg+xml"]), JSON.stringify(LOGO_TYPES));
ok("DEFAULT_CAPTION_COLOR = #a78bfa", DEFAULT_CAPTION_COLOR === "#a78bfa");
ok("DEFAULT_CAPTION_COLOR == SPEAKER_COLORS[0] (diarize.ts)", DEFAULT_CAPTION_COLOR === SPEAKER_COLORS[0], SPEAKER_COLORS[0]);
ok("the mirrored palette matches diarize.ts exactly",
  SPEAKER_COLORS.every((c, i) => activeWordColor(i, SPEAKER_COLORS.length, EMPTY_KIT) === c),
  JSON.stringify(SPEAKER_COLORS.map((_, i) => activeWordColor(i, SPEAKER_COLORS.length, EMPTY_KIT))));
ok("LOGO_HEIGHT_RATIO = 0.045", LOGO_HEIGHT_RATIO === 0.045);
ok("LOGO_MAX_WIDTH_RATIO = 0.32", LOGO_MAX_WIDTH_RATIO === 0.32);
ok("LOGO_MARGIN_RATIO = 0.02", LOGO_MARGIN_RATIO === 0.02);

// 3. normalizeHex: expansion, case folding, and every reject reason.
{
  ok("3-digit expands", normalizeHex("#abc") === "#aabbcc", `${normalizeHex("#abc")}`);
  ok("3-digit expands (digits)", normalizeHex("#123") === "#112233", `${normalizeHex("#123")}`);
  ok("3-digit expands (black)", normalizeHex("#000") === "#000000");
  ok("3-digit expands (white)", normalizeHex("#FFF") === "#ffffff");
  ok("6-digit passes through", normalizeHex("#a78bfa") === "#a78bfa");
  ok("uppercase folds down", normalizeHex("#A78BFA") === "#a78bfa", `${normalizeHex("#A78BFA")}`);
  ok("mixed case folds down", normalizeHex("#aB3Cd9") === "#ab3cd9");
  ok("surrounding whitespace forgiven", normalizeHex("  #ABC  ") === "#aabbcc", `${normalizeHex("  #ABC  ")}`);

  // Rejects: missing hash.
  ok("rejects missing hash (6)", normalizeHex("a78bfa") === null);
  ok("rejects missing hash (3)", normalizeHex("abc") === null);
  ok("rejects a double hash", normalizeHex("##abc") === null);
  // Rejects: bad length.
  ok("rejects 2 digits", normalizeHex("#ab") === null);
  ok("rejects 4 digits (no alpha shorthand)", normalizeHex("#abcd") === null);
  ok("rejects 5 digits", normalizeHex("#abcde") === null);
  ok("rejects 7 digits", normalizeHex("#abcdef1") === null);
  ok("rejects 8 digits (no alpha)", normalizeHex("#aabbccdd") === null);
  ok("rejects a bare hash", normalizeHex("#") === null);
  ok("rejects the empty string", normalizeHex("") === null);
  // Rejects: bad characters (right length, wrong alphabet).
  ok("rejects non-hex letters", normalizeHex("#gggggg") === null);
  ok("rejects a stray g in 6", normalizeHex("#abcdeg") === null);
  ok("rejects a stray z in 3", normalizeHex("#abz") === null);
  ok("rejects spaces inside", normalizeHex("#ab cde") === null);
  ok("rejects rgb()", normalizeHex("rgb(1,2,3)") === null);
  ok("rejects a named colour", normalizeHex("violet") === null);
  // Rejects: not a string at all.
  ok("rejects null", normalizeHex(null) === null);
  ok("rejects undefined", normalizeHex(undefined) === null);
  ok("rejects a number", normalizeHex(0xa78bfa) === null);
  ok("rejects an object", normalizeHex({}) === null);
}

// 4. contrastOnBlack: WCAG 2.1 ratio against the caption's black box.
{
  ok("black on black = 1", contrastOnBlack("#000000") === 1);
  ok("white on black = 21", contrastOnBlack("#ffffff") === 21);
  ok("default violet = 7.72", contrastOnBlack("#a78bfa") === 7.72, `${contrastOnBlack("#a78bfa")}`);
  ok("speaker amber = 12.58", contrastOnBlack("#fbbf24") === 12.58, `${contrastOnBlack("#fbbf24")}`);
  ok("speaker sky = 9.8", contrastOnBlack("#38bdf8") === 9.8, `${contrastOnBlack("#38bdf8")}`);
  ok("speaker rose = 7.8", contrastOnBlack("#fb7185") === 7.8, `${contrastOnBlack("#fb7185")}`);
  ok("pure red = 5.25", contrastOnBlack("#ff0000") === 5.25, `${contrastOnBlack("#ff0000")}`);
  ok("pure green = 15.3", contrastOnBlack("#00ff00") === 15.3, `${contrastOnBlack("#00ff00")}`);
  ok("pure blue = 2.44", contrastOnBlack("#0000ff") === 2.44, `${contrastOnBlack("#0000ff")}`);
  ok("mid grey = 5.32", contrastOnBlack("#808080") === 5.32, `${contrastOnBlack("#808080")}`);
  ok("near-black = 1.21", contrastOnBlack("#1a1a1a") === 1.21, `${contrastOnBlack("#1a1a1a")}`);
  ok("blue-600 = 4.06", contrastOnBlack("#2563eb") === 4.06, `${contrastOnBlack("#2563eb")}`);

  ok("shorthand matches longhand", contrastOnBlack("#fff") === contrastOnBlack("#ffffff"));
  ok("case-insensitive", contrastOnBlack("#A78BFA") === contrastOnBlack("#a78bfa"));
  ok("invalid hex answers the worst case", contrastOnBlack("nope") === 1);
  ok("empty string answers the worst case", contrastOnBlack("") === 1);

  // The UI warns below 3 — the default must never trip its own warning, and a
  // dark brand pick must.
  ok("default colour is above the warn line", contrastOnBlack(DEFAULT_CAPTION_COLOR) >= 3);
  ok("pure blue trips the warn line", contrastOnBlack("#0000ff") < 3);
  ok("ratio is bounded 1..21", [...Array(64)].every((_, i) => {
    const h = `#${i.toString(16).padStart(2, "0").repeat(3)}`;
    const c = contrastOnBlack(h);
    return c >= 1 && c <= 21;
  }));
}

// 5. logoBox: exact geometry at every export size. Heights pin
//    LOGO_HEIGHT_RATIO, x/y pin LOGO_MARGIN_RATIO, the ULTRA widths pin
//    LOGO_MAX_WIDTH_RATIO.
{
  // 540×960: height 43.2 → 43, margin 19.2 → 19.
  ok("960 wide 4:1", eq(logoBox(WIDE, OUT_960), { x: 348, y: 898, width: 173, height: 43 }), JSON.stringify(logoBox(WIDE, OUT_960)));
  ok("960 ultra 8:1 (clamped)", eq(logoBox(ULTRA, OUT_960), { x: 348, y: 919, width: 173, height: 22 }), JSON.stringify(logoBox(ULTRA, OUT_960)));
  ok("960 tall 1:3", eq(logoBox(TALL, OUT_960), { x: 507, y: 898, width: 14, height: 43 }), JSON.stringify(logoBox(TALL, OUT_960)));
  ok("960 square", eq(logoBox(SQUARE, OUT_960), { x: 478, y: 898, width: 43, height: 43 }), JSON.stringify(logoBox(SQUARE, OUT_960)));

  // 720×1280: height 57.6 → 58, margin 25.6 → 26.
  ok("1280 wide 4:1", eq(logoBox(WIDE, OUT_1280), { x: 464, y: 1196, width: 230, height: 58 }), JSON.stringify(logoBox(WIDE, OUT_1280)));
  ok("1280 ultra 8:1 (clamped)", eq(logoBox(ULTRA, OUT_1280), { x: 464, y: 1225, width: 230, height: 29 }), JSON.stringify(logoBox(ULTRA, OUT_1280)));
  ok("1280 tall 1:3", eq(logoBox(TALL, OUT_1280), { x: 675, y: 1196, width: 19, height: 58 }), JSON.stringify(logoBox(TALL, OUT_1280)));
  ok("1280 square", eq(logoBox(SQUARE, OUT_1280), { x: 636, y: 1196, width: 58, height: 58 }), JSON.stringify(logoBox(SQUARE, OUT_1280)));

  // 1080×1920: height 86.4 → 86, margin 38.4 → 38.
  ok("1920 wide 4:1", eq(logoBox(WIDE, OUT_1920), { x: 696, y: 1796, width: 346, height: 86 }), JSON.stringify(logoBox(WIDE, OUT_1920)));
  ok("1920 ultra 8:1 (clamped)", eq(logoBox(ULTRA, OUT_1920), { x: 696, y: 1839, width: 346, height: 43 }), JSON.stringify(logoBox(ULTRA, OUT_1920)));
  ok("1920 tall 1:3", eq(logoBox(TALL, OUT_1920), { x: 1013, y: 1796, width: 29, height: 86 }), JSON.stringify(logoBox(TALL, OUT_1920)));
  ok("1920 square", eq(logoBox(SQUARE, OUT_1920), { x: 956, y: 1796, width: 86, height: 86 }), JSON.stringify(logoBox(SQUARE, OUT_1920)));

  // The margin comes off the HEIGHT on both axes: on a 1920×1080 frame it is
  // 22 px, not the 38 px a width-derived margin would give.
  ok("landscape margin is height-derived", eq(logoBox(SQUARE, OUT_LANDSCAPE), { x: 1849, y: 1009, width: 49, height: 49 }), JSON.stringify(logoBox(SQUARE, OUT_LANDSCAPE)));

  // The clamp actually engages for ULTRA and actually does NOT for WIDE: the
  // 4:1 wordmark keeps the full LOGO_HEIGHT_RATIO height at every size, the
  // 8:1 one is cut to LOGO_MAX_WIDTH_RATIO of the frame and loses height.
  for (const [label, out] of [["960", OUT_960], ["1280", OUT_1280], ["1920", OUT_1920]]) {
    const wide = logoBox(WIDE, out);
    const ultra = logoBox(ULTRA, out);
    const square = logoBox(SQUARE, out);
    ok(`${label}: 4:1 is NOT clamped (full height kept)`, wide.height === square.height, `${wide.height} vs ${square.height}`);
    ok(`${label}: 8:1 IS clamped (width capped, height shrinks)`, ultra.width === wide.width && ultra.height < wide.height, JSON.stringify(ultra));
    ok(`${label}: clamped width == LOGO_MAX_WIDTH_RATIO of the frame`, ultra.width === Math.round(out.width * LOGO_MAX_WIDTH_RATIO), `${ultra.width}`);
    // Aspect survives the clamp — within the ±0.5 px of the pixel rounding.
    const ratio = ultra.width / ultra.height;
    ok(`${label}: 8:1 aspect preserved after the clamp`, Math.abs(ratio - 8) <= 8 * (0.5 / ultra.height), `${ratio}`);
  }

  // Degenerate inputs still produce a usable box — a logo must never be able
  // to throw inside a render loop.
  ok("zero natural size falls back to square", eq(logoBox({ width: 0, height: 0 }, OUT_1280), logoBox(SQUARE, OUT_1280)), JSON.stringify(logoBox({ width: 0, height: 0 }, OUT_1280)));
  ok("NaN natural size falls back to square", eq(logoBox({ width: NaN, height: NaN }, OUT_1280), logoBox(SQUARE, OUT_1280)));
  ok("missing natural size falls back to square", eq(logoBox({}, OUT_1280), logoBox(SQUARE, OUT_1280)));
  ok("negative natural size falls back to square", eq(logoBox({ width: -10, height: -2 }, OUT_1280), logoBox(SQUARE, OUT_1280)));
  ok("zero output size yields a 1x1 box", eq(logoBox(SQUARE, { width: 0, height: 0 }), { x: 0, y: 0, width: 1, height: 1 }), JSON.stringify(logoBox(SQUARE, { width: 0, height: 0 })));
  ok("garbage output size yields a 1x1 box", eq(logoBox(SQUARE, { width: NaN, height: Infinity }), { x: 0, y: 0, width: 1, height: 1 }));
  ok("a hair-thin logo still gets 1 px", logoBox({ width: 1, height: 100000 }, OUT_1280).width === 1, JSON.stringify(logoBox({ width: 1, height: 100000 }, OUT_1280)));
  ok("logoBox does not mutate its arguments", (() => {
    const nat = Object.freeze({ width: 800, height: 200 });
    const out = Object.freeze({ width: 720, height: 1280 });
    logoBox(nat, out);
    return nat.width === 800 && out.height === 1280;
  })());

  // Invariant sweep: 200 deterministic (fixed-seed LCG) natural sizes across
  // the three export frames. The box is always whole-pixel, at least 1×1,
  // fully inside the frame, inset by exactly the height-derived margin on the
  // right and bottom, and within a pixel of the unrounded ideal.
  let seed = 20250811 % 2147483647;
  const rnd = () => (seed = (seed * 48271) % 2147483647) / 2147483647;
  let holds = true;
  let detail = "";
  for (let i = 0; i < 200 && holds; i++) {
    const out = [OUT_960, OUT_1280, OUT_1920, OUT_LANDSCAPE][i % 4];
    const nat = { width: Math.round(1 + rnd() * 4000), height: Math.round(1 + rnd() * 4000) };
    const b = logoBox(nat, out);
    const margin = Math.round(out.height * LOGO_MARGIN_RATIO);
    // The unrounded ideal, straight off the spec's formula.
    const aspect = nat.width / nat.height;
    let eh = out.height * LOGO_HEIGHT_RATIO;
    let ew = eh * aspect;
    const maxW = out.width * LOGO_MAX_WIDTH_RATIO;
    if (ew > maxW) { ew = maxW; eh = ew / aspect; }
    const exactW = Math.max(1, ew);
    const exactH = Math.max(1, eh);
    const good =
      Number.isInteger(b.x) && Number.isInteger(b.y) &&
      Number.isInteger(b.width) && Number.isInteger(b.height) &&
      b.width >= 1 && b.height >= 1 &&
      b.x >= 0 && b.y >= 0 &&
      b.x + b.width <= out.width && b.y + b.height <= out.height &&
      b.x + b.width === out.width - margin &&
      b.y + b.height === out.height - margin &&
      Math.abs(b.width - exactW) <= 1 && Math.abs(b.height - exactH) <= 1;
    if (!good) { holds = false; detail = `case ${i}: ${JSON.stringify({ nat, out, box: b })}`; }
  }
  ok("logoBox invariants hold across the 200-shape sweep", holds, detail);
}

// 6. activeWordColor — the full matrix. Diarization wins over the brand
//    colour whenever the clip has 2+ speakers AND the word is assigned.
{
  const none = EMPTY_KIT;
  const red = kit("#ff0000");
  const logoOnly = kit(null, LOGO);
  const VIOLET = "#a78bfa", AMBER = "#fbbf24", SKY = "#38bdf8", ROSE = "#fb7185";

  // No kit — exactly the pre-brand-kit behaviour, at every speaker count.
  ok("no kit, 1 speaker, unassigned word", activeWordColor(undefined, 1, none) === VIOLET);
  ok("no kit, 1 speaker, speaker 0", activeWordColor(0, 1, none) === VIOLET);
  ok("no kit, 3 speakers, unassigned word", activeWordColor(undefined, 3, none) === VIOLET);
  ok("no kit, 3 speakers, speaker 0", activeWordColor(0, 3, none) === VIOLET);
  ok("no kit, 3 speakers, speaker 1", activeWordColor(1, 3, none) === AMBER, activeWordColor(1, 3, none));
  ok("no kit, 3 speakers, speaker 2", activeWordColor(2, 3, none) === SKY, activeWordColor(2, 3, none));
  ok("no kit, 4 speakers, speaker 3", activeWordColor(3, 4, none) === ROSE);

  // Kit with a colour — it replaces the violet everywhere diarization is not
  // speaking, and NOWHERE else.
  ok("kit colour, 1 speaker, unassigned word", activeWordColor(undefined, 1, red) === "#ff0000");
  ok("kit colour, 1 speaker, speaker 0", activeWordColor(0, 1, red) === "#ff0000", activeWordColor(0, 1, red));
  ok("kit colour, 3 speakers, unassigned word", activeWordColor(undefined, 3, red) === "#ff0000");
  ok("kit colour, 3 speakers, speaker 0 keeps violet", activeWordColor(0, 3, red) === VIOLET, activeWordColor(0, 3, red));
  ok("kit colour, 3 speakers, speaker 1 keeps amber", activeWordColor(1, 3, red) === AMBER, activeWordColor(1, 3, red));
  ok("kit colour, 3 speakers, speaker 2 keeps sky", activeWordColor(2, 3, red) === SKY);

  // The threshold is exactly 2: one speaker is a brand clip, two is a
  // conversation and the palette carries information.
  ok("kit colour, 2 speakers, speaker 1 keeps amber", activeWordColor(1, 2, red) === AMBER, activeWordColor(1, 2, red));
  ok("kit colour, 2 speakers, speaker 0 keeps violet", activeWordColor(0, 2, red) === VIOLET);
  ok("kit colour, 1 speaker beats the palette", activeWordColor(0, 1, red) === "#ff0000");
  ok("kit colour, 0 speakers (undiarized) applies", activeWordColor(undefined, 0, red) === "#ff0000");

  // Kit without a colour (logo only) — the default violet stands.
  ok("logo-only kit, 1 speaker", activeWordColor(undefined, 1, logoOnly) === VIOLET);
  ok("logo-only kit, 1 speaker, speaker 0", activeWordColor(0, 1, logoOnly) === VIOLET);
  ok("logo-only kit, 3 speakers, speaker 1", activeWordColor(1, 3, logoOnly) === AMBER);
  ok("logo-only kit, 3 speakers, unassigned", activeWordColor(undefined, 3, logoOnly) === VIOLET);

  // Colour handling inside the kit.
  ok("kit colour is normalized on use (shorthand)", activeWordColor(undefined, 1, kit("#F00")) === "#ff0000", activeWordColor(undefined, 1, kit("#F00")));
  ok("kit colour is normalized on use (case)", activeWordColor(undefined, 1, kit("#AABBCC")) === "#aabbcc");
  ok("a broken stored colour degrades to the default", activeWordColor(undefined, 1, kit("not-a-colour")) === VIOLET);
  ok("an empty stored colour degrades to the default", activeWordColor(undefined, 1, kit("")) === VIOLET);

  // Out-of-range / garbage speaker indices never throw and never return
  // undefined — the palette wraps, like render.ts's modulo.
  ok("speaker index wraps the palette", activeWordColor(5, 3, none) === AMBER, activeWordColor(5, 3, none));
  ok("speaker index 4 wraps to violet", activeWordColor(4, 3, none) === VIOLET);
  ok("negative speaker index wraps", activeWordColor(-1, 3, none) === ROSE, activeWordColor(-1, 3, none));
  ok("NaN speaker falls back to the brand colour", activeWordColor(NaN, 3, red) === "#ff0000");
  ok("missing kit falls back to the default", activeWordColor(undefined, 1, undefined) === VIOLET);
  ok("null kit falls back to the default", activeWordColor(1, 3, null) === AMBER);

  // Purity: the kit is never written to.
  const frozen = Object.freeze(kit("#00ff00"));
  ok("activeWordColor runs on a frozen kit", activeWordColor(undefined, 1, frozen) === "#00ff00");
  ok("frozen kit unchanged", frozen.captionColor === "#00ff00");
}

// 7. validateLogoFile: the accept case and every reject reason.
{
  ok("a small PNG is accepted", validateLogoFile({ type: "image/png", size: 12_345 }) === null);
  for (const type of LOGO_TYPES) {
    ok(`${type} accepted`, validateLogoFile({ type, size: 1_000 }) === null);
  }
  ok("exactly MAX_LOGO_BYTES accepted", validateLogoFile({ type: "image/png", size: MAX_LOGO_BYTES }) === null);
  ok("one byte over rejected", validateLogoFile({ type: "image/png", size: MAX_LOGO_BYTES + 1 }) !== null);
  ok("one byte over never reads as the limit", validateLogoFile({ type: "image/png", size: MAX_LOGO_BYTES + 1 }) === "That logo is 2.01 MB, and the limit is 2 MB.", validateLogoFile({ type: "image/png", size: MAX_LOGO_BYTES + 1 }));

  const big = validateLogoFile({ type: "image/png", size: 3_400_000 });
  ok("oversize message names both numbers", big === "That logo is 3.4 MB, and the limit is 2 MB.", `${big}`);

  const wrongType = "That file isn't an image we can use. Pick a PNG, JPEG, WebP or SVG.";
  ok("GIF rejected", validateLogoFile({ type: "image/gif", size: 1_000 }) === wrongType, `${validateLogoFile({ type: "image/gif", size: 1_000 })}`);
  ok("PDF rejected", validateLogoFile({ type: "application/pdf", size: 1_000 }) === wrongType);
  ok("empty type rejected", validateLogoFile({ type: "", size: 1_000 }) === wrongType);
  ok("missing type rejected", validateLogoFile({ size: 1_000 }) === wrongType);
  ok("type is checked before size", validateLogoFile({ type: "image/gif", size: 9_000_000 }) === wrongType);

  const empty = "That file is empty. Pick a PNG, JPEG, WebP or SVG.";
  ok("zero-byte file rejected", validateLogoFile({ type: "image/png", size: 0 }) === empty, `${validateLogoFile({ type: "image/png", size: 0 })}`);
  ok("negative size rejected", validateLogoFile({ type: "image/png", size: -1 }) === empty);

  const unreadable = "That file couldn't be read. Try picking it again.";
  ok("NaN size rejected", validateLogoFile({ type: "image/png", size: NaN }) === unreadable, `${validateLogoFile({ type: "image/png", size: NaN })}`);
  ok("missing size rejected", validateLogoFile({ type: "image/png" }) === unreadable);
  ok("no file at all rejected", validateLogoFile(undefined) === unreadable);
  ok("null file rejected", validateLogoFile(null) === unreadable);
}

// 8. IO degrade path. Under node there is no IndexedDB, which is exactly the
//    private-mode / blocked-storage case: nothing throws, and the kit still
//    holds for the session in memory.
{
  ok("no IndexedDB in this harness", typeof indexedDB === "undefined");

  const kitIn = { logo: LOGO, captionColor: "#FF0000", showLogo: false };
  let threw = "";
  try {
    ok("loadKit with nothing stored is EMPTY_KIT", eq(await loadKit(), EMPTY_KIT), JSON.stringify(await loadKit()));
    ok("saveKit resolves to undefined", (await saveKit(kitIn)) === undefined);
    const back = await loadKit();
    ok("saved kit survives in memory", eq(back, { logo: LOGO, captionColor: "#ff0000", showLogo: false }), JSON.stringify(back));
    ok("saveKit normalizes the colour it keeps", back.captionColor === "#ff0000");
    await saveKit({ logo: null, captionColor: "garbage", showLogo: true });
    ok("a broken colour is dropped on save", (await loadKit()).captionColor === null);
    await saveKit({ logo: { dataUrl: "https://evil.example/x.png", width: 10, height: 10 }, captionColor: null, showLogo: true });
    ok("a non-data-URL logo is dropped on save", (await loadKit()).logo === null);
    await saveKit(kitIn);
    ok("clearKit resolves to undefined", (await clearKit()) === undefined);
    ok("cleared kit is EMPTY_KIT again", eq(await loadKit(), EMPTY_KIT));
    await saveKit(null);
    ok("saving garbage stores an empty kit, not a broken one", eq(await loadKit(), EMPTY_KIT));
    await clearKit();
  } catch (err) {
    threw = String(err);
  }
  ok("the IO section never throws under node", threw === "", threw);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
