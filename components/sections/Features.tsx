import type { ReactNode } from "react";
import { Badge, Card, Container, SectionHeading } from "@/components/ui";

/* ---------------------------------------------------------------- */
/* Icons — simple 24px line glyphs, one per feature                 */
/* ---------------------------------------------------------------- */

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const icons = {
  sparkle: (
    <Icon>
      <path d="M11.5 3.5 13.2 8.3 18 10l-4.8 1.7-1.7 4.8-1.7-4.8L5 10l4.8-1.7 1.7-4.8Z" />
      <path d="m18.5 14.5.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1Z" />
    </Icon>
  ),
  captions: (
    <Icon>
      <rect x="3" y="5" width="18" height="14" rx="2.5" />
      <path d="M6.5 15.5H12M14.5 15.5h3M6.5 11.5h3" />
    </Icon>
  ),
  reframe: (
    <Icon>
      <rect x="8.5" y="3.5" width="7" height="17" rx="2" />
      <path d="M2.5 12h3.2M21.5 12h-3.2" />
      <path d="m4.2 10.5 1.5 1.5-1.5 1.5M19.8 10.5 18.3 12l1.5 1.5" />
    </Icon>
  ),
  palette: (
    <Icon>
      <path d="M12 3a9 9 0 1 0 0 18h.9c1 0 1.8-.8 1.8-1.8 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.8 1.8-1.8h2.1c2.4 0 4.4-2 4.4-4.4C22 6.6 17.5 3 12 3Z" />
      <circle cx="7.8" cy="10" r="1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="7.2" r="1" fill="currentColor" stroke="none" />
      <circle cx="16.2" cy="10" r="1" fill="currentColor" stroke="none" />
    </Icon>
  ),
  sliders: (
    <Icon>
      <path d="M6 4.5v9M6 17.5v2M12 4.5V6M12 10v9.5M18 4.5V11M18 15v4.5" />
      <circle cx="6" cy="15.5" r="2" />
      <circle cx="12" cy="8" r="2" />
      <circle cx="18" cy="13" r="2" />
    </Icon>
  ),
  send: (
    <Icon>
      <path d="m21 3-6.5 18-4-9-9-4L21 3Z" />
      <path d="M21 3 10.5 13.5" />
    </Icon>
  ),
  gauge: (
    <Icon>
      <path d="M3.5 18.5a9.5 9.5 0 1 1 17 0" />
      <path d="m12 13.5 3.6-4" />
      <circle cx="12" cy="13.5" r="1" fill="currentColor" stroke="none" />
    </Icon>
  ),
  layers: (
    <Icon>
      <path d="m12 3 8.5 4.25L12 11.5 3.5 7.25 12 3Z" />
      <path d="m3.5 12 8.5 4.25L20.5 12" />
      <path d="m3.5 16.25 8.5 4.25 8.5-4.25" />
    </Icon>
  ),
  mic: (
    <Icon>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3" />
    </Icon>
  ),
  hook: (
    <Icon>
      <circle cx="12" cy="4.2" r="1.7" />
      <path d="M12 5.9v7.3a4.7 4.7 0 0 0 9.4 0v-2.4l-2 1.5" />
    </Icon>
  ),
  film: (
    <Icon>
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <path d="M7.5 4.5v15M16.5 4.5v15M3 9.5h4.5M3 14.5h4.5M16.5 9.5H21M16.5 14.5H21" />
    </Icon>
  ),
  chat: (
    <Icon>
      <path d="M12 3.5a8.2 8.2 0 0 1 8.2 8.2c0 4.5-3.7 8.1-8.2 8.1-1.2 0-2.4-.2-3.4-.7L3.8 20.3l1.2-4.5a8 8 0 0 1-1.2-4.1A8.2 8.2 0 0 1 12 3.5Z" />
      <path d="M8.5 10.4h7M8.5 13.4h4.2" />
    </Icon>
  ),
  team: (
    <Icon>
      <circle cx="9" cy="7.8" r="3.3" />
      <path d="M2.8 20a6.2 6.2 0 0 1 12.4 0" />
      <path d="M15.8 4.9a3.3 3.3 0 0 1 0 5.8M17.6 14.6a6.2 6.2 0 0 1 3.6 5.4" />
    </Icon>
  ),
  code: (
    <Icon>
      <path d="m8.5 8-4.5 4 4.5 4M15.5 8l4.5 4-4.5 4M13.3 5.5l-2.6 13" />
    </Icon>
  ),
  split: (
    <Icon>
      <path d="M3.5 12h3.8c2 0 3.1-.9 4.1-2.3 1-1.4 2.1-2.2 4.1-2.2H20" />
      <path d="M3.5 12h3.8c2 0 3.1.9 4.1 2.3 1 1.4 2.1 2.2 4.1 2.2H20" />
      <path d="m17.8 5.3 2.2 2.2-2.2 2.2M17.8 14.3l2.2 2.2-2.2 2.2" />
    </Icon>
  ),
  bars: (
    <Icon>
      <path d="M4 20h16" />
      <path d="M7.5 16v-4M12 16V6.5M16.5 16v-6.5" />
    </Icon>
  ),
  terminal: (
    <Icon>
      <rect x="3" y="4.5" width="18" height="15" rx="2" />
      <path d="m7 9.5 3 3-3 3M13.5 15.5H17" />
    </Icon>
  ),
} as const;

/* ---------------------------------------------------------------- */
/* Content — 9 core + 8 pro (from DESIGN.md)                        */
/* ---------------------------------------------------------------- */

type Feature = {
  name: string;
  desc: string;
  icon: ReactNode;
};

