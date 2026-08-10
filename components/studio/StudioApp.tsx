"use client";

// ClipCatalyst Studio — the real in-browser clipping pipeline.
// All pipeline state comes from useStudioPipeline; this component owns the
// file pick + settings (lifted so they survive reset) and the five views:
// idle / running / done / error / unsupported-browser.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Container, Eyebrow, Logo } from "@/components/ui";
import { useStudioPipeline } from "./useStudioPipeline";
import StudioDropzone from "./StudioDropzone";
import StageChecklist from "./StageChecklist";
import ClipCard from "./ClipCard";
import {
  DEFAULT_SETTINGS,
  formatBytes,
  formatDuration,
  MAX_SOURCE_SECONDS,
  probeVideoDuration,
  type StudioUISettings,
} from "./format";

/** The browser must hold the whole file in memory — cap sources at ~1.4 GB. */
const MAX_SOURCE_BYTES = 1_400_000_000;

// Raw pipeline errors can be transformers.js/network noise; translate the
// common "model download died" shapes into one human sentence.
function friendlyErrorMessage(raw: string): string {
  return /fetch|network|Failed to load|ERR_/i.test(raw)
    ? "Couldn't download the AI model — check your connection and try again."
    : raw;
}

function TopBar() {
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-ink-950/85 backdrop-blur">
      <Container className="flex h-14 items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            href="/"
            aria-label="ClipCatalyst home"
            className="rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
          >
            <Logo />
          </Link>
          <span className="hidden h-4 w-px bg-line-strong sm:block" aria-hidden />
          <span className="hidden items-center gap-2 sm:flex">
            <span className="text-sm font-medium text-zinc-300">Studio</span>
            <Badge tone="neutral">Beta</Badge>
          </span>
        </div>
        <Link
          href="/demo"
          className="rounded-lg text-sm text-zinc-400 transition-colors hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
        >
          Product tour →
        </Link>
      </Container>
    </header>
  );
}

function UnsupportedView() {
  return (
    <div className="mx-auto w-full max-w-xl text-center">
      <Card className="p-8 sm:p-12">
        <span
          className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border border-line-strong bg-ink-800"
          aria-hidden
        >
          <svg
            width="24"
            height="24"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-ember-400"
          >
            <rect x="3" y="4" width="18" height="13" rx="2" />
            <path d="M8 21h8m-4-4v4" />
          </svg>
        </span>
        <h1
          tabIndex={-1}
          className="mt-6 font-display text-2xl font-semibold tracking-tight text-white outline-none sm:text-3xl"
        >
          Studio needs a modern desktop browser
        </h1>
        <p className="mx-auto mt-4 max-w-md text-sm leading-relaxed text-zinc-400">
          The whole pipeline — AI transcription, scoring, and video rendering —
          runs on your device, and this browser is missing the recording APIs
          it needs. Chrome, Edge, or Firefox on desktop will do it. The guided
          demo works everywhere.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Button href="/demo">Watch the live demo</Button>
          <Button href="/" variant="ghost">
            Back home
          </Button>
        </div>
      </Card>
    </div>
  );
}

