"use client";

// The brand kit panel (BRANDKIT.md, Module 4) — ONE component with two homes:
// a collapsible panel in the Studio idle view and a card on /account. Both
// edit the same local-first kit (lib/studio/brandkit.ts — IndexedDB, one
// store, one key), because a creator with no account still gets their logo on
// a browser render.
//
// Three rules this UI is not allowed to soften:
//
//   1. The preview tells the truth about THIS plan. On free or anonymous the
//      corner shows OUR mark and the captions keep the default violet,
//      because that is exactly what renders — render.ts applies the same gate
//      off `watermark`. The kit is still saved, and the colour still previews
//      live beside the field; that honest gap is the upsell, and a preview
//      that flattered the free plan would make the upsell a lie.
//   2. The geometry is the renderer's. The logo sits in `logoBox` against a
//      real 9:16 output box, the caption strip sits at the renderer's 72% on
//      the renderer's font ratio. Nothing here re-derives a number that
//      brandkit.ts or render.ts already owns.
//   3. Saving is local first. Every change lands in IndexedDB immediately, so
//      the next render matches the preview whether or not there is an account
//      or a network. A paid, signed-in account ALSO syncs to the server so
//      cloud renders match; a sync that fails is said out loud and changes
//      nothing locally.

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type DragEvent,
} from "react";
import { Card } from "@/components/ui";
import { useAccount } from "@/components/account/AccountProvider";
import { deleteBrand, fetchBrandLogo, putBrand } from "@/lib/account";
import {
  clearKit,
  contrastOnBlack,
  DEFAULT_CAPTION_COLOR,
  EMPTY_KIT,
  isEmptyKit,
  loadKit,
  logoBox,
  LOGO_TYPES,
  MAX_LOGO_BYTES,
  normalizeHex,
  saveKit,
  validateLogoFile,
  type BrandKit,
  type BrandLogo,
} from "@/lib/studio/brandkit";
import { cloudEnabled } from "./cloud";

/**
 * The preview's output box: 9:16 at a real pixel size, handed to `logoBox`
 * directly. Every ratio in brandkit.ts is scale-free, so the corner a creator
 * lines up here at 198×352 is the corner that renders at 1080×1920 — which is
 * the whole point of previewing against the renderer's own geometry instead
 * of eyeballing a percentage.
 */
const PREVIEW_HEIGHT = 352;
const PREVIEW_WIDTH = Math.round((PREVIEW_HEIGHT * 9) / 16);
/** The output height render.ts's few pixel literals (caption corner radius,
 *  progress bar) are written against — scaled here so they keep their real
 *  visual weight instead of overwhelming a 352 px preview. */
const RENDER_REFERENCE_HEIGHT = 1920;

/** Caption words in the preview: three words, 16 characters — inside the
 *  renderer's own 4-word / 18-char strip limit, so this is a strip that could
 *  really come out of a clip. The last word is the active one. */
const PREVIEW_WORDS = ["Your", "brand", "colour"] as const;

/** Presets: the site's own palette plus the diarization colours, so a pick
 *  here always lands somewhere that reads on a black caption box. */
const PRESETS: { hex: string; name: string }[] = [
  { hex: DEFAULT_CAPTION_COLOR, name: "Violet (the default)" },
  { hex: "#e879f9", name: "Fuchsia" },
  { hex: "#38bdf8", name: "Sky" },
  { hex: "#34d399", name: "Emerald" },
  { hex: "#fbbf24", name: "Amber" },
  { hex: "#fb7185", name: "Rose" },
  { hex: "#ffffff", name: "White" },
];

/** Below this the caption is hard to read on its black box. A warning, never
 *  a block: a creator picking their own dark brand colour is making a choice,
 *  not a mistake (brandkit.ts says the same thing where the maths lives). */
const MIN_CONTRAST = 3;

/** Typing a hex fires a change per keystroke — let it settle before the PUT.
 *  Every other control commits, and syncs, the moment it is used. */
const HEX_SYNC_DELAY_MS = 700;
/** Decode budget for a picked logo — the same one the renderer gives it. */
const LOGO_DECODE_TIMEOUT_MS = 3_000;

