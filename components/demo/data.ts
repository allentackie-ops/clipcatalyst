export type Platform = "TikTok" | "Shorts" | "Reels";

export type Clip = {
  id: string;
  title: string;
  /** Clip length, mm:ss */
  duration: string;
  /** Timecode in the source footage the clip was cut from */
  sourceAt: string;
  /** Virality score 0–100 */
  score: number;
  platforms: Platform[];
  /** Mock caption, split into words so one can be highlighted */
  captionWords: string[];
  /** Index of the word highlighted on the static card caption */
  captionHighlight: number;
  /** Tailwind gradient classes for the 9:16 thumbnail */
  gradient: string;
  /** Five AI-generated hooks */
  hooks: string[];
  /** Index of the recommended hook */
  recommendedHook: number;
};

export const PROJECT = {
  filename: "founder-podcast-ep42.mp4",
  runtime: "1:24:06",
  clipCount: 6,
} as const;

export const CLIPS: Clip[] = [
  {
    id: "clip-01",
    title: "Why we killed our best feature",
    duration: "0:37",
    sourceAt: "12:41",
    score: 94,
    platforms: ["TikTok", "Shorts"],
    captionWords: ["we", "deleted", "it", "on", "a", "Tuesday"],
    captionHighlight: 1,
    gradient: "bg-gradient-to-br from-brand-700/70 via-ink-800 to-ink-950",
    hooks: [
      "We deleted the feature 40% of users loved. Here’s why.",
      "Killing our best feature was the smartest call we ever made",
      "Our most popular feature was quietly killing the company",
      "Nobody tells you when to delete what’s working",
      "40% loved it. We shipped the delete anyway.",
    ],
    recommendedHook: 0,
  },
  {
    id: "clip-02",
    title: "The $0 marketing playbook",
    duration: "0:52",
    sourceAt: "27:03",
    score: 91,
    platforms: ["TikTok", "Reels"],
    captionWords: ["our", "CAC", "was", "literally", "zero"],
    captionHighlight: 4,
    gradient: "bg-gradient-to-tr from-ink-900 via-ink-800 to-spark-500/40",
    hooks: [
      "We hit 100K users on a $0 marketing budget",
      "The playbook growth agencies hope you never see",
      "Zero ad spend. 100K users. Here’s the math.",
      "Why we never bought a single ad",
      "Our entire growth strategy fits on one sticky note",
    ],
    recommendedHook: 0,
  },
  {
    id: "clip-03",
    title: "The hire that cost us $400K",
    duration: "0:41",
    sourceAt: "41:56",
    score: 87,
    platforms: ["Shorts", "Reels"],
    captionWords: ["the", "résumé", "was", "perfect"],
    captionHighlight: 3,
    gradient: "bg-gradient-to-b from-ember-500/30 via-ink-800 to-ink-950",
    hooks: [
      "The most expensive mistake wasn’t in the budget",
      "Hiring fast nearly broke the company",
      "A perfect résumé cost us $400,000",
      "We ignored one red flag. It cost $400K.",
      "What a $400K hiring mistake actually teaches you",
    ],
    recommendedHook: 2,
  },
  {
    id: "clip-04",
    title: "Ramen profitable in 9 months",
    duration: "0:58",
    sourceAt: "18:22",
    score: 82,
    platforms: ["TikTok", "Shorts"],
    captionWords: ["default", "alive", "changes", "everything"],
    captionHighlight: 1,
    gradient: "bg-gradient-to-br from-ink-700 via-brand-700/35 to-ink-950",
    hooks: [
      "We stopped fundraising and started charging",
      "Default alive by month nine — the exact path",
      "Ramen profitable is a superpower, not a phase",
      "Nine months to never needing a VC again",
      "The spreadsheet that got us to profitable",
    ],
    recommendedHook: 1,
  },
  {
    id: "clip-05",
    title: "47 rejections in a row",
    duration: "0:33",
    sourceAt: "58:47",
    score: 76,
    platforms: ["Reels", "Shorts"],
    captionWords: ["rejection", "number", "31", "hurt", "the", "most"],
    captionHighlight: 2,
    gradient: "bg-gradient-to-tl from-spark-500/25 via-ink-800 to-ink-900",
    hooks: [
      "47 investors said no. Number 48 changed everything.",
      "Reading my rejection emails out loud",
      "What rejection #31 taught me about pitching",
      "The deck that finally worked looked nothing like the first one",
      "Why the first 47 nos were the point",
    ],
    recommendedHook: 0,
  },
  {
    id: "clip-06",
    title: "Remote vs office: the real cost",
    duration: "0:45",
    sourceAt: "1:07:33",
    score: 68,
    platforms: ["TikTok", "Reels"],
    captionWords: ["the", "office", "was", "a", "$2M", "habit"],
    captionHighlight: 4,
    gradient: "bg-gradient-to-br from-ink-700 via-ink-850 to-ember-500/25",
    hooks: [
      "We went remote and revenue went up",
      "The real cost of a desk nobody sits at",
      "Remote isn’t a perk. It’s a P&L line.",
      "Our office was a $2M-a-year habit",
      "What we bought with the office money instead",
    ],
    recommendedHook: 3,
  },
];

export const PROCESSING_CLIP = {
  title: "Cold outreach that actually works",
  sourceAt: "1:02:14",
  eta: "~90s",
  stage: "L3 · visual processing",
} as const;