function ErrorView({
  message,
  canRetry,
  onRetry,
  onPickAnother,
}: {
  message: string;
  canRetry: boolean;
  onRetry: () => void;
  onPickAnother: () => void;
}) {
  return (
    <div className="mx-auto w-full max-w-xl text-center">
      <div className="rounded-2xl border border-ember-500/30 bg-ember-500/[0.04] p-8 sm:p-12">
        <span
          className="mx-auto flex h-12 w-12 items-center justify-center rounded-full border border-ember-500/30 bg-ember-500/10"
          aria-hidden
        >
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-ember-400"
          >
            <path d="M12 3 2.5 19.5h19L12 3Z" />
            <path d="M12 10v4m0 3.5v.01" />
          </svg>
        </span>
        <h1
          tabIndex={-1}
          className="mt-5 font-display text-2xl font-semibold tracking-tight text-white outline-none"
        >
          That didn&apos;t work
        </h1>
        <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-zinc-300" role="alert">
          {message}
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          {canRetry ? <Button onClick={onRetry}>Try again</Button> : null}
          <Button variant="secondary" onClick={onPickAnother}>
            Pick another video
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function StudioApp() {
  const { state, run, reset } = useStudioPipeline();

  // Lifted settings survive reset() — the user's choices stick between runs.
  const [settings, setSettings] = useState<StudioUISettings>(DEFAULT_SETTINGS);
  const [file, setFile] = useState<File | null>(null);
  const [fileDuration, setFileDuration] = useState<number | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [probing, setProbing] = useState(false);
  const [supported, setSupported] = useState(true);
  const [isSafari, setIsSafari] = useState(false);

  // Feature-detect instead of crashing mid-pipeline on Safari/mobile gaps.
  useEffect(() => {
    const ok =
      typeof window !== "undefined" &&
      typeof window.MediaRecorder !== "undefined" &&
      typeof window.AudioContext !== "undefined" &&
      typeof window.Worker !== "undefined" &&
      typeof window.OfflineAudioContext !== "undefined" &&
      typeof HTMLCanvasElement !== "undefined" &&
      "captureStream" in HTMLCanvasElement.prototype;
    setSupported(ok);

    // Safari passes the probes but can still refuse the final render step —
    // advise, don't block.
    const ua = navigator.userAgent.toLowerCase();
    setIsSafari(
      ua.includes("safari") &&
        !ua.includes("chrome") &&
        !ua.includes("chromium") &&
        !ua.includes("edg")
    );
  }, []);

  const handleFileSelect = useCallback(async (picked: File) => {
    setFileError(null);
    setFile(null);
    setFileDuration(null);

    const looksLikeVideo =
      picked.type.startsWith("video/") ||
      /\.(mp4|mov|webm|m4v)$/i.test(picked.name);
    if (!looksLikeVideo) {
      setFileError("That doesn't look like a video file — try an MP4, MOV, or WebM.");
      return;
    }

    // The whole file has to fit in browser memory — no streaming in the beta.
    if (picked.size > MAX_SOURCE_BYTES) {
      setFileError(
        `That file is ${formatBytes(picked.size)} — the browser has to hold the whole video in memory, so the beta caps out at 1.4 GB. Export a lower-bitrate version and try again.`
      );
      return;
    }

    setProbing(true);
    try {
      const duration = await probeVideoDuration(picked);
      if (duration !== null && duration > MAX_SOURCE_SECONDS) {
        setFileError(
          `That video runs ${formatDuration(duration)} — the browser beta caps out at 20:00. Trim it down and try again.`
        );
        return;
      }
      if (duration !== null && duration < 5) {
        setFileError(
          "That video is too short to clip — give it at least a few seconds of speech."
        );
        return;
      }
      setFile(picked);
      setFileDuration(duration);
    } catch {
      setFileError("Couldn't read that file as a video. Try an MP4, MOV, or WebM.");
    } finally {
      setProbing(false);
    }
  }, []);

  const handleClearFile = useCallback(() => {
    setFile(null);
    setFileDuration(null);
    setFileError(null);
  }, []);

  const handleGenerate = useCallback(() => {
    if (!file) return;
    // Free tier: watermark always on.
    void run(file, { ...settings, watermark: true });
  }, [file, settings, run]);

  const handleReset = useCallback(() => {
    handleClearFile();
    reset();
  }, [handleClearFile, reset]);

  // A stray drop outside the dropzone must never navigate the tab away from
  // a run; in the idle view it counts as a file pick instead.
  useEffect(() => {
    const onDragOver = (e: DragEvent) => e.preventDefault();
    const onDrop = (e: DragEvent) => {
      const alreadyHandled = e.defaultPrevented; // the dropzone took this one
      e.preventDefault();
      if (alreadyHandled) return;
      if (!supported || state.status !== "idle") return;
      const dropped = e.dataTransfer?.files?.[0];
      if (dropped) void handleFileSelect(dropped);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [supported, state.status, handleFileSelect]);

  // Move focus to the new view's heading on every state switch so screen
  // readers and keyboard users land somewhere meaningful (skip initial mount).
  const viewRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    const heading = viewRef.current?.querySelector("h1");
    if (heading instanceof HTMLElement) heading.focus();
  }, [state.status]);

  return (
    <div ref={viewRef} className="relative flex min-h-dvh flex-col bg-ink-950">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-32 left-1/2 h-96 w-[36rem] -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
      />
      <TopBar />

      <div className="relative flex-1 py-14 sm:py-20">
        <Container>
          {!supported ? (
            <UnsupportedView />
          ) : state.status === "idle" ? (
            <div className="animate-rise">
              <div className="mx-auto mb-10 max-w-2xl text-center">
                <Eyebrow>Studio beta</Eyebrow>
                <h1
                  tabIndex={-1}
                  className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl md:text-[2.75rem] md:leading-[1.1]"
                >
                  Find the clips hiding in your video
                </h1>
                <p className="mt-4 text-base leading-relaxed text-zinc-400 sm:text-lg">
                  Pick a file. On-device AI transcribes it, scores every moment
                  0–100, and cuts 9:16 captioned clips — no upload, no queue.
                </p>
              </div>
              {isSafari ? (
                <p className="mx-auto mb-6 max-w-xl rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-2.5 text-center text-xs leading-relaxed text-ember-300">
                  Studio works best in Chrome, Edge, or Firefox — Safari may
                  block the final render step.
                </p>
              ) : null}
              <StudioDropzone
                file={file}
                fileDuration={fileDuration}
                probing={probing}
                fileError={fileError}
                settings={settings}
                onSettingsChange={setSettings}
                onFileSelect={handleFileSelect}
                onClearFile={handleClearFile}
                onGenerate={handleGenerate}
              />
            </div>
          ) : state.status === "running" ? (
            <div className="animate-rise">
              <StageChecklist progress={state.progress} fileName={file?.name} />
            </div>
          ) : state.status === "done" ? (
            <div className="animate-rise">
              <div className="mx-auto flex max-w-4xl flex-col items-center gap-4 text-center sm:flex-row sm:items-end sm:justify-between sm:text-left">
                <div>
                  <p className="mb-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-signal-400">
                    Done — on your device
                  </p>
                  <h1
                    tabIndex={-1}
                    className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
                  >
                    Your clips are ready
                  </h1>
                  <p className="mt-3 text-sm text-zinc-400 sm:text-base">
                    <span className="font-mono">{state.clips.length}</span>{" "}
                    {state.clips.length === 1 ? "clip" : "clips"}, scored and
                    captioned. Preview, then download.
                  </p>
                </div>
                <Button variant="secondary" onClick={handleReset}>
                  Clip another video
                </Button>
              </div>

              {state.failedCount > 0 ? (
                <p className="mx-auto mt-8 max-w-4xl rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-3 text-center text-sm text-ember-300">
                  <span className="font-mono">{state.failedCount}</span>{" "}
                  {state.failedCount === 1 ? "clip" : "clips"} didn&apos;t
                  finish rendering — here&apos;s what did.
                </p>
              ) : null}

              <div
                className={`mx-auto mt-10 grid max-w-4xl gap-6 ${
                  state.clips.length > 1 ? "md:grid-cols-2" : "md:max-w-md"
                } ${state.clips.length > 2 ? "xl:max-w-6xl xl:grid-cols-3" : ""}`}
              >
                {state.clips.map((clip, i) => (
                  <ClipCard key={clip.id} clip={clip} index={i} />
                ))}
              </div>

              <p className="mt-10 text-center font-mono text-xs text-zinc-500">
                Exports include the beta watermark · MP4 vs WebM depends on your
                browser
              </p>
            </div>
          ) : (
            <div className="animate-rise">
              <ErrorView
                message={friendlyErrorMessage(state.message)}
                canRetry={file !== null}
                onRetry={handleGenerate}
                onPickAnother={handleReset}
              />
            </div>
          )}
        </Container>
      </div>
    </div>
  );
}
