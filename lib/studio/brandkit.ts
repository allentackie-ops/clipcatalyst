// Brand kit: a creator's own logo in the corner and their own colour on the
// caption highlight. This module is the shared core BOTH renderers obey — the
// browser canvas path (render.ts) and the ffmpeg/ASS path (api/pipeline) — so
// the geometry and the colour rule live here exactly once and the Python side
// is a direct port with a cross-check test, like croptrack and diarize.
//
// Two rules are worth stating up front because every other decision follows:
//
//   1. A brand kit must NEVER be the reason a render fails. Every function
//      below is total: garbage in gets a truthful fallback out, not a throw.
//      normalizeHex answers null, logoBox answers a usable box, loadKit
//      answers EMPTY_KIT. The corner drawing is a nicety; the clip is not.
//   2. Diarization wins over the brand colour. When a clip has two or more
//      speakers, SPEAKER_COLORS keeps colouring the active word — that colour
//      carries information (who is talking) the brand colour would destroy.
//      The brand colour replaces the default violet for unassigned words and
//      single-speaker clips only. activeWordColor is the ONE place that
//      decides this, and both engines call through it.
//
// The plan gate lives with the caller, not here: `watermark_required` on the
// plan means our mark is drawn and the kit's logo is not, and that precedence
// is the renderer's to apply. This module knows nothing about plans.
//
// Everything above the IO section is pure and has ZERO runtime imports (the
// SPEAKER_COLORS mirror below is deliberate) — the compiled JS is loaded
// straight out of .croptrack-build by the node harness.

/** The whole feature. No fonts, no caption templates, no secondary colours —
 *  those are separate features and pretending otherwise is how the pricing
 *  page got into trouble in the first place. */
export type BrandKit = {
  /** Logo drawn bottom-right in place of the ClipCatalyst mark. */
  logo: { dataUrl: string; width: number; height: number } | null;
  /** Caption highlight colour, "#rrggbb". null = the default violet. */
  captionColor: string | null;
  /** Draw the logo at all. Off = a clean corner (paid plans only). */
  showLogo: boolean;
};

/** The logo half of a kit, named for callers that pass it around alone. */
export type BrandLogo = NonNullable<BrandKit["logo"]>;

/** Pixel box a logo is drawn into, top-left origin, whole pixels. */
export type LogoBox = { x: number; y: number; width: number; height: number };

// --- Tunables. Every one of these is pinned by scripts/brandkit.test.mjs. ---

/** Upload ceiling. 2 MB of data URL is already ~2.7 MB of base64 in the
 *  record, which is why logos live in IndexedDB and not localStorage. */
export const MAX_LOGO_BYTES = 2_000_000;
/** Accepted upload types. SVG is allowed: both engines rasterize it (the
 *  browser via <img>, ffmpeg via its own decoder) at the size we ask for. */
export const LOGO_TYPES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/svg+xml",
];
/** The violet the caption highlight has always used. */
export const DEFAULT_CAPTION_COLOR = "#a78bfa"; // == SPEAKER_COLORS[0]
/** Logo height as a fraction of output height — small enough to read as a
 *  mark rather than a banner at every export size. */
export const LOGO_HEIGHT_RATIO = 0.045;
/** Width ceiling as a fraction of output width, for wordmark-shaped logos.
 *  A 4:1 logo on 9:16 lands exactly on this line (0.045 × 4 == 0.32 × 9/16),
 *  so anything wider than a 4:1 wordmark is what actually gets clamped. */
export const LOGO_MAX_WIDTH_RATIO = 0.32;
/** Corner margin, as a fraction of output HEIGHT on both axes — an equal
 *  visual inset, not an equal fraction of two different edges. */
export const LOGO_MARGIN_RATIO = 0.02;

export const EMPTY_KIT: BrandKit = Object.freeze({
  logo: null,
  captionColor: null,
  showLogo: true,
});

export function isEmptyKit(kit: BrandKit): boolean {
  return kit.logo === null && kit.captionColor === null && kit.showLogo === true;
}

