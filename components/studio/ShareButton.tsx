"use client";

// One-click publish, honestly framed: navigator.share({ files }) opens the
// native share sheet — on a phone that puts TikTok/YouTube/Instagram one tap
// away. Device clips already hold their Blob; cloud clips are fetched first
// ("Preparing…"). Browsers that can't share files get a truthful hint instead
// of a dead publish button.

import { useRef, useState } from "react";

const UNSUPPORTED_HINT =
  "Your browser can't open a share sheet. Download the clip and post from your phone.";
const SHARE_FAILED_HINT =
  "The share sheet didn't open. Download the clip and post from your phone instead.";
const FETCH_FAILED_HINT =
  "Couldn't fetch this clip for sharing. Download it instead.";

export default function ShareButton({
  url,
  blob,
  filename,
  title,
  ariaLabel,
  className = "",
}: {
  /** Where the clip lives (object URL on device, HTTP url in the cloud). */
  url: string;
  /** The clip's real Blob when we already hold it; absent → fetch `url`. */
  blob?: Blob;
  filename: string;
  /** Share-sheet title, usually the clip's hook-y title. */
  title?: string;
  ariaLabel?: string;
  className?: string;
}) {
  const [preparing, setPreparing] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  // One share at a time — a second tap mid-sheet would throw InvalidStateError.
  const busyRef = useRef(false);

  const handleShare = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setHint(null);
    try {
      let file: File;
      if (blob) {
        file = new File([blob], filename, { type: blob.type || "video/mp4" });
      } else {
        // Cloud clips carry no bytes — pull them down before the sheet opens.
        setPreparing(true);
        let fetched: Blob;
        try {
          const res = await fetch(url);
          if (!res.ok) throw new Error(`fetch failed (${res.status})`);
          fetched = await res.blob();
        } catch {
          setHint(FETCH_FAILED_HINT);
          return;
        } finally {
          setPreparing(false);
        }
        file = new File([fetched], filename, { type: fetched.type || "video/mp4" });
      }
      const files = [file];
      if (typeof navigator.share !== "function" || !navigator.canShare?.({ files })) {
        setHint(UNSUPPORTED_HINT);
        return;
      }
      try {
        await navigator.share({ files, title });
      } catch (e) {
        // AbortError = the user closed the sheet — that's a choice, not a bug.
        if (e instanceof Error && e.name === "AbortError") return;
        setHint(SHARE_FAILED_HINT);
      }
    } finally {
      busyRef.current = false;
    }
  };

  return (
    <span className={`relative inline-flex ${className}`}>
      <button
        type="button"
        onClick={() => void handleShare()}
        onBlur={() => setHint(null)}
        disabled={preparing}
        aria-label={ariaLabel}
        className="inline-flex items-center justify-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-4 py-2.5 text-sm font-medium text-white transition-colors duration-200 hover:border-brand-400/60 hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 disabled:cursor-wait disabled:opacity-60"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <path d="M12 15V3m0 0 4 4m-4-4L8 7" />
          <path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
        </svg>
        {preparing ? "Preparing…" : "Share"}
      </button>
      {hint ? (
        <p
          role="status"
          className="absolute bottom-full right-0 z-20 mb-2 w-60 rounded-xl border border-line-strong bg-ink-800 px-3.5 py-2.5 text-left text-xs leading-relaxed text-zinc-300 shadow-[0_8px_32px_rgba(0,0,0,0.5)]"
        >
          {hint}
        </p>
      ) : null}
    </span>
  );
}
