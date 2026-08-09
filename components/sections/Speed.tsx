import {
  Badge,
  Card,
  Container,
  GradientText,
  SectionHeading,
} from "@/components/ui";

const rows = [
  {
    name: "ClipCatalyst",
    time: "90 sec",
    self: true,
    // 90s of 1080s ≈ 8.3% — min-w keeps the bar visibly non-trivial
    width: "w-[8.33%] min-w-[4.75rem]",
  },
  {
    name: "OpusClip",
    time: "~12 min",
    self: false,
    width: "w-[66.7%]",
  },
  {
    name: "Klap",
    time: "~18 min",
    self: false,
    width: "w-full",
  },
];

const hows = [
  {
    title: "Optimized GPU pipelines",
    copy: "Decode, reframe, and render on tuned GPU workers — zero queue time, zero idle cycles.",
    icon: (
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        aria-hidden
      >
        <rect x="7" y="7" width="10" height="10" rx="2" />
        <path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M19 5l-2 2M5 19l2-2M19 19l-2-2" />
      </svg>
    ),
  },
  {
    title: "Parallel processing",
    copy: "Every clip renders simultaneously. Ten clips finish in roughly the time of one.",
    icon: (
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M3 6h12M3 12h18M3 18h8" />
        <path d="M18 3l3 3-3 3M18 15l3 3-3 3" />
      </svg>
    ),
  },
  {
    title: "Intelligent caching",
    copy: "Faces, cuts, and caption patterns are cached — repeat runs come back even faster.",
    icon: (
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M12 3 4 7l8 4 8-4-8-4Z" />
        <path d="M4 12l8 4 8-4" />
        <path d="M4 17l8 4 8-4" />
      </svg>
    ),
  },
];

export default function Speed() {
  return (
    <section id="speed" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/20 blur-[120px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="The 90-Second Guarantee"
          title={
            <>
              Done in <GradientText>90 seconds</GradientText>. Not 20 minutes.
            </>
          }
          lede="Median time from upload to finished clip: under 90 seconds. The competition is still drawing a progress bar."
        />

        {/* Comparison chart */}
        <Card className="mx-auto mt-14 max-w-4xl p-6 sm:p-10 md:mt-16">
          <div className="mb-8 flex flex-wrap items-center justify-between gap-3">
            <p className="font-mono text-xs uppercase tracking-[0.15em] text-zinc-500">
              Time to finished clips · 60-min podcast
            </p>
            <Badge tone="signal">Up to 12× faster</Badge>
          </div>

          <div className="flex flex-col gap-6 sm:gap-5">
            {rows.map((row) => (
              <div
                key={row.name}
                className="grid grid-cols-2 items-center gap-y-2 sm:grid-cols-[8.5rem_1fr_4.5rem] sm:gap-x-5 sm:gap-y-0"
              >
                <span
                  className={`col-start-1 row-start-1 text-sm font-medium ${
                    row.self ? "text-white" : "text-zinc-400"
                  }`}
                >
                  {row.name}
                </span>
                <span
                  className={`col-start-2 row-start-1 justify-self-end font-mono text-sm sm:col-start-3 ${
                    row.self ? "font-semibold text-white" : "text-zinc-500"
                  }`}
                >
                  {row.time}
                </span>
                <div className="col-span-2 row-start-2 h-9 overflow-hidden rounded-lg bg-white/[0.04] sm:col-span-1 sm:col-start-2 sm:row-start-1">
                  {row.self ? (
                    <div
                      className={`flex h-full items-center justify-end rounded-lg bg-gradient-to-r from-brand-500 to-spark-500 pr-2.5 shadow-[0_0_20px_rgba(139,92,246,0.45)] ${row.width}`}
                    >
                      <svg
                        width="12"
                        height="12"
                        viewBox="0 0 24 24"
                        fill="none"
                        aria-hidden
                      >
                        <path
                          d="M13 2 4.5 13.5H11L9.5 22 19 10h-6.5L13 2Z"
                          fill="white"
                        />
                      </svg>
                    </div>
                  ) : (
                    <div
                      className={`h-full rounded-lg border border-line bg-ink-600/50 ${row.width}`}
                    />
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Minute scale, aligned to the bar column */}
          <div
            aria-hidden
            className="mt-4 hidden sm:grid sm:grid-cols-[8.5rem_1fr_4.5rem] sm:gap-x-5"
          >
            <div className="col-start-2 flex justify-between border-t border-line pt-2 font-mono text-[10px] text-zinc-600">
              <span>0</span>
              <span>6 min</span>
              <span>12 min</span>
              <span>18 min</span>
            </div>
          </div>

          <p className="mt-6 font-mono text-xs text-zinc-600 sm:mt-4">
            Median processing time · 1080p source · 10 clips per batch
          </p>
        </Card>

        {/* How we do it */}
        <div className="mx-auto mt-8 grid max-w-4xl gap-4 sm:grid-cols-3">
          {hows.map((how) => (
            <Card key={how.title} className="p-6">
              <span className="mb-4 flex h-9 w-9 items-center justify-center rounded-lg border border-brand-500/30 bg-brand-500/10 text-brand-300">
                {how.icon}
              </span>
              <h3 className="text-sm font-semibold text-white">{how.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-zinc-400">
                {how.copy}
              </p>
            </Card>
          ))}
        </div>
      </Container>
    </section>
  );
}
