"use client";

// ClipCatalyst Studio — the real in-browser clipping pipeline.
// All pipeline state comes from useStudioPipeline; this component owns the
// file pick + settings (lifted so they survive reset) and the six views:
// idle / running / done / error / unsupported-browser / phone-or-tablet.
// Which one is reachable at all is decided up front by ./capabilities.ts.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Button, Card, Container, Eyebrow, Logo } from "@/components/ui";
import { useAccount } from "@/components/account/AccountProvider";
import { useStudioPipeline } from "./useStudioPipeline";
import StudioDropzone from "./StudioDropzone";
import StageChecklist from "./StageChecklist";
import ClipCard from "./ClipCard";
import ResultsView from "./ResultsView";
import {
  DEFAULT_SETTINGS,
  formatBytes,
  formatDuration,
  MAX_SOURCE_SECONDS,
  probeVideoDuration,
  type StudioUISettings,
} from "./format";
import {
  CLOUD_API,
  cloudEnabled,
  createJob,
  pollJob,
  startJob,
  toFinishedClips,
  uploadSource,
  type CloudFinishedClip,
  type CloudStage,
} from "./cloud";
import {
  ASSUME_READY,
  classifyPlatform,
  readBrowserCapabilities,
  type CapabilityVerdict,
} from "./capabilities";

// ---- Cloud beta (only reachable when NEXT_PUBLIC_CLOUD_API is set) ----

type Engine = "device" | "cloud";

/** Local state machine for a cloud run; "idle" = the device path owns the UI. */
type CloudRunState =
  | { status: "idle" }
  | { status: "uploading"; progress: number }
  | { status: "queued" }
  | { status: "processing"; stage: CloudStage; progress: number; detail: string }
  | { status: "done"; clips: CloudFinishedClip[] }
  | { status: "error"; message: string };

const CLOUD_STAGE_IDS: readonly CloudStage[] = [
  "probe",
  "transcribe",
  "diarize",
  "analyze",
  "reframe",
  "render",
];

type CloudChecklistStage = "upload" | "queued" | CloudStage;

const CLOUD_CHECKLIST: { id: CloudChecklistStage; label: string }[] = [
  { id: "upload", label: "Upload video" },
  { id: "queued", label: "Queue" },
  { id: "probe", label: "Probe video" },
  { id: "transcribe", label: "Transcribe" },
  { id: "diarize", label: "Identify speakers" },
  { id: "analyze", label: "Score moments" },
  { id: "reframe", label: "Reframe on the speaker" },
  { id: "render", label: "Render clips" },
];

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

/**
 * Desktop browser missing one of the five pipeline APIs. Phones never land
 * here — "this browser is missing recording APIs" is a true sentence and the
 * wrong answer on a phone, where the real reason is the device (see
 * MobileUnsupportedView).
 */
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

/**
 * Phone or tablet with no cloud engine configured. Shown BEFORE any file is
 * picked, because the on-device run cannot succeed here and finding that out
 * after the upload — via a decode error that blames the video — is what this
 * whole change exists to stop.
 *
 * The reason given is the true one for the device: on iOS every browser is
 * WebKit, which will not hand a video file's audio to the page; elsewhere the
 * pipeline simply needs more memory than a phone tab is given. It never says
 * "your browser is missing recording APIs".
 */