/**
 * Caption palette, index = speaker. diarize.ts owns SPEAKER_COLORS — mirrored
 * here because the import must stay type-only (render.ts mirrors ZOOM_RAMP_S
 * from edits.ts for the same reason). If these ever diverge, diarize.ts wins.
 */
const SPEAKER_COLORS = ["#a78bfa", "#fbbf24", "#38bdf8", "#fb7185"] as const;

// --- Colour ----------------------------------------------------------------

/**
 * "#abc" → "#aabbcc", "#AABBCC" → "#aabbcc"; anything else is null.
 *
 * Surrounding whitespace is forgiven because the UI's hex field is a text
 * input a human types into. Nothing else is: no missing hash, no named
 * colours, no rgb(), no 4/8-digit alpha forms. The Python validator applies
 * the same rule so a colour that survives here survives the upload too.
 */
export function normalizeHex(input: string): string | null {
  if (typeof input !== "string") return null;
  const raw = input.trim().toLowerCase();
  if (raw.length !== 4 && raw.length !== 7) return null;
  if (raw[0] !== "#") return null;
  const body = raw.slice(1);
  for (const ch of body) {
    if (!((ch >= "0" && ch <= "9") || (ch >= "a" && ch <= "f"))) return null;
  }
  if (body.length === 3) return `#${body[0]}${body[0]}${body[1]}${body[1]}${body[2]}${body[2]}`;
  return `#${body}`;
}