const ACCEPT = LOGO_TYPES.join(",");

const CHIP_CLS =
  "rounded-full border border-line px-3.5 py-2 text-sm text-zinc-300 transition-colors duration-150 hover:border-line-strong hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";

/** Save/sync state behind the panel's one live region. */
type SaveStatus = "idle" | "saved" | "syncing" | "synced";

/**
 * Natural size of a `data:` URL logo, or null when it can't be decoded inside
 * the budget. The size matters because `logoBox` preserves the aspect: a
 * wordmark and a square badge must not come out the same width.
 *
 * An SVG with no intrinsic dimensions reports 0 — kept as-is rather than
 * guessed at, because logoBox already reads a zero as square and the renderer
 * prefers the decoded size anyway.
 */
function measureLogo(dataUrl: string): Promise<BrandLogo | null> {
  return new Promise((resolve) => {
    if (typeof Image === "undefined") {
      resolve(null);
      return;
    }
    const img = new Image();
    let settled = false;
    const finish = (value: BrandLogo | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), LOGO_DECODE_TIMEOUT_MS);
    img.onload = () =>
      finish({ dataUrl, width: img.naturalWidth, height: img.naturalHeight });
    img.onerror = () => finish(null);
    img.src = dataUrl;
  });
}

/** A picked file as a `data:` URL, or null when the browser can't read it. */
function readAsDataUrl(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      const reader = new FileReader();
      reader.onload = () =>
        resolve(typeof reader.result === "string" ? reader.result : null);
      reader.onerror = () => resolve(null);
      reader.readAsDataURL(file);
    } catch {
      resolve(null);
    }
  });
}

/**
 * The colour to STORE for a picked value. `captionColor` means "override the
 * default", so picking the default violet back stores null — which keeps the
 * kit genuinely empty (isEmptyKit) instead of leaving a colour that agrees
 * with the default and makes every render take the branded path for nothing.
 */
function storedColor(input: string): string | null {
  const norm = normalizeHex(input);
  return norm === null || norm === DEFAULT_CAPTION_COLOR ? null : norm;
}

/** "2 MB" — the upload ceiling, said the way a human would. */
function limitMb(): string {
  return `${MAX_LOGO_BYTES / 1_000_000} MB`;
}

/**
 * The 9:16 preview, drawn with the renderer's own numbers: captions centred
 * at 72% of the height on a 4.2%-of-height font, the mark at a 2% margin, the
 * logo inside `logoBox`, the progress bar along the bottom.
 *
 * `watermark` is the plan's `watermark_required` and carries the whole gate:
 * true means our mark AND the default caption colour, exactly as render.ts
 * resolves it, whatever the kit says.
 */
