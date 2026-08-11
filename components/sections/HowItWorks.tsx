import { Card, Container, GradientText, SectionHeading } from "@/components/ui";

type Step = {
  num: string;
  name: string;
  desc: string;
  /** Step dot on the lg connector line. */
  dot: string;
  optional?: boolean;
  progressMock?: boolean;
};

const STEPS: Step[] = [
  {
    num: "01",
    name: "Add a video",
    desc: "Drop in an MP4, MOV, or WebM — up to 20 minutes and 1.4 GB. It stays on your machine; nothing is uploaded.",
    dot: "bg-brand-400 shadow-[0_0_12px_rgba(167,139,250,0.7)]",
  },
  {
    num: "02",
    name: "Configure",
    desc: "Pick clip length — 15, 30, or 60s — how many clips (1–3), and export quality up to 1080p. Or skip straight past it.",
    dot: "bg-brand-400 shadow-[0_0_12px_rgba(167,139,250,0.7)]",
    optional: true,
  },
  {
    num: "03",
    name: "Studio processes",
    desc: "Transcribes, separates speakers, scores every candidate moment, reframes to 9:16, and renders — all in this tab.",
    dot: "bg-spark-400 shadow-[0_0_12px_rgba(232,121,249,0.7)]",
    progressMock: true,
  },
  {
    num: "04",
    name: "Review & edit",
    desc: "Check each 0–100 score and the one fix it suggests, then type the change: pauses, pace, zooms, trims, hooks.",
    dot: "bg-ember-400 shadow-[0_0_12px_rgba(251,191,36,0.7)]",
  },
  {
    num: "05",
    name: "Download",
    desc: "Save the MP4 at up to 1080p, or hand it to your phone's native share sheet and post from there.",
    dot: "bg-signal-400 shadow-[0_0_12px_rgba(52,211,153,0.7)]",
  },
];

export default function HowItWorks() {
  return (
    <section id="how-it-works" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          eyebrow="From VOD to viral"
          title={
            <>
              Dropped file to <GradientText>finished clip</GradientText>
            </>
          }
          lede="Five steps from a video on your desk to a captioned vertical clip in your downloads. One of them is optional, and every one of them runs inside your browser."
        />

        <div className="relative mt-16">
          {/* Connector line across the five step dots (lg only) */}
          <div
            aria-hidden
            className="absolute left-[calc(10%-4.8px)] right-[calc(10%-4.8px)] top-[3.5px] hidden h-px bg-[linear-gradient(90deg,rgba(167,139,250,0.5),rgba(232,121,249,0.45)_45%,rgba(251,191,36,0.4)_72%,rgba(52,211,153,0.5))] lg:block"
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
                  <span
                    className={`absolute left-1/2 top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full ${step.dot}`}
                  />
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
                          <span className="truncate">clip 2/3</span>
                          <span className="shrink-0 text-brand-300">
                            rendering
                          </span>
                        </div>
                        <div
                          className="mt-2 h-1 overflow-hidden rounded-full bg-white/10"
                          role="img"
                          aria-label="Processing progress: 62 percent, rendering clip 2 of 3"
                        >
                          <div className="h-full w-[62%] animate-pulse-soft rounded-full bg-gradient-to-r from-brand-500 to-spark-500" />
                        </div>
                      </div>
                    </div>
                  ) : null}
                </Card>
              </li>
            ))}
          </ol>

          <p className="mt-12 text-center font-mono text-xs text-zinc-500">
            all five steps run in your browser{" "}
            <span className="text-zinc-700">·</span>{" "}
            <span className="text-signal-400">nothing uploaded</span>
          </p>
        </div>
      </Container>
    </section>
  );
}
