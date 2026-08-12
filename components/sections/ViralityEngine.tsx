import type { ReactNode } from "react";

import {
  Card,
  Container,
  GradientText,
  ScoreRing,
  SectionHeading,
} from "@/components/ui";

const HOOKS: {
  id: string;
  text: string;
  recommended?: boolean;
}[] = [
  { id: "A", text: "Nobody warns you about this part of raising a seed round." },
  {
    id: "B",
    text: "I lost $40K before I learned this one rule.",
    recommended: true,
  },
  { id: "C", text: "The email that saved my company had four words." },
  { id: "D", text: "VCs hate it when founders do this." },
  { id: "E", text: "Month 3 almost killed us. Here's what changed." },
];

const CAPABILITIES: {
  name: string;
  blurb: string;
  icon: ReactNode;
}[] = [
  {
    name: "Platform-specific optimization",
    blurb:
      "Tunes each clip for TikTok, Reels, or Shorts. Three feeds, three trend models, no one-size-fits-all.",
    icon: (
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="m12 2 9 5-9 5-9-5 9-5Z" />
        <path d="m3 12.5 9 5 9-5" />
        <path d="m3 17.5 9 5 9-5" />
      </svg>
    ),
  },
  {
    name: "Hook generation",
    blurb:
      "Writes 5 scroll-stopping hooks per clip, then recommends the strongest, with the retention math to back it.",
    icon: (
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" />
        <path d="M19 15.5l.8 1.7 1.7.8-1.7.8-.8 1.7-.8-1.7-1.7-.8 1.7-.8.8-1.7Z" />
      </svg>
    ),
  },
  {
    name: "Timing optimization",
    blurb:
      "Finds the exact millisecond to cut in and out. Retention lives or dies in the first 400ms.",
    icon: (
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3.5 2" />
      </svg>
    ),
  },
  {
    name: "Audio analysis",
    blurb:
      "Detects the laughter spikes, dramatic pauses, and emotional peaks that feeds reward.",
    icon: (
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M4 10v4" />
        <path d="M8 7v10" />
        <path d="M12 4v16" />
        <path d="M16 7v10" />
        <path d="M20 10v4" />
      </svg>
    ),
  },
];

export default function ViralityEngine() {
  return (
    <section id="virality-engine" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          title={
            <>
              Not <span className="text-zinc-500">a score</span>.{" "}
              <GradientText>An engine</GradientText>.
            </>
          }
          lede="Every clip gets a virality score out of 100. Underneath it, four systems decide what to cut, what to say first, and how to say it. Then they tell you exactly how to push the number higher."
        />

        <div className="mt-16 grid items-stretch gap-10 lg:grid-cols-[1.08fr_1fr] lg:gap-12">
          {/* Engine readout panel */}
          <Card className="overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between gap-4 border-b border-line px-5 py-4 sm:px-6">
              <div className="min-w-0">
                <p className="truncate font-mono text-xs text-zinc-300">
                  seed-round-stories.mp4{" "}
                  <span className="text-zinc-600">·</span>{" "}
                  <span className="text-zinc-500">clip 03</span>
                </p>
                <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-600">
                  Virality readout
                </p>
              </div>
              <ScoreRing score={87} size={56} className="shrink-0" />
            </div>

            {/* Hooks */}
            <div className="px-5 py-5 sm:px-6">
              <div className="flex items-baseline justify-between">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                  Generated hooks
                </p>
                <p className="font-mono text-[10px] text-zinc-600">5 / clip</p>
              </div>
              <ul className="mt-3 space-y-1.5">
                {HOOKS.map((hook) => (
                  <li
                    key={hook.id}
                    className={`flex items-start gap-3 rounded-xl border px-3 py-2.5 ${
                      hook.recommended
                        ? "border-signal-500/30 bg-signal-500/[0.07]"
                        : "border-transparent"
                    }`}
                  >
                    <span
                      className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md font-mono text-[10px] font-semibold ${
                        hook.recommended
                          ? "bg-signal-500/15 text-signal-400"
                          : "bg-white/5 text-zinc-500"
                      }`}
                    >
                      {hook.id}
                    </span>
                    <span
                      className={`flex-1 text-sm leading-snug ${
                        hook.recommended ? "text-white" : "text-zinc-400"
                      }`}
                    >
                      &ldquo;{hook.text}&rdquo;
                    </span>
                    {hook.recommended ? (
                      <span className="shrink-0 rounded-md bg-signal-500/10 px-1.5 py-0.5 font-mono text-[10px] text-signal-400">
                        +12 retention
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>

            {/* Timing bar */}
            <div className="border-t border-line px-5 py-5 sm:px-6">
              <div className="flex items-baseline justify-between">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
                  Timing
                </p>
                <p className="font-mono text-[10px] text-zinc-600">
                  retention-optimized cut
                </p>
              </div>
              <div className="relative mt-3 h-2.5 rounded-full bg-ink-700">
                <div
                  aria-hidden
                  className="absolute inset-y-0 left-[17%] right-[32%] rounded-full bg-brand-500"
                />
                <div
                  aria-hidden
                  className="absolute -inset-y-1 left-[17%] w-0.5 rounded-full bg-white/80"
                />
                <div
                  aria-hidden
                  className="absolute -inset-y-1 right-[32%] w-0.5 rounded-full bg-white/80"
                />
              </div>
              <div className="mt-2.5 flex items-center justify-between font-mono text-[10px]">
                <span className="text-zinc-600">00:00.000</span>
                <span className="text-brand-300">
                  00:12.480 <span className="text-zinc-500">→</span> 00:49.210
                </span>
                <span className="text-zinc-600">01:12.960</span>
              </div>
            </div>

            {/* Actionable tip */}
            <div className="flex items-start gap-2.5 border-t border-line bg-white/[0.02] px-5 py-4 sm:px-6">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="mt-0.5 shrink-0 text-ember-400"
                aria-hidden
              >
                {/* Advisory, not identity: the tip is worth points, so the
                    icon is a rising line rather than the brand mark. */}
                <path d="M3 16.5 9.5 10l4 4L21 6.5" />
                <path d="M15 6.5h6v6" />
              </svg>
              <p className="text-xs leading-relaxed text-zinc-400">
                <span className="font-medium text-zinc-200">Tip:</span> lead
                with the payoff. Moving the &ldquo;$40K&rdquo; line to 0:00 is
                worth ~6 points.
              </p>
            </div>
          </Card>

          {/* Capability cards */}
          <div className="grid h-full gap-4 sm:grid-cols-2">
            {CAPABILITIES.map((cap) => (
              <Card key={cap.name} className="h-full p-5 sm:p-6">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-brand-500/25 bg-brand-500/10 text-brand-300">
                  {cap.icon}
                </div>
                <h3 className="mt-4 font-display text-base font-semibold tracking-tight text-white">
                  {cap.name}
                </h3>
                <p className="mt-2 text-sm leading-relaxed text-zinc-400">
                  {cap.blurb}
                </p>
              </Card>
            ))}
          </div>
        </div>
      </Container>
    </section>
  );
}
