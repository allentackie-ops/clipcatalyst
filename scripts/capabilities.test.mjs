// Behavioural tests for the Studio platform gate (components/studio/
// capabilities.ts). The module is pure and import-free, so the npm script
// compiles that single file into .croptrack-build/ and we drive it here with
// plain readings — no DOM, no browser, no React.
//
// Run:  npm run test:capabilities

import {
  classifyPlatform,
  ASSUME_READY,
} from "../.croptrack-build/capabilities.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};

// Real user-agent strings, copied verbatim from the platforms they name.
const UA = {
  iphoneSafari:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
  iphoneChrome:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
  iphoneFirefox:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/127.0 Mobile/15E148 Safari/605.1.15",
  // iPadOS ≥ 13 with "Request Desktop Website" (the default): byte-for-byte a
  // macOS Safari UA. Only maxTouchPoints separates it from a real Mac.
  ipadDesktopUA:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  ipadMobileUA:
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  macSafari:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  androidPhone:
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.71 Mobile Safari/537.36",
  androidTablet:
    "Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.71 Safari/537.36",
  desktopChrome:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.71 Safari/537.36",
  desktopFirefox:
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
  desktopEdge:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.2592.61",
};

const ALL_APIS = {
  hasMediaRecorder: true,
  hasAudioContext: true,
  hasWorker: true,
  hasOfflineAudioContext: true,
  hasCanvasCaptureStream: true,
};

/** Readings for a device; overrides win. Cloud is off unless asked for. */
const input = (over = {}) => ({
  ...ALL_APIS,
  userAgent: UA.desktopChrome,
  maxTouchPoints: 0,
  viewportMinPx: 1440,
  cloudConfigured: false,
  ...over,
});

// --- 1. iOS: every browser there is WebKit ---------------------------------

for (const [label, ua] of [
  ["Safari", UA.iphoneSafari],
  ["Chrome (CriOS)", UA.iphoneChrome],
  ["Firefox (FxiOS)", UA.iphoneFirefox],
]) {
  const v = classifyPlatform(
    input({ userAgent: ua, maxTouchPoints: 5, viewportMinPx: 390 })
  );
  ok(`iPhone ${label}: iOS + phone + webkit`,
    v.isIos && v.isMobile && v.device === "phone" && v.engine === "webkit",
    JSON.stringify(v));
  ok(`iPhone ${label}: blocked with an iOS-specific reason`,
    v.gate === "mobile-blocked" && v.blockReason === "ios-webkit",
    `${v.gate}/${v.blockReason}`);
  ok(`iPhone ${label}: not treated as desktop Safari`, !v.isDesktopSafari);
}

// The founder's exact case: an iPhone where all five API probes PASS. The old
// gate let this straight through to the pipeline and the decode error.
{
  const v = classifyPlatform(
    input({ userAgent: UA.iphoneSafari, maxTouchPoints: 5, viewportMinPx: 390 })
  );
  ok("iPhone with every API present is still not 'ready'",
    v.missingApis.length === 0 && v.gate !== "ready", v.gate);
}

// --- 2. iPadOS behind the macOS UA -----------------------------------------

{
  const ipad = classifyPlatform(
    input({ userAgent: UA.ipadDesktopUA, maxTouchPoints: 5, viewportMinPx: 820 })
  );
  ok("iPad on desktop UA is detected via maxTouchPoints",
    ipad.isIos && ipad.isMobile && ipad.device === "tablet", JSON.stringify(ipad));
  ok("iPad on desktop UA is blocked, not called desktop Safari",
    ipad.gate === "mobile-blocked" && !ipad.isDesktopSafari, ipad.gate);

  const ipadMobileUA = classifyPlatform(
    input({ userAgent: UA.ipadMobileUA, maxTouchPoints: 5, viewportMinPx: 768 })
  );
  ok("iPad on its mobile UA classifies identically",
    ipadMobileUA.isIos && ipadMobileUA.device === "tablet" &&
    ipadMobileUA.gate === "mobile-blocked", JSON.stringify(ipadMobileUA));

  // Same UA string, zero touch points → a real Mac. This is the whole
  // discrimination, so it is asserted from both sides.
  const mac = classifyPlatform(
    input({ userAgent: UA.macSafari, maxTouchPoints: 0, viewportMinPx: 900 })
  );
  ok("Real Mac Safari (same UA, 0 touch points) stays desktop + ready",
    !mac.isIos && !mac.isMobile && mac.device === "desktop" && mac.gate === "ready",
    JSON.stringify(mac));
  ok("Real Mac Safari keeps the desktop-Safari advisory flag",
    mac.isDesktopSafari && mac.engine === "webkit");
  ok("A Mac reporting 1 touch point is still a Mac",
    !classifyPlatform(input({ userAgent: UA.macSafari, maxTouchPoints: 1 })).isIos);
}