const CORE_FEATURES: Feature[] = [
  {
    name: "Smart highlighting",
    desc: "Golden-nugget detection surfaces the moments worth clipping.",
    icon: icons.sparkle,
  },
  {
    name: "Auto-captioning",
    desc: "10+ animated templates with keyword highlights, emoji, and speaker colors.",
    icon: icons.captions,
  },
  {
    name: "Vertical reframing",
    desc: "Auto 9:16 crop with face tracking that never loses the speaker.",
    icon: icons.reframe,
  },
  {
    name: "Brand kit",
    desc: "Your logo, fonts, and colors applied to every clip automatically.",
    icon: icons.palette,
  },
  {
    name: "Built-in editor",
    desc: "Trim, tweak, and polish without ever leaving the browser.",
    icon: icons.sliders,
  },
  {
    name: "One-click publish",
    desc: "Push straight to TikTok, Shorts, and Reels the moment a clip is done.",
    icon: icons.send,
  },
  {
    name: "Virality score",
    desc: "Every clip rated 0–100, with tips to push it higher.",
    icon: icons.gauge,
  },
  {
    name: "Bulk processing",
    desc: "Feed it whole podcasts and VODs; get a full clip library back.",
    icon: icons.layers,
  },
  {
    name: "Multi-speaker detection",
    desc: "Knows who is talking — and frames and captions accordingly.",
    icon: icons.mic,
  },
];

const PRO_FEATURES: Feature[] = [
  {
    name: "AI hook generator",
    desc: "Five scroll-stopping hooks per clip, strongest one flagged.",
    icon: icons.hook,
  },
  {
    name: "Auto B-roll",
    desc: "Relevant stock footage layered in exactly where it lands.",
    icon: icons.film,
  },
  {
    name: "Chat-based editing",
    desc: "Type “make clip 2 more energetic” — the AI does the rest.",
    icon: icons.chat,
  },
  {
    name: "Team workspace",
    desc: "Approvals and shared brand kits keep every clip on-message.",
    icon: icons.team,
  },
  {
    name: "XML export",
    desc: "Hand finished timelines to Premiere, Resolve, or Final Cut.",
    icon: icons.code,
  },
  {
    name: "A/B testing",
    desc: "Three versions per clip, so the data picks the winner.",
    icon: icons.split,
  },
  {
    name: "Analytics dashboard",
    desc: "See which clips convert — then double down on what works.",
    icon: icons.bars,
  },
  {
    name: "API access",
    desc: "Wire clipping into your own pipeline, end to end.",
    icon: icons.terminal,
  },
];

/* ---------------------------------------------------------------- */
/* Cards                                                            */
/* ---------------------------------------------------------------- */

function CoreCard({ feature }: { feature: Feature }) {
  return (
    <Card className="flex items-start gap-3.5 p-5 transition-colors duration-200 hover:border-line-strong">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-line bg-white/5 text-zinc-300">
        {feature.icon}
      </span>
      <span>
        <span className="block text-[15px] font-medium text-white">
          {feature.name}
        </span>
        <span className="mt-1 block text-sm leading-relaxed text-zinc-400">
          {feature.desc}
        </span>
      </span>
    </Card>
  );
}

function ProCard({ feature }: { feature: Feature }) {
  return (
    <div className="rounded-2xl bg-gradient-to-br from-spark-500/40 via-white/10 to-brand-500/30 p-px transition-all duration-300 hover:from-spark-500/60 hover:to-brand-500/50">
      <div className="flex h-full items-start gap-3.5 rounded-[calc(1rem-1px)] bg-ink-850 p-5">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-spark-500/30 bg-spark-500/10 text-spark-400">
          {feature.icon}
        </span>
        <span>
          <span className="block text-[15px] font-medium text-white">
            {feature.name}
          </span>
          <span className="mt-1 block text-sm leading-relaxed text-zinc-400">
            {feature.desc}
          </span>
        </span>
      </div>
    </div>
  );
}

function GroupHeading({
  badge,
  tone,
  label,
  count,
}: {
  badge: string;
  tone: "neutral" | "spark";
  label: string;
  count: number;
}) {
  return (
    <div className="mb-6 flex items-center gap-4">
      <h3 className="flex items-center gap-3 font-display text-lg font-semibold tracking-tight text-white">
        <Badge tone={tone}>{badge}</Badge>
        {label}
      </h3>
      <div aria-hidden className="h-px flex-1 bg-line" />
      <span className="font-mono text-xs text-zinc-500">
        {String(count).padStart(2, "0")}
      </span>
    </div>
  );
}

/* ---------------------------------------------------------------- */
/* Section                                                          */
/* ---------------------------------------------------------------- */

export default function Features() {
  return (
    <section id="features" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-1/2 h-[28rem] w-[28rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand-600/10 blur-[140px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="Everything in the box"
          title="The whole clipping stack, built in"
          lede="Nine core features on every plan — including free. Eight Pro tools your current editor can't touch."
        />

        <div className="mt-14 md:mt-16">
          <GroupHeading
            badge="Core"
            tone="neutral"
            label="Every plan"
            count={CORE_FEATURES.length}
          />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {CORE_FEATURES.map((feature) => (
              <CoreCard key={feature.name} feature={feature} />
            ))}
          </div>
        </div>

        <div className="mt-14 md:mt-16">
          <GroupHeading
            badge="Pro"
            tone="spark"
            label="The differentiators"
            count={PRO_FEATURES.length}
          />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PRO_FEATURES.map((feature) => (
              <ProCard key={feature.name} feature={feature} />
            ))}
          </div>
        </div>
      </Container>
    </section>
  );
}
