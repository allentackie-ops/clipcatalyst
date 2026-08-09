import { Badge, Button, Container, GradientText, ScoreRing } from "@/components/ui";

const STATS = [
  { value: "<90s", label: "median time" },
  { value: "4K", label: "max export" },
  { value: "0–100", label: "virality score" },
  { value: "10GB", label: "max upload" },
];

const WAVEFORM = [
  "h-1.5",
  "h-3",
  "h-2",
  "h-4",
  "h-2.5",
  "h-5",
  "h-3",
  "h-4",
  "h-2",
  "h-3.5",
  "h-1.5",
  "h-2.5",
  "h-3",
  "h-1.5",
];

export default function Hero() {
  return (
    <section
      id="hero"
      className="relative overflow-hidden pt-32 pb-20 md:pb-28 lg:flex lg:min-h-[90vh] lg:items-center lg:pt-36 lg:pb-24"
    >
      {/* single decorative glow */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 right-[-8rem] h-[28rem] w-[28rem] rounded-full bg-brand-600/20 blur-[130px] lg:right-[2%] lg:top-16"
      />

      <Container>
        <div className="grid items-center gap-16 lg:grid-cols-[1.05fr_0.95fr] lg:gap-10">
          {/* ---- Copy column ---- */}
          <div className="relative">
            <Badge tone="brand">
              <span
                aria-hidden
                className="h-1.5 w-1.5 rounded-full bg-signal-400 animate-pulse-soft"
              />
              Early access &mdash; waitlist open
            </Badge>

            <h1 className="mt-6 font-display text-4xl font-semibold tracking-tight text-white sm:text-5xl lg:text-6xl lg:leading-[1.05]">
              <span className="block">Studio-quality clips</span>
              <span className="block">
                in <GradientText>90 seconds</GradientText>.
              </span>
            </h1>

            <p className="mt-6 max-w-xl text-base leading-relaxed text-zinc-400 sm:text-lg">
              Paste a link. The AI finds your viral moments, reframes them to
              9:16, captions every word, and scores each clip 0&ndash;100. Stop
              editing &mdash; start posting.
            </p>

            <div className="mt-9 flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
              <Button href="#waitlist" size="lg">
                Get early access
              </Button>
              <Button href="/demo" variant="secondary" size="lg">
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  aria-hidden
                  className="text-brand-400"
                >
                  <path d="M8 5.5v13l11-6.5-11-6.5Z" />
                </svg>
                Try the live demo
              </Button>
            </div>

            {/* Stat strip */}
            <div className="mt-12 grid max-w-xl grid-cols-2 gap-x-6 gap-y-8 border-t border-line pt-8 sm:grid-cols-4">
              {STATS.map((stat) => (
                <div key={stat.label}>
                  <p className="font-mono text-xl font-semibold text-white">
                    {stat.value}
                  </p>
                  <p className="mt-1.5 text-[11px] font-medium uppercase tracking-[0.15em] text-zinc-500">
                    {stat.label}
                  </p>
                </div>
              ))}
            </div>
          </div>

          {/* ---- Visual column: fanned clip mockups ---- */}
          <div className="relative mx-auto flex w-full max-w-[24rem] items-center justify-center py-6 lg:max-w-[26rem]">
            {/* back-left card */}
            <div
              aria-hidden
              className="absolute left-0 top-1/2 z-0 aspect-[9/16] w-32 -translate-y-[58%] -rotate-6 overflow-hidden rounded-2xl border border-line bg-gradient-to-br from-ink-700 via-ink-850 to-spark-500/20 opacity-90 shadow-xl shadow-black/40 sm:w-36 lg:w-40"
            >
              <span className="absolute left-2.5 top-2.5 rounded-md bg-black/50 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
                0:21
              </span>
              <div className="absolute inset-x-3 bottom-4 space-y-1.5">
                <div className="h-2 rounded-full bg-white/15" />
                <div className="mx-auto h-2 w-2/3 rounded-full bg-white/10" />
              </div>
            </div>

            {/* back-right card */}
            <div
              aria-hidden
              className="absolute right-0 top-1/2 z-0 aspect-[9/16] w-32 -translate-y-[42%] rotate-6 overflow-hidden rounded-2xl border border-line bg-gradient-to-br from-ink-700 via-ink-850 to-ember-500/20 opacity-90 shadow-xl shadow-black/40 sm:w-36 lg:w-40"
            >
              <div className="absolute right-2.5 top-2.5 flex flex-col items-end gap-1.5">
                <span className="rounded-md bg-black/50 px-1.5 py-0.5 font-mono text-[10px] text-signal-400">
                  81
                </span>
                <span className="rounded-md bg-black/50 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
                  0:58
                </span>
              </div>
              <div className="absolute inset-x-3 bottom-4 space-y-1.5">
                <div className="mx-auto h-2 w-3/4 rounded-full bg-white/15" />
                <div className="mx-auto h-2 w-1/2 rounded-full bg-white/10" />
              </div>
            </div>

            {/* front card */}
            <div className="relative z-10 aspect-[9/16] w-48 overflow-hidden rounded-2xl border border-line-strong bg-gradient-to-br from-ink-700 via-ink-800 to-brand-700/30 shadow-2xl shadow-black/50 sm:w-56 lg:w-60">
              {/* soft top sheen inside the "video" */}
              <div
                aria-hidden
                className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,rgba(255,255,255,0.07),transparent_55%)]"
              />

              {/* top row: duration + score */}
              <div className="absolute inset-x-3 top-3 flex items-start justify-between">
                <span className="rounded-md bg-black/50 px-1.5 py-0.5 font-mono text-[10px] text-zinc-200">
                  0:37
                </span>
                <ScoreRing
                  score={94}
                  size={44}
                  className="rounded-full bg-ink-950/70"
                />
              </div>

              {/* face-tracking frame */}
              <div
                aria-hidden
                className="absolute left-1/2 top-[26%] h-24 w-20 -translate-x-1/2 rounded-xl border border-brand-400/50"
              >
                {/* blurred subject silhouette */}
                <div className="absolute inset-0 overflow-hidden rounded-xl">
                  <div className="absolute left-1/2 top-10 h-14 w-16 -translate-x-1/2 rounded-[2.5rem] bg-brand-300/20 blur-sm" />
                  <div className="absolute left-1/2 top-2.5 h-7 w-7 -translate-x-1/2 rounded-full bg-brand-300/25 blur-sm" />
                </div>
                <span className="absolute -top-2 left-1/2 -translate-x-1/2 whitespace-nowrap rounded border border-line bg-ink-950/90 px-1.5 font-mono text-[8px] uppercase tracking-[0.15em] text-brand-300">
                  face lock
                </span>
              </div>

              {/* bottom stack: waveform, captions, platforms, scrubber */}
              <div className="absolute inset-x-3 bottom-3 flex flex-col items-center gap-2.5">
                <div
                  aria-hidden
                  className="flex h-5 items-center justify-center gap-[3px]"
                >
                  {WAVEFORM.map((height, i) => (
                    <span
                      key={i}
                      className={`w-[3px] rounded-full bg-white/20 ${height}`}
                    />
                  ))}
                </div>
                <p className="flex flex-col items-center gap-1 text-center font-display text-[13px] font-bold leading-none text-white">
                  <span className="rounded-md bg-black/60 px-2 py-1">
                    we hit a million views
                  </span>
                  <span className="rounded-md bg-black/60 px-2 py-1">
                    with <span className="text-brand-400">zero</span> ad spend
                  </span>
                </p>
                <span className="flex flex-wrap items-center justify-center gap-1.5">
                  <Badge tone="neutral" className="px-2! py-0.5! text-[10px]!">
                    TikTok
                  </Badge>
                  <Badge tone="neutral" className="px-2! py-0.5! text-[10px]!">
                    Shorts
                  </Badge>
                  <Badge tone="neutral" className="px-2! py-0.5! text-[10px]!">
                    Reels
                  </Badge>
                </span>
                <div
                  aria-hidden
                  className="h-1 w-full overflow-hidden rounded-full bg-white/10"
                >
                  <div className="h-full w-[62%] rounded-full bg-gradient-to-r from-brand-400 to-spark-400" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </Container>
    </section>
  );
}