function MobileUnsupportedView({ verdict }: { verdict: CapabilityVerdict }) {
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
            className="text-brand-400"
          >
            <rect x="6" y="2.5" width="12" height="19" rx="2.5" />
            <path d="M11 18.5h2" />
          </svg>
        </span>
        <h1
          tabIndex={-1}
          className="mt-6 font-display text-2xl font-semibold tracking-tight text-white outline-none sm:text-3xl"
        >
          Clipping runs on a desktop, not a phone
        </h1>
        <p className="mx-auto mt-4 max-w-md text-sm leading-relaxed text-zinc-400">
          {verdict.isIos ? (
            <>
              Studio does the whole job inside this tab — Whisper
              transcription, moment scoring, and video rendering. On iPhone and
              iPad every browser runs on Apple&apos;s WebKit engine, which
              won&apos;t hand a video file&apos;s audio track to the page, and
              the render step needs more memory than iOS gives a tab.
            </>
          ) : (
            <>
              Studio does the whole job inside this tab — Whisper
              transcription, moment scoring, and video rendering. That is more
              memory and CPU than a phone browser will give one tab, so a run
              started here would stall or die partway through.
            </>
          )}
        </p>
        <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-zinc-400">
          Nothing is wrong with your video. Open this page in Chrome, Edge, or
          Firefox on a computer and it runs end to end. Cloud clipping — upload
          once, get clips back on any device — is coming.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Button href="/demo">Watch the product tour</Button>
          <Button href="/#pricing" variant="secondary">
            See the plans
          </Button>
          <Button href="/" variant="ghost">
            Back home
          </Button>
        </div>
        <p className="mt-8 font-mono text-xs text-zinc-600">
          Detected: {verdict.device} · {verdict.engine} engine
        </p>
      </Card>
    </div>
  );
}