/** sRGB channel → linear light (WCAG 2.1 relative luminance). */
function channelLuminance(byte: number): number {
  const c = byte / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

/**
 * WCAG contrast ratio of `hex` against the caption's black box: 1 (invisible)
 * to 21 (white on black), two decimals. The UI warns below 3 — it does not
 * block, because a creator picking their own dark brand colour is making a
 * choice, not a mistake. An unparseable colour answers 1, the worst case, so
 * a broken value always trips the warning rather than passing silently.
 */
export function contrastOnBlack(hex: string): number {
  const norm = normalizeHex(hex);
  if (norm === null) return 1;
  const r = channelLuminance(parseInt(norm.slice(1, 3), 16));
  const g = channelLuminance(parseInt(norm.slice(3, 5), 16));
  const b = channelLuminance(parseInt(norm.slice(5, 7), 16));
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  // Black's luminance is 0, so the ratio is (L + 0.05) / (0 + 0.05).
  return Math.round(((luminance + 0.05) / 0.05) * 100) / 100;
}

/**
 * The colour the active caption word takes — the ONE place both engines
 * agree. Diarization wins: with 2+ speakers an assigned word keeps its
 * speaker colour, because that colour says WHO is talking. Everything else —
 * unassigned words, single-speaker clips — takes the brand colour when the
 * kit has one, else the default violet (== SPEAKER_COLORS[0], so an unbranded
 * clip is coloured exactly as it was before brand kits existed).
 *
 * A stored colour that no longer parses degrades to the default rather than
 * emitting a broken colour string into a canvas or an ASS file.
 */
export function activeWordColor(
  speaker: number | undefined,
  speakerCount: number,
  kit: BrandKit
): string {
  if (speaker !== undefined && Number.isFinite(speaker) && speakerCount >= 2) {
    const idx = ((Math.trunc(speaker) % SPEAKER_COLORS.length) + SPEAKER_COLORS.length) % SPEAKER_COLORS.length;
    return SPEAKER_COLORS[idx];
  }
  const brand = kit ? normalizeHex(kit.captionColor ?? "") : null;
  return brand ?? DEFAULT_CAPTION_COLOR;
}

// --- Geometry --------------------------------------------------------------

/**
 * The box the logo is drawn into, bottom-right, aspect preserved: height is
 * LOGO_HEIGHT_RATIO of the output height, width follows the natural aspect,
 * and a wordmark too wide for LOGO_MAX_WIDTH_RATIO of the output width is
 * clamped with the height RE-DERIVED so it never stretches. The margin is
 * LOGO_MARGIN_RATIO of the output height on both axes.
 *
 * This is the geometry everything else trusts: render.ts drawImage()s into
 * it, and render.py computes the same numbers for its overlay filter. Whole
 * pixels out — a half-pixel overlay origin makes ffmpeg resample the logo.
 *
 * Total by construction: a zero, negative or non-finite natural size falls
 * back to a square, and the box is never smaller than 1×1.
 */
export function logoBox(
  natural: { width: number; height: number },
  out: { width: number; height: number }
): LogoBox {
  const outW = Math.max(1, Math.round(finiteOr(out?.width, 0)));
  const outH = Math.max(1, Math.round(finiteOr(out?.height, 0)));
  const natW = finiteOr(natural?.width, 0);
  const natH = finiteOr(natural?.height, 0);
  // Unknown natural size (an SVG with no intrinsic dimensions, a logo that
  // failed to decode) reads as square rather than as a divide by zero.
  const aspect = natW > 0 && natH > 0 ? natW / natH : 1;

  let height = outH * LOGO_HEIGHT_RATIO;
  let width = height * aspect;
  const maxWidth = outW * LOGO_MAX_WIDTH_RATIO;
  if (width > maxWidth) {
    width = maxWidth;
    height = width / aspect;
  }
  const w = Math.max(1, Math.round(width));
  const h = Math.max(1, Math.round(height));
  const margin = Math.round(outH * LOGO_MARGIN_RATIO);
  // The origin only needs clamping for absurd frames (taller than they are
  // wide by more than ~16:1), where the height-derived margin outgrows the
  // width. Real export sizes never reach it; an off-canvas overlay origin
  // would be an ffmpeg error rather than a missing logo, so it is guarded.
  return {
    x: Math.max(0, outW - margin - w),
    y: Math.max(0, outH - margin - h),
    width: w,
    height: h,
  };
}

function finiteOr(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

// --- Upload validation -----------------------------------------------------

/**
 * Error message for a rejected logo, or null when it is fine. The message is
 * shown to the creator verbatim, so it says what is wrong and what the limit
 * is — never "invalid file".
 *
 * The size here is the browser's; the server re-checks the bytes it actually
 * received and sniffs the content type rather than trusting this one.
 */
export function validateLogoFile(file: { type: string; size: number }): string | null {
  if (!file || typeof file.size !== "number" || !Number.isFinite(file.size)) {
    return "That file couldn't be read. Try picking it again.";
  }
  if (typeof file.type !== "string" || !LOGO_TYPES.includes(file.type)) {
    return "That file isn't an image we can use. Pick a PNG, JPEG, WebP or SVG.";
  }
  if (file.size <= 0) {
    return "That file is empty. Pick a PNG, JPEG, WebP or SVG.";
  }
  if (file.size > MAX_LOGO_BYTES) {
    return `That logo is ${formatMb(file.size)} MB, and the limit is ${formatMb(MAX_LOGO_BYTES)} MB.`;
  }
  return null;
}

/** Megabytes for a message, rounded UP to the nearest 10 KB so a file that is
 *  one byte over never reads as "That logo is 2 MB — the limit is 2 MB". */
function formatMb(bytes: number): string {
  return String(Math.ceil(bytes / 10_000) / 100);
}

// ===========================================================================
// IO — IndexedDB, browser only. Nothing above this line touches a global.
// ===========================================================================
//
// The Studio works with no account, so the kit is local-first: one store, one
// key, the logo data URL and the colours in the same record. A 2 MB data URL
// will not fit comfortably in localStorage, which is what rules it out.
//
// Every failure path degrades to an in-memory kit for the session — blocked
// storage in private mode, a quota rejection, a browser that has no
// IndexedDB at all. The kit still applies to this session's renders; it just
// will not survive a reload. A brand kit must never break a render, and
// "could not persist" is not a reason to lose the creator's logo mid-session.
//
// Every entry point is guarded (`typeof indexedDB === "undefined"`), so
// importing this module under node or during prerender is inert.

const DB_NAME = "clipcatalyst";
const DB_VERSION = 1;
const STORE_NAME = "brandkit";
const RECORD_KEY = "kit";
/** A blocked upgrade (another tab holding the old version) leaves open()
 *  pending forever — fall back rather than hang the panel. */
const IDB_TIMEOUT_MS = 3_000;

/** The session's kit when IndexedDB is unavailable or refused the write. */
let memoryKit: BrandKit | null = null;

function hasIndexedDB(): boolean {
  // Some private modes expose the global as null rather than removing it.
  return typeof indexedDB !== "undefined" && !!indexedDB;
}

/** Whatever came out of storage → a kit that is safe to render with. */
function coerceKit(value: unknown): BrandKit {
  if (!value || typeof value !== "object") return EMPTY_KIT;
  const raw = value as Partial<BrandKit>;
  const logo = raw.logo;
  const okLogo =
    !!logo &&
    typeof logo === "object" &&
    typeof logo.dataUrl === "string" &&
    logo.dataUrl.startsWith("data:") &&
    Number.isFinite(logo.width) &&
    Number.isFinite(logo.height);
  return {
    logo: okLogo ? { dataUrl: logo.dataUrl, width: logo.width, height: logo.height } : null,
    captionColor: typeof raw.captionColor === "string" ? normalizeHex(raw.captionColor) : null,
    showLogo: raw.showLogo !== false,
  };
}

function promisify<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IndexedDB request failed"));
  });
}

