"use client";

// Sign in with Google (LIBRARY.md Part 1) — the ONLY place in the site that
// loads a third-party script, and it loads it lazily.
//
// Three rules this component exists to keep:
//
//   1. It is Google's script, so it lands on ONE page. The tag is injected
//      from an effect inside a component only /account renders, which means
//      no marketing page, no Studio, and no static prerender ever fetches it.
//      Nothing else imports this file.
//   2. No client id, no button, and no script either.
//      NEXT_PUBLIC_GOOGLE_CLIENT_ID is the same build-time switch
//      CC_GOOGLE_CLIENT_ID is on the server. Unset, the endpoint 503s
//      honestly and this renders nothing rather than a control that cannot
//      work. The effect below checks the same flag before loading anything,
//      so such a build makes no request to Google at all.
//   3. Google says who somebody is, and that is all. The ID token goes
//      straight to our API, which verifies it against Google's keys and mints
//      the same session the password path does; nothing here reads or trusts
//      the token's contents.
//
// The button is explicit: no One Tap, no auto-select, no prompt on load.
// Signing in is a click, like everything else on this page.

import { useCallback, useEffect, useRef, useState } from "react";
import { cloudEnabled } from "@/components/studio/cloud";
import { signInWithGoogle } from "@/lib/account";
import { useAccount } from "./AccountProvider";

/** The OAuth client id every ID token must name as its `aud`. Empty = off. */
const GOOGLE_CLIENT_ID = (process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ?? "").trim();

/** Whether the Google button can exist at all: an API to talk to, and a
 *  client id to name. Exported so the auth card can drop its own divider. */
export const googleSignInEnabled = cloudEnabled && GOOGLE_CLIENT_ID !== "";

const GIS_SRC = "https://accounts.google.com/gsi/client";

/** A blocked script tag often fires no `error` at all — cap the wait so the
 *  card says something honest instead of showing a permanent spinner. */
const GIS_LOAD_TIMEOUT_MS = 10_000;

/** GIS renders a fixed-width button; keep it inside Google's own 200–400 px
 *  range and let it follow the card. */
const MIN_BUTTON_WIDTH = 200;
const MAX_BUTTON_WIDTH = 400;

// The slice of Google Identity Services this component uses. Typed here
// rather than pulled from a package: it is three calls, and a dependency for
// three calls is a dependency to keep patched forever.
type GoogleCredentialResponse = { credential?: string };

type GoogleIdentityServices = {
  accounts: {
    id: {
      initialize: (config: {
        client_id: string;
        callback: (response: GoogleCredentialResponse) => void;
        auto_select?: boolean;
        cancel_on_tap_outside?: boolean;
        itp_support?: boolean;
      }) => void;
      renderButton: (
        parent: HTMLElement,
        options: {
          type?: "standard" | "icon";
          theme?: "outline" | "filled_blue" | "filled_black";
          size?: "small" | "medium" | "large";
          text?: "signin_with" | "signup_with" | "continue_with";
          shape?: "rectangular" | "pill" | "circle" | "square";
          logo_alignment?: "left" | "center";
          width?: number;
        }
      ) => void;
    };
  };
};

function gis(): GoogleIdentityServices | null {
  const value = (window as unknown as { google?: GoogleIdentityServices })
    .google;
  return value?.accounts?.id ? value : null;
}

/** One shared load per page: several mounts (or a re-mount after signing out)
 *  reuse the same tag rather than stacking script elements. */
let loader: Promise<void> | null = null;

function loadGis(): Promise<void> {
  if (loader !== null) return loader;
  loader = new Promise<void>((resolve, reject) => {
    if (gis() !== null) {
      resolve();
      return;
    }
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${GIS_SRC}"]`
    );
    const script = existing ?? document.createElement("script");
    const timer = setTimeout(() => {
      // Let the next mount try again — a blocked network now is not a
      // permanently broken button.
      loader = null;
      reject(new Error("Google's sign-in script didn't load."));
    }, GIS_LOAD_TIMEOUT_MS);
    script.addEventListener("load", () => {
      clearTimeout(timer);
      if (gis() === null) {
        loader = null;
        reject(new Error("Google's sign-in script didn't load."));
        return;
      }
      resolve();
    });
    script.addEventListener("error", () => {
      clearTimeout(timer);
      loader = null;
      reject(new Error("Google's sign-in script couldn't be reached."));
    });
    if (existing === null) {
      script.src = GIS_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
  });
  return loader;
}

/** The container's width, clamped into the range GIS accepts. */
function buttonWidth(el: HTMLElement | null): number {
  const measured = el?.clientWidth ?? MAX_BUTTON_WIDTH;
  return Math.round(
    Math.min(MAX_BUTTON_WIDTH, Math.max(MIN_BUTTON_WIDTH, measured))
  );
}

type Phase = "loading" | "ready" | "signing" | "unavailable";