function ErrorView({
  message,
  canRetry,
  onRetry,
  onPickAnother,
  showMobileHelp = false,
}: {
  message: string;
  canRetry: boolean;
  onRetry: () => void;
  onPickAnother: () => void;
  /** On a phone, "try again" is often not a real option — offer somewhere to
   *  go instead of a retry loop. */
  showMobileHelp?: boolean;
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
        {showMobileHelp ? (
          <p className="mt-6 text-xs leading-relaxed text-zinc-500">
            On a phone, the on-device engine can&apos;t finish this.{" "}
            <Link href="/demo" className="text-brand-300 underline-offset-4 hover:underline">
              Watch the product tour
            </Link>{" "}
            — or open Studio on a desktop, where it runs end to end.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function EngineToggle({
  engine,
  onChange,
  mobile,
}: {
  engine: Engine;
  onChange: (engine: Engine) => void;
  /** On a phone/tablet the on-device chip stays selectable but is labelled
   *  for what it is: the desktop engine. Cloud is preselected. */
  mobile: boolean;
}) {
  const chips: { id: Engine; label: string }[] = [
    { id: "device", label: mobile ? "On-device (desktop)" : "On-device" },
    { id: "cloud", label: "Cloud beta" },
  ];
  return (
    <div className="mx-auto mt-6 max-w-xl text-center">
      <div
        role="radiogroup"
        aria-label="Processing engine"
        className="inline-flex items-center gap-1 rounded-full border border-line bg-ink-900 p-1"
      >
        {chips.map((chip) => {
          const selected = engine === chip.id;
          return (
            <button
              key={chip.id}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(chip.id)}
              className={`rounded-full px-4 py-1.5 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
                selected
                  ? "bg-brand-500/15 text-brand-300"
                  : "text-zinc-400 hover:text-white"
              }`}
            >
              {chip.label}
            </button>
          );
        })}
      </div>
      <p className="mt-2.5 text-xs leading-relaxed text-zinc-500">
        {engine === "cloud" ? (
          <>
            Cloud uploads your video to your ClipCatalyst server — minutes-long
            jobs finish in <span className="font-mono">~2 min</span>.
          </>
        ) : mobile ? (
          <>
            On-device runs the whole pipeline in this tab — it needs Chrome,
            Edge, or Firefox on a desktop. On a phone it usually stops while
            reading the video&apos;s audio.
          </>
        ) : (
          <>Everything runs in this browser — no upload, no queue.</>
        )}
      </p>
    </div>
  );
}

function CloudProgressBar({ progress }: { progress: number }) {
  if (progress < 0) {
    // Indeterminate: sweeping shimmer built on the marquee keyframes.
    return (
      <div
        className="h-1 w-full overflow-hidden rounded-full bg-white/10"
        role="progressbar"
        aria-label="Working"
      >
        <div className="flex h-full w-[200%] animate-marquee [animation-duration:1.4s]">
          <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-500 to-transparent" />
          <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-500 to-transparent" />
        </div>
      </div>
    );
  }
  const pct = Math.round(Math.min(Math.max(progress, 0), 1) * 100);
  return (
    <div
      className="h-1 w-full overflow-hidden rounded-full bg-white/10"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
    >
      <div
        className="h-full rounded-full bg-gradient-to-r from-brand-500 to-spark-400 transition-[width] duration-300 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function CloudCheckIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m5 13 4 4L19 7" />
    </svg>
  );
}

/** Running state for cloud jobs — mirrors StageChecklist with server stages. */
function CloudChecklist({
  state,
  fileName,
  onCancel,
}: {
  state: Extract<
    CloudRunState,
    { status: "uploading" | "queued" | "processing" }
  >;
  fileName?: string;
  onCancel: () => void;
}) {
  const current: { id: CloudChecklistStage; progress: number; detail: string } =
    state.status === "uploading"
      ? {
          id: "upload",
          progress: state.progress,
          detail: "Sending your video to the server",
        }
      : state.status === "queued"
        ? { id: "queued", progress: -1, detail: "Waiting for a worker" }
        : { id: state.stage, progress: state.progress, detail: state.detail };
  const currentIndex = CLOUD_CHECKLIST.findIndex((s) => s.id === current.id);
  const pct =
    current.progress >= 0
      ? Math.round(Math.min(Math.max(current.progress, 0), 1) * 100)
      : null;
  const announcement =
    CLOUD_CHECKLIST[currentIndex >= 0 ? currentIndex : 0].label;

  return (
    <div className="mx-auto w-full max-w-xl">
      {/* Announce stage transitions only — never per-poll progress. */}
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
      <div className="text-center">
        <h1
          tabIndex={-1}
          className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
        >
          Finding your clips
        </h1>
        {fileName ? (
          <p className="mt-3 truncate font-mono text-xs text-zinc-500" title={fileName}>
            {fileName}
          </p>
        ) : null}
      </div>

      <Card className="mt-8 p-6 sm:p-8">
        <ol className="flex flex-col gap-6">
          {CLOUD_CHECKLIST.map((stage, i) => {
            const done = i < currentIndex;
            const active = i === currentIndex;
            return (
              <li key={stage.id} className="flex items-start gap-4">
                <span
                  className={`mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-full border font-mono text-xs transition-colors duration-300 ${
                    done
                      ? "border-signal-500/40 bg-signal-500/10 text-signal-400"
                      : active
                        ? "border-brand-400/60 bg-brand-500/15 text-brand-300"
                        : "border-line text-zinc-600"
                  }`}
                  aria-hidden
                >
                  {done ? (
                    <CloudCheckIcon />
                  ) : active ? (
                    <span className="h-2 w-2 animate-pulse-soft rounded-full bg-brand-400" />
                  ) : (
                    `0${i + 1}`
                  )}
                </span>

                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex items-baseline justify-between gap-3">
                    <p
                      className={`text-sm font-medium ${
                        done
                          ? "text-zinc-400"
                          : active
                            ? "text-white"
                            : "text-zinc-600"
                      }`}
                    >
                      {stage.label}
                      {done ? <span className="sr-only"> — done</span> : null}
                    </p>
                    {active ? (
                      <p className="shrink-0 font-mono text-xs text-zinc-400">
                        {pct !== null ? `${pct}%` : "…"}
                      </p>
                    ) : null}
                  </div>

                  {active ? (
                    <div className="mt-2.5 flex flex-col gap-2">
                      <CloudProgressBar progress={current.progress} />
                      {current.detail ? (
                        <p className="truncate font-mono text-xs text-zinc-500">
                          {current.detail}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      </Card>

      <div className="mt-6 flex flex-col items-center gap-2">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <p className="text-xs text-zinc-600">
          Canceling stops watching the job — nothing is kept on the server
          beyond your upload.
        </p>
      </div>
    </div>
  );
}

export default function StudioApp() {
  const { state, run, reset } = useStudioPipeline();
  const { user, loading: accountLoading } = useAccount();

  // Entitlements are the server's (/v1/me reports the EFFECTIVE plan, so a
  // canceled subscription is already free). Signed out, still loading, or on
  // Free → watermark, exactly today's behaviour. Device renders run on the
  // user's own hardware, so this is soft client-side enforcement — the
  // honest boundary is documented in STUDIO.md; cloud renders are decided
  // server-side from the same entitlements.
  const watermarkRequired =
    accountLoading || user === null || user.entitlements.watermark_required;

  // Lifted settings survive reset() — the user's choices stick between runs.
  const [settings, setSettings] = useState<StudioUISettings>(DEFAULT_SETTINGS);
  const [file, setFile] = useState<File | null>(null);
  const [fileDuration, setFileDuration] = useState<number | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [probing, setProbing] = useState(false);
  // Starts at the desktop/ready placeholder so the server-rendered markup and
  // the first client paint are identical to what shipped before the gate
  // existed; the real verdict lands in the effect below.
  const [capabilities, setCapabilities] =
    useState<CapabilityVerdict>(ASSUME_READY);

  // Cloud beta: engine choice + the cloud run's own state machine. With
  // NEXT_PUBLIC_CLOUD_API unset, cloudEnabled is false, the toggle never
  // renders, and both stay at their initial values — the device path is
  // untouched.
  const [engine, setEngine] = useState<Engine>("device");
  const [cloudState, setCloudState] = useState<CloudRunState>({
    status: "idle",
  });
  const cloudAbortRef = useRef<AbortController | null>(null);
  // Object URLs minted by toFinishedClips for a signed-in user's clips (the
  // files are fetched with the session bearer). We own them — revoke on the
  // next run, on cancel, and on unmount, or the blobs leak.
  const cloudObjectUrlsRef = useRef<string[]>([]);

  const revokeCloudObjectUrls = useCallback(() => {
    for (const url of cloudObjectUrlsRef.current) URL.revokeObjectURL(url);
    cloudObjectUrlsRef.current = [];
  }, []);

  // Leaving the page mid-run: kill the upload and the 1.5 s poll loop.
  useEffect(
    () => () => {
      cloudAbortRef.current?.abort();
      for (const url of cloudObjectUrlsRef.current) URL.revokeObjectURL(url);
    },
    []
  );

  const runCloud = useCallback(
    async (picked: File) => {
      cloudAbortRef.current?.abort();
      revokeCloudObjectUrls();
      const controller = new AbortController();
      cloudAbortRef.current = controller;
      // Every setState checks the signal so an aborted run can't repaint.
      const safeSet = (next: CloudRunState) => {
        if (!controller.signal.aborted) setCloudState(next);
      };

      try {
        safeSet({ status: "uploading", progress: 0 });
        const created = await createJob(CLOUD_API, {
          filename: picked.name,
          size_bytes: picked.size,
          target_length: settings.targetLength,
          count: settings.count,
          height: settings.height,
        });
        await uploadSource(
          CLOUD_API,
          created.job_id,
          picked,
          (p) => safeSet({ status: "uploading", progress: p }),
          { uploadUrl: created.upload.url, signal: controller.signal }
        );
        safeSet({ status: "queued" });
        await startJob(CLOUD_API, created.job_id);
        const job = await pollJob(
          CLOUD_API,
          created.job_id,
          (snapshot) => {
            if (snapshot.status === "processing") {
              const stage = CLOUD_STAGE_IDS.includes(
                snapshot.stage as CloudStage
              )
                ? (snapshot.stage as CloudStage)
                : "probe";
              safeSet({
                status: "processing",
                stage,
                progress: snapshot.progress,
                detail: snapshot.detail,
              });
            }
          },
          controller.signal
        );
        if (job.status === "failed") {
          throw new Error(
            job.error || "The cloud job failed. Try again in a minute."
          );
        }
        const finished = await toFinishedClips(
          CLOUD_API,
          job.clips,
          controller.signal
        );
        if (controller.signal.aborted) {
          for (const url of finished.objectUrls) URL.revokeObjectURL(url);
          return;
        }
        cloudObjectUrlsRef.current = finished.objectUrls;
        safeSet({ status: "done", clips: finished.clips });
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e instanceof DOMException && e.name === "AbortError") return;
        safeSet({
          status: "error",
          message:
            e instanceof Error
              ? e.message
              : "Something went wrong while processing in the cloud.",
        });
      }
    },
    [settings, revokeCloudObjectUrls]
  );

  const handleCloudCancel = useCallback(() => {
    cloudAbortRef.current?.abort();
    cloudAbortRef.current = null;
    revokeCloudObjectUrls();
    setCloudState({ status: "idle" });
  }, [revokeCloudObjectUrls]);

  // Classify the platform once, instead of crashing mid-pipeline on a device
  // that was never going to finish. The API probes alone all pass on a modern
  // iPhone, which is exactly how phones used to reach the decode error — see
  // ./capabilities.ts for the reasoning behind each verdict.
  useEffect(() => {
    const verdict = classifyPlatform(readBrowserCapabilities(cloudEnabled));
    setCapabilities(verdict);
    // On a phone with a cloud engine configured, cloud is the path that
    // actually works — preselect it before the user picks a file. The
    // on-device chip stays there for anyone who wants to try anyway.
    if (verdict.gate === "cloud-only") setEngine("cloud");
  }, []);

  // Views that accept a file at all: desktop-ready, or mobile-with-cloud.
  const acceptsFiles =
    capabilities.gate === "ready" || capabilities.gate === "cloud-only";

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
    if (cloudEnabled && engine === "cloud") {
      void runCloud(file);
      return;
    }
    void run(file, { ...settings, watermark: watermarkRequired });
  }, [file, settings, run, engine, runCloud, watermarkRequired]);

  const handleReset = useCallback(() => {
    handleCloudCancel();
    handleClearFile();
    reset();
  }, [handleCloudCancel, handleClearFile, reset]);

  // A stray drop outside the dropzone must never navigate the tab away from
  // a run; in the idle view it counts as a file pick instead.
  useEffect(() => {
    const onDragOver = (e: DragEvent) => e.preventDefault();
    const onDrop = (e: DragEvent) => {
      const alreadyHandled = e.defaultPrevented; // the dropzone took this one
      e.preventDefault();
      if (alreadyHandled) return;
      if (
        !acceptsFiles ||
        state.status !== "idle" ||
        cloudState.status !== "idle"
      )
        return;
      const dropped = e.dataTransfer?.files?.[0];
      if (dropped) void handleFileSelect(dropped);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [acceptsFiles, state.status, cloudState.status, handleFileSelect]);

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
  }, [state.status, cloudState.status]);

  return (
    <div ref={viewRef} className="relative flex min-h-dvh flex-col bg-ink-950">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-32 left-1/2 h-96 w-[36rem] -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
      />
      <TopBar />

      <div className="relative flex-1 py-14 sm:py-20">
        <Container>
          {capabilities.gate === "missing-apis" ? (
            <UnsupportedView />
          ) : capabilities.gate === "mobile-blocked" ? (
            <MobileUnsupportedView verdict={capabilities} />
          ) : cloudState.status === "uploading" ||
            cloudState.status === "queued" ||
            cloudState.status === "processing" ? (
            <div className="animate-rise">
              <CloudChecklist
                state={cloudState}
                fileName={file?.name}
                onCancel={handleCloudCancel}
              />
            </div>
          ) : cloudState.status === "done" ? (
            <div className="animate-rise">
              <div className="mx-auto flex max-w-4xl flex-col items-center gap-4 text-center sm:flex-row sm:items-end sm:justify-between sm:text-left">
                <div>
                  <p className="mb-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-signal-400">
                    Done — in the cloud
                  </p>
                  <h1
                    tabIndex={-1}
                    className="font-display text-3xl font-semibold tracking-tight text-white outline-none sm:text-4xl"
                  >
                    Your clips are ready
                  </h1>
                  <p className="mt-3 text-sm text-zinc-400 sm:text-base">
                    <span className="font-mono">{cloudState.clips.length}</span>{" "}
                    {cloudState.clips.length === 1 ? "clip" : "clips"}, scored
                    and captioned. Preview, then download.
                  </p>
                </div>
                <Button variant="secondary" onClick={handleReset}>
                  Clip another video
                </Button>
              </div>

              <div
                className={`mx-auto mt-10 grid max-w-4xl gap-6 ${
                  cloudState.clips.length > 1 ? "md:grid-cols-2" : "md:max-w-md"
                } ${
                  cloudState.clips.length > 2 ? "xl:max-w-6xl xl:grid-cols-3" : ""
                }`}
              >
                {cloudState.clips.map((clip, i) => (
                  <ClipCard key={clip.id} clip={clip} index={i} />
                ))}
              </div>

              <p className="mt-10 text-center font-mono text-xs text-zinc-500">
                Rendered on your ClipCatalyst server · MP4
                {watermarkRequired ? " · includes the beta watermark" : ""}
              </p>
            </div>
          ) : cloudState.status === "error" ? (
            <div className="animate-rise">
              <ErrorView
                message={cloudState.message}
                canRetry={file !== null}
                onRetry={handleGenerate}
                onPickAnother={handleReset}
              />
            </div>
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
              {capabilities.isDesktopSafari ? (
                <p className="mx-auto mb-6 max-w-xl rounded-xl border border-ember-500/30 bg-ember-500/[0.06] px-4 py-2.5 text-center text-xs leading-relaxed text-ember-300">
                  Studio works best in Chrome, Edge, or Firefox — Safari may
                  block the final render step.
                </p>
              ) : null}
              {capabilities.gate === "cloud-only" ? (
                <p className="mx-auto mb-6 max-w-xl rounded-xl border border-brand-500/30 bg-brand-500/[0.06] px-4 py-2.5 text-center text-xs leading-relaxed text-brand-200">
                  You&apos;re on a phone, so Studio is set to the cloud engine —
                  your video uploads, the server does the work, and the clips
                  come back here. The on-device engine is desktop-only.
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
              {cloudEnabled ? (
                <EngineToggle
                  engine={engine}
                  onChange={setEngine}
                  mobile={capabilities.isMobile}
                />
              ) : null}
            </div>
          ) : state.status === "running" ? (
            <div className="animate-rise">
              <StageChecklist progress={state.progress} fileName={file?.name} />
            </div>
          ) : state.status === "done" ? (
            <div className="animate-rise">
              <ResultsView
                clips={state.clips}
                sourceUrl={state.sourceUrl}
                sourceDuration={state.sourceDuration}
                transcript={state.transcript}
                renderOptions={state.renderOptions}
                failedCount={state.failedCount}
                onReset={handleReset}
              />
            </div>
          ) : (
            <div className="animate-rise">
              {/* Device-pipeline failure. On a phone this is very likely the
                  platform, not the file — offer a way out, not a retry loop.
                  (The cloud ErrorView above stays as-is: a cloud failure is
                  the server's, and retrying there is a real option.) */}
              <ErrorView
                message={friendlyErrorMessage(state.message)}
                canRetry={file !== null}
                onRetry={handleGenerate}
                onPickAnother={handleReset}
                showMobileHelp={capabilities.isMobile}
              />
            </div>
          )}
        </Container>
      </div>
    </div>
  );
}