function KitPreview({
  kit,
  watermark,
}: {
  kit: BrandKit;
  watermark: boolean;
}) {
  const fontSize = PREVIEW_HEIGHT * 0.042;
  const padX = fontSize * 0.62;
  const padY = fontSize * 0.42;
  const lineHeight = fontSize * 1.3;
  const margin = Math.round(PREVIEW_HEIGHT * 0.02);
  const scale = PREVIEW_HEIGHT / RENDER_REFERENCE_HEIGHT;
  const radius = 12 * scale;
  const barHeight = Math.max(1, Math.round(4 * scale));

  const logo = !watermark && kit.showLogo ? kit.logo : null;
  const box = logo
    ? logoBox(logo, { width: PREVIEW_WIDTH, height: PREVIEW_HEIGHT })
    : null;
  const activeColor = watermark
    ? DEFAULT_CAPTION_COLOR
    : (kit.captionColor ?? DEFAULT_CAPTION_COLOR);

  const corner = watermark
    ? "the ClipCatalyst mark"
    : logo
      ? "your logo"
      : "a clean corner";

  return (
    <div
      role="img"
      aria-label={`Preview of a 9:16 clip: captions with the active word in ${activeColor}, and ${corner}.`}
      style={{ width: PREVIEW_WIDTH, height: PREVIEW_HEIGHT }}
      className="relative shrink-0 overflow-hidden rounded-2xl border border-line-strong bg-ink-800"
    >
      {/* Stand-in for the video: a soft key light, so the caption box and the
          corner mark sit on something rather than on flat colour. */}
      <span
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-[22%] h-28 w-28 -translate-x-1/2 rounded-full bg-white/10 blur-2xl"
      />

      {/* Caption strip — the renderer's geometry, at preview scale. */}
      <div
        aria-hidden
        className="absolute inset-x-0 flex justify-center"
        style={{ top: PREVIEW_HEIGHT * 0.72, transform: "translateY(-50%)" }}
      >
        <p
          className="max-w-[86%] text-center font-bold"
          style={{
            backgroundColor: "rgba(0,0,0,0.55)",
            borderRadius: radius,
            padding: `${padY}px ${padX}px`,
            fontSize,
            lineHeight: `${lineHeight}px`,
          }}
        >
          {PREVIEW_WORDS.map((word, i) => (
            <span
              key={word}
              style={{
                color:
                  i === PREVIEW_WORDS.length - 1 ? activeColor : "#ffffff",
              }}
            >
              {word}
              {i < PREVIEW_WORDS.length - 1 ? " " : ""}
            </span>
          ))}
        </p>
      </div>

      {/* The corner, in the renderer's precedence order. */}
      {watermark ? (
        <span
          aria-hidden
          className="absolute font-semibold"
          style={{
            right: margin,
            bottom: margin,
            fontSize: PREVIEW_HEIGHT * 0.021,
            color: "rgba(255,255,255,0.55)",
          }}
        >
          ⚡ ClipCatalyst
        </span>
      ) : box && logo ? (
        // eslint-disable-next-line @next/next/no-img-element -- a data: URL
        // the visitor just picked; there is nothing for next/image to optimize.
        <img
          src={logo.dataUrl}
          alt=""
          aria-hidden
          style={{
            position: "absolute",
            left: box.x,
            top: box.y,
            width: box.width,
            height: box.height,
            opacity: 0.9,
          }}
        />
      ) : null}

      {/* Progress bar: violet, bottom edge, exactly where the render puts it. */}
      <span
        aria-hidden
        className="absolute bottom-0 left-0 bg-brand-500"
        style={{ height: barHeight, width: "38%" }}
      />
    </div>
  );
}

