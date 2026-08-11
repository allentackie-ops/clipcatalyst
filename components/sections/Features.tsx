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
/* Content — what the two engines actually do                       */
/* ---------------------------------------------------------------- */

type Feature = {
  name: string;
  desc: string;
  icon: ReactNode;
};

/** Shipped and free: every one of these runs in the browser, no account. */
const CORE_FEATURES: Feature[] = [
  {
    name: "Smart highlighting",
    desc: "Scores every candidate window on hook, density, energy and completeness, then picks the best without overlaps.",
    icon: icons.sparkle,
  },
  {
    name: "Word-level captions",
    desc: "Every word timed and burned in, with the spoken word highlighted as it lands.",
    icon: icons.captions,
  },
  {
    name: "Vertical reframing",
    desc: "Auto 9:16 crop with face tracking that keeps the speaker in frame.",
    icon: icons.reframe,
  },
  {
    name: "Multi-speaker detection",
    desc: "Diarization separates the voices and gives each speaker their own caption color.",
    icon: icons.mic,
  },
  {
    name: "Virality score",
    desc: "Every clip rated 0–100, with the reason it scored that way and one concrete fix.",
    icon: icons.gauge,
  },
  {
    name: "Hook suggestions",
    desc: "Up to five opening lines per clip, strongest first — swap between them by chat.",
    icon: icons.hook,
  },
  {
    name: "Chat-based editing",
    desc: "“Remove the pause at 0:14.” “Make it more energetic.” “Trim the first 2 seconds.” Undo with a word.",
    icon: icons.chat,
  },
  {
    name: "Runs on your machine",
    desc: "Whisper, scoring, and rendering all happen in the tab. Your video never leaves the device.",
    icon: icons.terminal,
  },
  {
    name: "Download & share",
    desc: "Save the MP4, or hand it straight to your phone's native share sheet.",
    icon: icons.send,
  },
];

/**
 * The cloud pipeline: written and tested, but never run on GPU hardware and
 * switched off by default. Listed as what it is built to add — not as
 * something a visitor can use today.
 */
const CLOUD_FEATURES: Feature[] = [
  {
    name: "Bigger sources",
    desc: "Uploads up to 2 GB a file, so hour-long episodes stop being the problem.",
    icon: icons.film,
  },
  {
    name: "GPU transcription",
    desc: "distil-large-v3 on a GPU worker, instead of the tiny model a browser tab can afford.",
    icon: icons.sliders,
  },
  {
    name: "Watermark-free renders",
    desc: "Paid plans drop the ClipCatalyst mark from every export.",
    icon: icons.palette,
  },
  {
    name: "Higher export ceiling",
    desc: "720p on the free plan, 1080p on Starter, up to 2160×3840 on Pro and Enterprise.",
    icon: icons.layers,
  },
  {
    name: "Monthly allowances",
    desc: "3 clips free, 30 on Starter, 100 on Pro, uncapped on Enterprise.",
    icon: icons.bars,
  },
  {
    name: "Owned job history",
    desc: "Every render is a job on your account — status you can poll, results you can re-fetch.",
    icon: icons.code,
  },
  {
    name: "Per-clip failure isolation",
    desc: "One clip failing to render never takes the rest of the batch down with it.",
    icon: icons.split,
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
          eyebrow="What ships"
          title="The whole clipping stack, in one tab"
          lede="Nine things the free browser Studio does today — no account, no upload — and seven the cloud pipeline is built to add once it is switched on."
        />

        <div className="mt-14 md:mt-16">
          <GroupHeading
            badge="Free"
            tone="neutral"
            label="In your browser, today"
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