// --- 3. Android -------------------------------------------------------------

{
  const phone = classifyPlatform(
    input({ userAgent: UA.androidPhone, maxTouchPoints: 5, viewportMinPx: 412 })
  );
  ok("Android phone: mobile, chromium, capacity reason (not iOS)",
    phone.isMobile && !phone.isIos && phone.device === "phone" &&
    phone.engine === "chromium" && phone.blockReason === "mobile-capacity",
    JSON.stringify(phone));

  const tablet = classifyPlatform(
    input({ userAgent: UA.androidTablet, maxTouchPoints: 5, viewportMinPx: 800 })
  );
  ok("Android tablet (no 'Mobile' token) is a tablet and still mobile",
    tablet.isMobile && tablet.device === "tablet" &&
    tablet.gate === "mobile-blocked", JSON.stringify(tablet));
}

// --- 4. Desktop is untouched ------------------------------------------------

{
  for (const [label, ua, engine] of [
    ["Chrome", UA.desktopChrome, "chromium"],
    ["Firefox", UA.desktopFirefox, "gecko"],
    ["Edge", UA.desktopEdge, "chromium"],
    ["Safari", UA.macSafari, "webkit"],
  ]) {
    const v = classifyPlatform(input({ userAgent: ua }));
    ok(`Desktop ${label}: ready, desktop, ${engine}`,
      v.gate === "ready" && v.device === "desktop" && v.engine === engine &&
      v.blockReason === "none" && !v.isMobile, JSON.stringify(v));
  }

  // A desktop user with a narrow window is not a phone.
  const narrow = classifyPlatform(input({ viewportMinPx: 480 }));
  ok("Narrow desktop window stays ready", narrow.gate === "ready" && !narrow.isMobile);

  // Windows touchscreen laptops report ten touch points.
  const touchLaptop = classifyPlatform(
    input({ maxTouchPoints: 10, viewportMinPx: 768 })
  );
  ok("Windows touch laptop stays desktop + ready",
    touchLaptop.gate === "ready" && !touchLaptop.isMobile, JSON.stringify(touchLaptop));

  // Cloud being configured must not change anything on desktop.
  const cloudDesktop = classifyPlatform(input({ cloudConfigured: true }));
  ok("Cloud configured leaves desktop on the ready path",
    cloudDesktop.gate === "ready" && cloudDesktop.blockReason === "none");
}

// --- 5. Missing APIs --------------------------------------------------------

{
  const noCapture = classifyPlatform(input({ hasCanvasCaptureStream: false }));
  ok("Desktop without captureStream → missing-apis + named API",
    noCapture.gate === "missing-apis" &&
    noCapture.blockReason === "missing-apis" &&
    noCapture.missingApis.join() === "HTMLCanvasElement.captureStream",
    JSON.stringify(noCapture));

  const noRecorder = classifyPlatform(input({ hasMediaRecorder: false }));
  ok("Desktop without MediaRecorder → missing-apis",
    noRecorder.gate === "missing-apis" &&
    noRecorder.missingApis.join() === "MediaRecorder");

  const bare = classifyPlatform(input({
    hasMediaRecorder: false, hasAudioContext: false, hasWorker: false,
    hasOfflineAudioContext: false, hasCanvasCaptureStream: false,
  }));
  ok("All five missing are all reported, in a stable order",
    bare.missingApis.join(",") ===
    "MediaRecorder,AudioContext,Worker,OfflineAudioContext,HTMLCanvasElement.captureStream",
    bare.missingApis.join(","));

  // A phone missing APIs must hear about the phone, not about MediaRecorder.
  const phoneNoApis = classifyPlatform(input({
    userAgent: UA.iphoneSafari, maxTouchPoints: 5, viewportMinPx: 390,
    hasMediaRecorder: false, hasCanvasCaptureStream: false,
  }));
  ok("Phone missing APIs reports the phone reason, not missing-apis",
    phoneNoApis.gate === "mobile-blocked" &&
    phoneNoApis.blockReason === "ios-webkit" &&
    phoneNoApis.missingApis.length === 2, JSON.stringify(phoneNoApis));
}