export default function BrandKitPanel({
  variant = "studio",
}: {
  /** "studio" — a collapsible panel under the dropzone, open to anyone;
   *  "account" — a card on the signed-in dashboard. Same kit, same controls,
   *  different frame. */
  variant?: "studio" | "account";
}) {
  const { user, loading: accountLoading } = useAccount();

  // The same rule StudioApp applies to a render: signed out, still loading,
  // or on Free → our mark. /v1/me reports the EFFECTIVE plan, so a canceled
  // subscription is already free here.
  const watermarkRequired =
    accountLoading || user === null || user.entitlements.watermark_required;
  // PUT needs the plan's own brand-kit promise. DELETE deliberately does not
  // (the server says so): clearing your kit off our machines must not depend
  // on what you're paying today, so a lapsed account can still reset.
  const canPush =
    cloudEnabled && user !== null && user.entitlements.brand_kit === true;
  const canClear = cloudEnabled && user !== null;

  const [kit, setKit] = useState<BrandKit>(EMPTY_KIT);
  const [loaded, setLoaded] = useState(false);
  const [hex, setHex] = useState("");
  const [logoError, setLogoError] = useState<string | null>(null);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [syncError, setSyncError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [dragging, setDragging] = useState(false);

  // The kit the callbacks compose on — updated eagerly so a rapid pick →
  // toggle → colour never composes on a stale render's copy.
  const kitRef = useRef<BrandKit>(EMPTY_KIT);
  /** One local↔account reconcile per signed-in session (see the effect). */
  const reconciledRef = useRef(false);
  const deadRef = useRef(false);
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Newest sync wins: an older PUT landing late must not overwrite the
   *  status of the one that replaced it. */
  const syncSeqRef = useRef(0);

  const fieldId = useId();
  const logoInputId = `${fieldId}-logo`;
  const hexInputId = `${fieldId}-hex`;
  const showLogoId = `${fieldId}-show`;
  const hexHintId = `${fieldId}-hex-hint`;
  const bodyId = `${fieldId}-body`;

  useEffect(() => {
    deadRef.current = false;
    return () => {
      deadRef.current = true;
      if (syncTimerRef.current !== null) clearTimeout(syncTimerRef.current);
    };
  }, []);

  // The stored kit. loadKit never throws — EMPTY_KIT covers a blocked or
  // absent IndexedDB, and the panel stays fully usable in memory for the
  // session either way.
  useEffect(() => {
    void loadKit().then((stored) => {
      if (deadRef.current) return;
      kitRef.current = stored;
      setKit(stored);
      setHex(stored.captionColor ?? "");
      setLoaded(true);
    });
  }, []);

  /** Push the whole kit to the server. Empty → DELETE (which also removes the
   *  logo file), otherwise PUT. Not reachable without the entitlement. */
  const syncKit = useCallback(
    async (next: BrandKit) => {
      const clearing = isEmptyKit(next);
      if (clearing ? !canClear : !canPush) return;
      const seq = ++syncSeqRef.current;
      setStatus("syncing");
      setSyncError(null);
      try {
        if (clearing) await deleteBrand();
        else await putBrand(next);
        if (deadRef.current || seq !== syncSeqRef.current) return;
        setStatus("synced");
      } catch (e) {
        if (deadRef.current || seq !== syncSeqRef.current) return;
        // The local kit is untouched and still applies to this browser's
        // renders — say exactly that rather than implying the change is lost.
        setStatus("saved");
        setSyncError(
          e instanceof Error
            ? e.message
            : "Couldn't reach the ClipCatalyst server."
        );
      }
    },
    [canClear, canPush]
  );

  const scheduleSync = useCallback(
    (next: BrandKit, delayMs: number) => {
      if (syncTimerRef.current !== null) clearTimeout(syncTimerRef.current);
      syncTimerRef.current = null;
      if (isEmptyKit(next) ? !canClear : !canPush) return;
      syncTimerRef.current = setTimeout(() => {
        syncTimerRef.current = null;
        void syncKit(next);
      }, delayMs);
    },
    [canClear, canPush, syncKit]
  );

  /**
   * Apply a change: state, IndexedDB, then the server when there is one.
   * Local first and unconditionally — a creator's logo must survive a failed
   * PUT, a signed-out session, and a browser in private mode.
   */
  const commit = useCallback(
    (next: BrandKit, syncDelayMs = 0) => {
      kitRef.current = next;
      setKit(next);
      setStatus("saved");
      setSyncError(null);
      void saveKit(next); // never throws; degrades to memory for the session
      scheduleSync(next, syncDelayMs);
    },
    [scheduleSync]
  );

  // Reconcile this device's kit with the account's, once per signed-in
  // session. It only ever fills a gap on one side:
  //
  //   * the account has a kit and this device doesn't → pull it, so signing
  //     in on a new machine doesn't show an empty panel next to a feature
  //     that is being paid for;
  //   * this device has one and the account doesn't → push it, so a kit built
  //     in Studio before signing in reaches the cloud renderer too.
  //
  // Two kits that both exist are left alone. Local is what renders in this
  // browser, silently overwriting either side is worse than a mismatch the
  // next edit fixes anyway (every commit syncs the WHOLE kit).
  useEffect(() => {
    if (user === null) {
      // Signed out — the next account gets its own reconcile.
      reconciledRef.current = false;
      return;
    }
    if (reconciledRef.current || !loaded || !cloudEnabled) return;
    const remote = user.brand;
    if (!remote) return; // server predates brand kits — nothing to reconcile
    const remoteEmpty =
      remote.logo_url === null &&
      remote.caption_color === null &&
      remote.show_logo !== false;
    if (isEmptyKit(kitRef.current) === remoteEmpty) return;

    if (!remoteEmpty) {
      reconciledRef.current = true;
      void (async () => {
        // The logo comes back as bytes; everything else is already in /v1/me.
        const dataUrl =
          remote.logo_url === null ? null : await fetchBrandLogo();
        const logo = dataUrl === null ? null : await measureLogo(dataUrl);
        if (deadRef.current || !isEmptyKit(kitRef.current)) return;
        const pulled: BrandKit = {
          logo,
          captionColor: normalizeHex(remote.caption_color ?? ""),
          showLogo: remote.show_logo !== false,
        };
        if (isEmptyKit(pulled)) return;
        kitRef.current = pulled;
        setKit(pulled);
        setHex(pulled.captionColor ?? "");
        // It is this device's kit now — and already the account's, so there
        // is nothing to send back.
        void saveKit(pulled);
      })();
      return;
    }
    // Local-only kit. A plan without the entitlement has nowhere to push it,
    // and must stay reconcilable in case the visitor upgrades in this session.
    if (!canPush) return;
    reconciledRef.current = true;
    void syncKit(kitRef.current);
  }, [user, loaded, canPush, syncKit]);

  const pickLogo = useCallback(
    async (file: File) => {
      setLogoError(null);
      const problem = validateLogoFile(file);
      if (problem !== null) {
        setLogoError(problem);
        return;
      }
      const dataUrl = await readAsDataUrl(file);
      const logo = dataUrl === null ? null : await measureLogo(dataUrl);
      if (deadRef.current) return;
      if (logo === null) {
        setLogoError(
          "That image couldn't be read. Try re-exporting it, or pick another file."
        );
        return;
      }
      // Adding a logo turns the corner back on: picking a file and getting
      // nothing in the preview because a toggle was off days ago is a puzzle,
      // not a feature.
      commit({ ...kitRef.current, logo, showLogo: true });
    },
    [commit]
  );

  const onDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      // Claims the drop so Studio's window-level handler doesn't read a logo
      // as a video file to clip (StudioApp checks defaultPrevented).
      e.preventDefault();
      setDragging(false);
      const dropped = e.dataTransfer.files?.[0];
      if (dropped) void pickLogo(dropped);
    },
    [pickLogo]
  );

  const onHexChange = useCallback(
    (value: string) => {
      setHex(value);
      if (value.trim() === "") {
        // Emptying the field is how you say "back to the default violet".
        commit({ ...kitRef.current, captionColor: null }, HEX_SYNC_DELAY_MS);
        return;
      }
      if (normalizeHex(value) === null) return; // half-typed — keep the last
      commit(
        { ...kitRef.current, captionColor: storedColor(value) },
        HEX_SYNC_DELAY_MS
      );
    },
    [commit]
  );

  const resetKit = useCallback(() => {
    kitRef.current = EMPTY_KIT;
    setKit(EMPTY_KIT);
    setHex("");
    setLogoError(null);
    setStatus("saved");
    setSyncError(null);
    // clearKit, not saveKit(EMPTY_KIT): forget the record entirely rather
    // than leave a blank one behind.
    void clearKit();
    scheduleSync(EMPTY_KIT, 0);
  }, [scheduleSync]);

  const activeColor = kit.captionColor ?? DEFAULT_CAPTION_COLOR;
  const contrast = contrastOnBlack(activeColor);
  const lowContrast = contrast < MIN_CONTRAST;
  const hexInvalid = hex.trim() !== "" && normalizeHex(hex) === null;

  const statusLine =
    status === "syncing"
      ? "Saving to your account…"
      : status === "synced"
        ? "Saved. Your cloud renders use it too."
        : status === "saved"
          ? "Saved on this device."
          : "";

  const summary = `${
    kit.logo === null
      ? "No logo"
      : kit.showLogo
        ? "Logo on"
        : "Logo hidden"
  } · ${kit.captionColor ?? "default colour"}`;

  const body = (
    <div className="flex flex-col gap-6 md:flex-row md:gap-7">
      <div className="flex flex-col items-center gap-2.5 md:items-start">
        <KitPreview kit={kit} watermark={watermarkRequired} />
        <p className="font-mono text-[11px] text-zinc-500">
          9:16 · real proportions
        </p>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-6">
        {/* --- Logo ------------------------------------------------------ */}
        <div className="flex flex-col gap-2.5">
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-zinc-500">
            Logo
          </span>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            className={`flex flex-col gap-3 rounded-xl border border-dashed px-4 py-4 transition-colors duration-200 ${
              dragging
                ? "border-brand-400 bg-brand-500/10"
                : "border-line-strong bg-ink-900/60"
            }`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <input
                id={logoInputId}
                type="file"
                accept={ACCEPT}
                className="peer sr-only"
                onChange={(e) => {
                  const picked = e.target.files?.[0];
                  if (picked) void pickLogo(picked);
                  e.target.value = "";
                }}
              />
              <label
                htmlFor={logoInputId}
                className={`cursor-pointer ${CHIP_CLS} peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-brand-400`}
              >
                {kit.logo === null ? "Choose a logo" : "Replace logo"}
              </label>
              <button
                type="button"
                onClick={() => {
                  setLogoError(null);
                  commit({ ...kitRef.current, logo: null });
                }}
                className="rounded-full px-3 py-2 text-sm text-zinc-400 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
              >
                Remove logo
              </button>
            </div>
            <p className="font-mono text-[11px] leading-relaxed text-zinc-500">
              or drop one here · PNG, JPEG, WebP or SVG · ≤ {limitMb()}
            </p>
          </div>
          {logoError ? (
            <p role="alert" className="text-sm leading-relaxed text-ember-300">
              {logoError}
            </p>
          ) : null}
          <div className="flex items-center gap-2.5">
            <input
              id={showLogoId}
              type="checkbox"
              checked={kit.showLogo}
              onChange={(e) =>
                commit({ ...kitRef.current, showLogo: e.target.checked })
              }
              className="h-4 w-4 shrink-0 accent-brand-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
            />
            <label htmlFor={showLogoId} className="text-sm text-zinc-300">
              Draw my logo in the corner
            </label>
          </div>
        </div>

        {/* --- Caption colour -------------------------------------------- */}
        <div className="flex flex-col gap-2.5">
          <span className="font-mono text-[11px] font-medium uppercase tracking-[0.18em] text-zinc-500">
            Caption colour
          </span>
          <div
            role="group"
            aria-label="Caption colour presets"
            className="flex flex-wrap gap-2"
          >
            {PRESETS.map((preset) => {
              const selected = storedColor(preset.hex) === kit.captionColor;
              return (
                <button
                  key={preset.hex}
                  type="button"
                  aria-pressed={selected}
                  aria-label={`${preset.name} caption colour`}
                  title={preset.name}
                  onClick={() => {
                    setHex(preset.hex);
                    commit({
                      ...kitRef.current,
                      captionColor: storedColor(preset.hex),
                    });
                  }}
                  className={`h-8 w-8 rounded-full border-2 transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
                    selected
                      ? "border-brand-400 ring-2 ring-brand-400/50"
                      : "border-white/15 hover:border-white/40"
                  }`}
                  style={{ backgroundColor: preset.hex }}
                />
              );
            })}
          </div>

          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor={hexInputId}
                className="font-mono text-[11px] text-zinc-500"
              >
                Hex
              </label>
              <input
                id={hexInputId}
                type="text"
                spellCheck={false}
                autoComplete="off"
                placeholder={DEFAULT_CAPTION_COLOR}
                value={hex}
                onChange={(e) => onHexChange(e.target.value)}
                aria-describedby={
                  hexInvalid || lowContrast ? hexHintId : undefined
                }
                className="h-10 w-32 rounded-full border border-line bg-ink-950 px-4 font-mono text-sm text-zinc-200 placeholder:text-zinc-600 transition-colors hover:border-line-strong focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
              />
            </div>
            {/* The colour's own live caption preview — always the chosen
                colour, whatever the plan does to the 9:16 preview. */}
            <p
              aria-hidden
              className="rounded-lg bg-black/55 px-2.5 py-2.5 text-sm font-bold leading-none"
            >
              <span className="text-white">this </span>
              <span style={{ color: activeColor }}>word</span>
            </p>
          </div>

          <div id={hexHintId} className="flex flex-col gap-1">
            {hexInvalid ? (
              <p className="text-xs leading-relaxed text-ember-300">
                That isn&rsquo;t a hex colour. Try{" "}
                <span className="font-mono">{DEFAULT_CAPTION_COLOR}</span> or{" "}
                <span className="font-mono">#fff</span>. Your last colour is
                still in use.
              </p>
            ) : null}
            {lowContrast ? (
              <p className="text-xs leading-relaxed text-ember-300">
                Low contrast:{" "}
                <span className="font-mono">{contrast.toFixed(2)}:1</span> on
                the caption&rsquo;s black box. Under {MIN_CONTRAST}:1 is hard
                to read at a glance, though we&rsquo;ll still render it.
              </p>
            ) : null}
          </div>

          <p className="text-xs leading-relaxed text-zinc-500">
            When a clip has two or more speakers, the speaker colours win:
            that colour says who is talking, and your brand colour would paint
            over it.
          </p>
        </div>

        {/* --- Actions + status ------------------------------------------ */}
        <div className="flex flex-col items-start gap-2">
          <button type="button" onClick={resetKit} className={CHIP_CLS}>
            Reset to default
          </button>
          <div aria-live="polite" className="flex flex-col gap-1 empty:hidden">
            {statusLine ? (
              <p className="font-mono text-[11px] text-zinc-500">
                {statusLine}
              </p>
            ) : null}
            {syncError ? (
              <p role="alert" className="text-xs leading-relaxed text-ember-300">
                Saved here, but syncing to your account failed: {syncError} Your
                clips in this browser still use it.
              </p>
            ) : null}
          </div>
        </div>

        {/* --- The plan line, which has to be the truth ------------------- */}
        {watermarkRequired ? (
          <p className="text-xs leading-relaxed text-zinc-400">
            Free clips carry the ClipCatalyst mark and the default caption
            colour, which is what the preview shows. Your kit is saved here and
            lands on your clips from Starter up.{" "}
            <Link
              href="/#pricing"
              className="text-brand-300 underline-offset-4 hover:underline"
            >
              See the plans →
            </Link>
          </p>
        ) : (
          <p className="text-xs leading-relaxed text-zinc-400">
            Your logo replaces our mark and your colour highlights the active
            word, on every clip you render, in this browser and in the cloud.
          </p>
        )}
      </div>
    </div>
  );

  if (variant === "account") {
    return (
      <Card className="p-6 sm:p-7">
        <h2 className="font-display text-lg font-semibold tracking-tight text-white">
          Brand kit
        </h2>
        <p className="mt-2 mb-6 text-sm leading-relaxed text-zinc-400">
          Your logo in the corner and your colour on the caption highlight, on
          browser renders and cloud renders alike.
        </p>
        {body}
      </Card>
    );
  }

  return (
    <div className="mx-auto mt-8 w-full max-w-2xl">
      <Card>
        <h2>
          <button
            type="button"
            aria-expanded={open}
            aria-controls={bodyId}
            onClick={() => setOpen((v) => !v)}
            className={`flex w-full items-center justify-between gap-4 px-5 py-4 text-left transition-colors hover:bg-white/[0.03] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
              open ? "rounded-t-2xl" : "rounded-2xl"
            }`}
          >
            <span className="min-w-0">
              <span className="block text-sm font-medium text-white">
                Brand kit
              </span>
              <span className="mt-1 flex items-center gap-2">
                <span
                  aria-hidden
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: activeColor }}
                />
                <span className="truncate font-mono text-[11px] text-zinc-500">
                  {summary}
                </span>
              </span>
            </span>
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
              className={`shrink-0 text-zinc-500 transition-transform duration-200 ${
                open ? "rotate-180" : ""
              }`}
            >
              <path d="m6 9 6 6 6-6" />
            </svg>
          </button>
        </h2>
        {open ? (
          <div id={bodyId} className="border-t border-line px-5 py-6">
            {body}
          </div>
        ) : null}
      </Card>
    </div>
  );
}
