// Platform classification for Studio — "can this device actually run the
// on-device pipeline, and if not, what is the honest reason?"
//
// PURE AND IMPORT-FREE ON PURPOSE. `classifyPlatform` takes readings and
// returns a verdict; it touches no globals, so scripts/capabilities.test.mjs
// can compile this one file on its own (npm run test:capabilities) and drive
// every branch without a DOM. `readBrowserCapabilities` is the only function
// here that reads the browser, and it is never called by the tests.
//
// Why this file exists: the old gate was five `typeof` probes. Every one of
// them passes on a modern iPhone, so Studio happily accepted a video there,
// spent the user's time, and then failed inside decodeAudioData with a
// message blaming their file. The truth is that the pipeline — Whisper,
// face tracking, MediaRecorder rendering — is a desktop workload, and on iOS
// every browser is WebKit, which will not hand a video container's audio
// track to Web Audio at all. Better to say so before the upload.

/** The five runtime APIs the on-device pipeline cannot run without. */
export type StudioApiReadings = {
  hasMediaRecorder: boolean;
  hasAudioContext: boolean;
  hasWorker: boolean;
  hasOfflineAudioContext: boolean;
  hasCanvasCaptureStream: boolean;
};

export type CapabilityInput = StudioApiReadings & {
  /** navigator.userAgent, verbatim. Case is normalized internally. */
  userAgent: string;
  /** navigator.maxTouchPoints. iPadOS ≥ 13 reports 5 behind a macOS UA. */
  maxTouchPoints: number;
  /** Shorter viewport edge in CSS px (0 = unknown, e.g. during SSR). */
  viewportMinPx: number;
  /** cloudEnabled from ./cloud — a cloud API base was set at build time. */
  cloudConfigured: boolean;
};

/** Rendering engine family. On iOS this is always "webkit", whatever the
 *  browser calls itself: Chrome, Edge and Firefox for iOS are WebKit skins. */
export type StudioEngineFamily = "webkit" | "chromium" | "gecko" | "unknown";

export type StudioDeviceClass = "desktop" | "tablet" | "phone";

/**
 * What the Studio view should do:
 * - "ready"          desktop with every API present — today's behaviour.
 * - "cloud-only"     mobile with a cloud API configured. Nothing is blocked;
 *                    the cloud engine is preselected because it is the one
 *                    that works here.
 * - "mobile-blocked" mobile with no cloud engine. Say so before any upload.
 * - "missing-apis"   desktop missing one of the five APIs — today's
 *                    UnsupportedView, unchanged.
 */
export type StudioGate =
  | "ready"
  | "cloud-only"
  | "mobile-blocked"
  | "missing-apis";

/** Why the on-device engine is not the recommended path (if it isn't). */
export type StudioBlockReason =
  | "none"
  /** iOS/iPadOS: WebKit's decodeAudioData refuses video containers. */
  | "ios-webkit"
  /** Any other phone/tablet: the pipeline needs more memory than a tab gets. */
  | "mobile-capacity"
  /** Desktop browser without MediaRecorder/captureStream/Web Audio/Workers. */
  | "missing-apis";

export type CapabilityVerdict = {
  gate: StudioGate;
  blockReason: StudioBlockReason;
  engine: StudioEngineFamily;
  device: StudioDeviceClass;
  /** iPhone, iPod or iPad — including iPadOS behind its macOS disguise. */
  isIos: boolean;
  /** Phone or tablet of any kind. */
  isMobile: boolean;
  /** Safari on a real Mac: passes every probe, may still refuse the final
   *  render step — advise, never block (this is the pre-existing banner). */
  isDesktopSafari: boolean;
  /** Which of the five APIs were absent, in a stable order. */
  missingApis: string[];
};

/**
 * The verdict assumed before the browser has been read: a plain desktop with
 * everything present. Used as the initial state so the server-rendered markup
 * and the first client paint match exactly what shipped before this file.
 */
export const ASSUME_READY: CapabilityVerdict = {
  gate: "ready",
  blockReason: "none",
  engine: "unknown",
  device: "desktop",
  isIos: false,
  isMobile: false,
  isDesktopSafari: false,
  missingApis: [],
};

/**
 * Below this (shorter viewport edge, CSS px) a touch device with an
 * unrecognized UA is treated as a phone. Deliberately only ever used
 * TOGETHER with a touch count and the absence of a desktop OS token — a
 * narrow desktop window must never be mistaken for a phone.
 */
const SMALL_SCREEN_MAX_PX = 820;

/** UA tokens that only ever appear on a desktop operating system. */
const DESKTOP_OS = /windows nt|macintosh|cros|x11/;

/** Non-Apple, non-Android mobile platforms still in the wild. */
const OTHER_MOBILE = /windows phone|iemobile|blackberry|bb10|opera mini|opera mobi|kindle|silk\//;

/**
 * iPhone/iPod/iPad, including the iPadOS 13+ disguise.
 *
 * Since iPadOS 13, "Request Desktop Website" is the DEFAULT for Safari on
 * iPad and it sends a verbatim macOS Safari user agent — no "iPad" anywhere.
 * The remaining tell is the touch count: Macs report maxTouchPoints 0 (even
 * with a Touch Bar or a trackpad), iPads report 5.
 */