// --- 6. Cloud turns the phone block into a working path ---------------------

{
  const iphoneCloud = classifyPlatform(input({
    userAgent: UA.iphoneSafari, maxTouchPoints: 5, viewportMinPx: 390,
    cloudConfigured: true,
  }));
  ok("iPhone + cloud configured → cloud-only, never blocked",
    iphoneCloud.gate === "cloud-only" && iphoneCloud.isMobile,
    JSON.stringify(iphoneCloud));
  ok("iPhone + cloud keeps the honest reason for the toggle copy",
    iphoneCloud.blockReason === "ios-webkit");

  // Cloud rendering happens server-side, so a phone with no MediaRecorder and
  // no captureStream is still perfectly usable.
  const phoneCloudNoApis = classifyPlatform(input({
    userAgent: UA.androidPhone, maxTouchPoints: 5, viewportMinPx: 412,
    cloudConfigured: true, hasMediaRecorder: false, hasCanvasCaptureStream: false,
  }));
  ok("Phone + cloud + missing render APIs is still cloud-only",
    phoneCloudNoApis.gate === "cloud-only", phoneCloudNoApis.gate);
}

// --- 7. Unknown / degenerate readings ---------------------------------------

{
  const unknownHandheld = classifyPlatform(input({
    userAgent: "Mozilla/5.0 (Unknown; Mobile-ish device) SomeEngine/1.0",
    maxTouchPoints: 5, viewportMinPx: 360,
  }));
  ok("Unknown touch device with a phone-sized screen is treated as a phone",
    unknownHandheld.isMobile && unknownHandheld.device === "phone" &&
    unknownHandheld.engine === "unknown", JSON.stringify(unknownHandheld));

  // Touch alone, on a big screen, is not enough (kiosks, touch monitors).
  const bigTouch = classifyPlatform(input({
    userAgent: "Mozilla/5.0 (Unknown) SomeEngine/1.0",
    maxTouchPoints: 5, viewportMinPx: 1080,
  }));
  ok("Touch on a large unknown screen is not a phone", !bigTouch.isMobile);

  // SSR-shaped input: empty UA, no viewport. Must not throw or invent mobile.
  const empty = classifyPlatform(input({
    userAgent: "", maxTouchPoints: 0, viewportMinPx: 0,
  }));
  ok("Empty readings degrade to desktop/ready/unknown-engine",
    empty.gate === "ready" && !empty.isMobile && empty.engine === "unknown",
    JSON.stringify(empty));
}

// --- 8. Contract: purity + the SSR placeholder ------------------------------

{
  const src = input({ userAgent: UA.iphoneSafari, maxTouchPoints: 5, viewportMinPx: 390 });
  const before = JSON.stringify(src);
  const a = classifyPlatform(src);
  const b = classifyPlatform(src);
  ok("classifyPlatform is pure (stable output)", JSON.stringify(a) === JSON.stringify(b));
  ok("classifyPlatform does not mutate its input", JSON.stringify(src) === before);

  ok("ASSUME_READY is the desktop/ready placeholder",
    ASSUME_READY.gate === "ready" && ASSUME_READY.blockReason === "none" &&
    ASSUME_READY.isMobile === false && ASSUME_READY.missingApis.length === 0,
    JSON.stringify(ASSUME_READY));
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
