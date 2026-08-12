import { Card, Container, GradientText, SectionHeading } from "@/components/ui";

type Step = {
  num: string;
  name: string;
  desc: string;
  optional?: boolean;
  progressMock?: boolean;
};

const STEPS: Step[] = [
  {
    num: "01",
    name: "Upload",
    desc: "Paste a YouTube link, drop a file up to 10GB (MP4, MOV, AVI, WebM), or connect Drive or Dropbox.",
  },
  {
    num: "02",
    name: "Configure",
    desc: "Pick platforms, clip length (15, 30, or 60s), your brand kit, and the vibe. Or skip straight past it.",
    optional: true,
  },
  {
    num: "03",
    name: "AI processes",
    desc: "The engine finds, cuts, captions, and scores every moment. Preview clips live as they finish.",
    progressMock: true,
  },
  {
    num: "04",
    name: "Review & edit",
    desc: "Check each virality score out of 100, trim in the built-in editor, or just type the change in chat.",
  },
  {
    num: "05",
    name: "Publish",
    desc: "Download up to 4K, one-click post to TikTok, Shorts, and Reels, schedule, or share a review link.",
  },
];

export default function HowItWorks() {
  return (
    <section id="how-it-works" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          title={
            <>
              Upload to <GradientText>posted</GradientText> in five steps
            </>
          }
          lede="Five steps from raw footage to a clip on your feed. One is optional, and the engine handles the heavy one in about 90 seconds."
        />

        <div className="relative mt-16">
          {/* Connector line across the five step dots (lg only) */}
          <div
            aria-hidden
            className="absolute left-[calc(10%-4.8px)] right-[calc(10%-4.8px)] top-[3.5px] hidden h-px bg-brand-500/40 lg:block"
          />

          <ol className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5 lg:gap-3">
            {STEPS.map((step, i) => (
              <li
                key={step.num}
                className={`flex flex-col ${
                  i === STEPS.length - 1 ? "sm:col-span-2 lg:col-span-1" : ""
                }`}
              >
                {/* Step dot on the connector line (lg only) */}
                <div aria-hidden className="relative mb-6 hidden h-2 lg:block">
                  <span className="absolute left-1/2 top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand-400" />
                </div>

                <Card className="flex flex-1 flex-col p-5">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-mono text-3xl font-semibold leading-none tracking-tight text-zinc-600">
                      {step.num}
                    </span>
                    {step.optional ? (
                      <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-zinc-500">
                        Optional
                      </span>
                    ) : null}
                  </div>

                  <h3 className="mt-5 font-display text-lg font-semibold tracking-tight text-white">
                    {step.name}
                  </h3>
                  <p className="mt-2 text-sm leading-relaxed text-zinc-400">
                    {step.desc}
                  </p>

                  {step.progressMock ? (
                    <div className="mt-auto pt-4">
                      <div className="rounded-lg border border-line bg-ink-900/80 p-2.5">
                        <div className="flex items-center justify-between gap-2 font-mono text-[10px] text-zinc-500">
                          <span className="truncate">clip 5/8</span>
                          <span className="shrink-0 text-brand-300">
                            ~90s remaining
                          </span>
                        </div>
                        <div
                          className="mt-2 h-1 overflow-hidden rounded-full bg-white/10"
                          role="img"
                          aria-label="Processing progress: 62 percent, about 90 seconds remaining"
                        >
                          <div className="h-full w-[62%] animate-pulse-soft rounded-full bg-brand-500" />
                        </div>
                      </div>
                    </div>
                  ) : null}
                </Card>
              </li>
            ))}
          </ol>

          <p className="mt-12 text-center font-mono text-xs text-zinc-500">
            median time from upload to first clip{" "}
            <span className="text-zinc-700">·</span>{" "}
            <span className="text-signal-400">&lt;90s</span>
          </p>
        </div>
      </Container>
    </section>
  );
}