export default function GoogleSignInButton() {
  const { refresh } = useAccount();
  const [phase, setPhase] = useState<Phase>("loading");
  const [error, setError] = useState<string | null>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const deadRef = useRef(false);
  /** The width the button was last drawn at, so a resize only redraws when
   *  the number actually changed. */
  const widthRef = useRef(0);

  // The credential callback. Google hands us an ID token; our API decides
  // what it is worth. A refusal (unverified email, wrong audience, a server
  // with sign-in switched off) arrives as the server's own sentence.
  const handleCredential = useCallback(
    async (response: GoogleCredentialResponse) => {
      const credential = response.credential ?? "";
      if (!credential) {
        setError("Google didn't return a sign-in token. Try again.");
        return;
      }
      setError(null);
      setPhase("signing");
      try {
        await signInWithGoogle(credential);
        // Flips the page to the dashboard; this component unmounts with it.
        await refresh();
      } catch (e) {
        if (deadRef.current) return;
        setPhase("ready");
        setError(
          e instanceof Error
            ? e.message
            : "That Google sign-in didn't work. Try again."
        );
      }
    },
    [refresh]
  );

  // Keep the callback in a ref so re-rendering the button never re-runs the
  // whole load-and-initialize effect.
  const credentialRef = useRef(handleCredential);
  useEffect(() => {
    credentialRef.current = handleCredential;
  }, [handleCredential]);

  useEffect(() => {
    // Hooks cannot sit behind the `if (!googleSignInEnabled) return null`
    // below, so the guard has to be repeated here. Without it this effect
    // still ran in a build with no client id and fetched Google's SDK for a
    // button that renders nothing, which made the privacy page's "no
    // third-party scripts" claim false for every such build.
    if (!googleSignInEnabled) return;

    deadRef.current = false;
    let cancelled = false;

    void loadGis().then(
      () => {
        if (cancelled || deadRef.current) return;
        const google = gis();
        const host = hostRef.current;
        if (google === null || host === null) {
          setPhase("unavailable");
          return;
        }
        google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: (response) => void credentialRef.current(response),
          // Explicit only: nobody is signed in by a page load.
          auto_select: false,
          cancel_on_tap_outside: true,
          itp_support: true,
        });
        widthRef.current = buttonWidth(host);
        host.replaceChildren();
        google.accounts.id.renderButton(host, {
          type: "standard",
          theme: "filled_black",
          size: "large",
          text: "signin_with",
          shape: "pill",
          logo_alignment: "left",
          width: widthRef.current,
        });
        setPhase("ready");
      },
      () => {
        if (cancelled || deadRef.current) return;
        setPhase("unavailable");
      }
    );

    // GIS draws a fixed pixel width, so a rotation or a resized window would
    // leave it stranded at the old one. Redraw only when the clamped width
    // really moved.
    const onResize = () => {
      const google = gis();
      const host = hostRef.current;
      if (google === null || host === null) return;
      const next = buttonWidth(host);
      if (next === widthRef.current) return;
      widthRef.current = next;
      host.replaceChildren();
      google.accounts.id.renderButton(host, {
        type: "standard",
        theme: "filled_black",
        size: "large",
        text: "signin_with",
        shape: "pill",
        logo_alignment: "left",
        width: next,
      });
    };
    window.addEventListener("resize", onResize);
    return () => {
      cancelled = true;
      deadRef.current = true;
      window.removeEventListener("resize", onResize);
    };
  }, []);

  if (!googleSignInEnabled) return null;

  return (
    <div className="mt-6 flex flex-col gap-3">
      <div className="flex items-center gap-3" aria-hidden>
        <span className="h-px flex-1 bg-line" />
        <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">
          or
        </span>
        <span className="h-px flex-1 bg-line" />
      </div>

      <div className="flex flex-col items-center gap-2">
        {/* Google renders its own (keyboard-reachable, labelled) button in
            here. Left empty for React so the two never fight over the node —
            and given up on entirely once we know nothing is coming, rather
            than holding a button's worth of empty space open forever. */}
        <div
          ref={hostRef}
          className={
            phase === "unavailable"
              ? "hidden"
              : "min-h-[44px] w-full max-w-[400px]"
          }
        />
        {phase === "loading" ? (
          <p className="font-mono text-[11px] text-zinc-600">
            Loading Google sign-in…
          </p>
        ) : null}
        {phase === "unavailable" ? (
          <p className="text-center text-xs leading-relaxed text-zinc-500">
            Google sign-in couldn&rsquo;t load. A blocker or a flaky
            connection will do that. Email and password above works either
            way.
          </p>
        ) : null}
      </div>

      <div aria-live="polite" className="flex flex-col gap-1 empty:hidden">
        {phase === "signing" ? (
          <p className="text-center text-sm text-brand-300">
            Signing you in with Google…
          </p>
        ) : null}
        {error ? (
          <p
            role="alert"
            className="text-center text-sm leading-relaxed text-ember-300"
          >
            {error}
          </p>
        ) : null}
      </div>

      <p className="text-center text-xs leading-relaxed text-zinc-500">
        Google tells us who you are and nothing more. Posting to TikTok,
        Instagram or YouTube is a separate connection you add later.
      </p>
    </div>
  );
}