function detectIos(ua: string, maxTouchPoints: number): boolean {
  if (ua.includes("iphone") || ua.includes("ipod") || ua.includes("ipad")) {
    return true;
  }
  return ua.includes("macintosh") && maxTouchPoints > 1;
}

function detectEngine(ua: string, isIos: boolean): StudioEngineFamily {
  // Every engine on iOS is WebKit — the App Store rules leave no other
  // option — so CriOS/FxiOS/EdgiOS are all WebKit for our purposes.
  if (isIos) return "webkit";
  if (ua.includes("firefox/") || ua.includes("fxios/")) return "gecko";
  if (
    ua.includes("edg/") ||
    ua.includes("chrome/") ||
    ua.includes("chromium/") ||
    ua.includes("crios/") ||
    ua.includes("opr/") ||
    ua.includes("samsungbrowser/")
  ) {
    return "chromium";
  }
  // Desktop Safari is the only thing left that says "safari" and nothing
  // else; every Chromium UA also carries a Safari token, hence the ordering.
  if (ua.includes("safari/")) return "webkit";
  return "unknown";
}

/**
 * Classify a browser from readings alone. Pure: same input → same verdict,
 * and the input object is never mutated.
 */
export function classifyPlatform(input: CapabilityInput): CapabilityVerdict {
  const ua = input.userAgent.toLowerCase();
  const isIos = detectIos(ua, input.maxTouchPoints);
  const isAndroid = ua.includes("android");
  const engine = detectEngine(ua, isIos);

  // A touch device with a phone-sized screen and no desktop OS token. Both
  // signals are required: Windows touchscreen laptops report ten touch
  // points, and a desktop user can always drag their window narrow.
  const smallScreen =
    input.viewportMinPx > 0 && input.viewportMinPx <= SMALL_SCREEN_MAX_PX;
  const unknownHandheld =
    !DESKTOP_OS.test(ua) && input.maxTouchPoints > 1 && smallScreen;

  const isMobile =
    isIos || isAndroid || OTHER_MOBILE.test(ua) || unknownHandheld;

  let device: StudioDeviceClass = "desktop";
  if (isIos) {
    // "ipad", or the macOS-UA disguise, means tablet; otherwise iPhone/iPod.
    device = ua.includes("ipad") || ua.includes("macintosh") ? "tablet" : "phone";
  } else if (isAndroid) {
    // Android tablets omit the "Mobile" token that Android phones carry.
    device = ua.includes("mobile") ? "phone" : "tablet";
  } else if (isMobile) {
    device = "phone";
  }

  const missingApis: string[] = [];
  if (!input.hasMediaRecorder) missingApis.push("MediaRecorder");
  if (!input.hasAudioContext) missingApis.push("AudioContext");
  if (!input.hasWorker) missingApis.push("Worker");
  if (!input.hasOfflineAudioContext) missingApis.push("OfflineAudioContext");
  if (!input.hasCanvasCaptureStream) {
    missingApis.push("HTMLCanvasElement.captureStream");
  }

  // Order matters. On a phone, "your browser is missing recording APIs" is
  // the wrong answer even when it is literally true — the reason clipping
  // won't happen here is that it's a phone. Mobile is decided first, and a
  // configured cloud engine means nothing is blocked at all.
  let gate: StudioGate;
  let blockReason: StudioBlockReason;
  if (isMobile) {
    gate = input.cloudConfigured ? "cloud-only" : "mobile-blocked";
    blockReason = isIos ? "ios-webkit" : "mobile-capacity";
  } else if (missingApis.length > 0) {
    gate = "missing-apis";
    blockReason = "missing-apis";
  } else {
    gate = "ready";
    blockReason = "none";
  }

  return {
    gate,
    blockReason,
    engine,
    device,
    isIos,
    isMobile,
    isDesktopSafari: engine === "webkit" && !isMobile,
    missingApis,
  };
}

/**
 * Read the live browser into a CapabilityInput. The only impure function in
 * this file; call it from a client effect, never during render or SSR.
 */
export function readBrowserCapabilities(
  cloudConfigured: boolean
): CapabilityInput {
  const hasWindow = typeof window !== "undefined";
  const nav = hasWindow ? window.navigator : undefined;
  return {
    userAgent: nav?.userAgent ?? "",
    maxTouchPoints: nav?.maxTouchPoints ?? 0,
    viewportMinPx: hasWindow
      ? Math.min(window.innerWidth || 0, window.innerHeight || 0)
      : 0,
    cloudConfigured,
    hasMediaRecorder: hasWindow && typeof window.MediaRecorder !== "undefined",
    hasAudioContext: hasWindow && typeof window.AudioContext !== "undefined",
    hasWorker: hasWindow && typeof window.Worker !== "undefined",
    hasOfflineAudioContext:
      hasWindow && typeof window.OfflineAudioContext !== "undefined",
    hasCanvasCaptureStream:
      typeof HTMLCanvasElement !== "undefined" &&
      "captureStream" in HTMLCanvasElement.prototype,
  };
}