/** Open (and create) the store, or null when IndexedDB can't serve us. */
function openDb(): Promise<IDBDatabase | null> {
  if (!hasIndexedDB()) return Promise.resolve(null);
  return new Promise<IDBDatabase | null>((resolve) => {
    let settled = false;
    const done = (db: IDBDatabase | null) => {
      if (settled) return;
      settled = true;
      resolve(db);
    };
    const timer = setTimeout(() => done(null), IDB_TIMEOUT_MS);
    const finish = (db: IDBDatabase | null) => {
      clearTimeout(timer);
      done(db);
    };
    let req: IDBOpenDBRequest;
    try {
      req = indexedDB.open(DB_NAME, DB_VERSION);
    } catch {
      finish(null); // storage disabled entirely (some private modes)
      return;
    }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME);
    };
    req.onsuccess = () => {
      const db = req.result;
      // An older DB from a future/parallel schema without our store is not
      // something to repair mid-session; degrade to memory.
      finish(db.objectStoreNames.contains(STORE_NAME) ? db : null);
    };
    req.onerror = () => finish(null);
    req.onblocked = () => finish(null);
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  work: (store: IDBObjectStore) => Promise<T>
): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction(STORE_NAME, mode);
    const result = await work(tx.objectStore(STORE_NAME));
    return result;
  } catch {
    return null;
  } finally {
    try {
      db.close();
    } catch {
      // Closing a already-errored connection is not worth reporting.
    }
  }
}

/** The stored kit. Never throws: EMPTY_KIT on any failure. */
export async function loadKit(): Promise<BrandKit> {
  try {
    const stored = await withStore("readonly", (store) =>
      promisify<unknown>(store.get(RECORD_KEY))
    );
    if (stored === null || stored === undefined) return memoryKit ?? EMPTY_KIT;
    const kit = coerceKit(stored);
    memoryKit = kit;
    return kit;
  } catch {
    return memoryKit ?? EMPTY_KIT;
  }
}

/** Persist the kit. Never throws; a failed write still applies this session. */
export async function saveKit(kit: BrandKit): Promise<void> {
  const clean = coerceKit(kit);
  memoryKit = clean;
  try {
    await withStore("readwrite", (store) => promisify(store.put(clean, RECORD_KEY)));
  } catch {
    // Quota, private mode, a closed connection — the in-memory kit stands.
  }
}

/** Forget the kit everywhere. Never throws. */
export async function clearKit(): Promise<void> {
  memoryKit = null;
  try {
    await withStore("readwrite", (store) => promisify(store.delete(RECORD_KEY)));
  } catch {
    // Nothing to do — the in-memory kit is already gone.
  }
}
