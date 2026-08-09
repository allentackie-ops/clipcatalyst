import { Badge, ScoreRing } from "@/components/ui";
import type { Clip } from "./data";
import { PROCESSING_CLIP } from "./data";
import { IconPlay } from "./icons";

function StaticCaption({ clip }: { clip: Clip }) {
  return (
    <span className="block rounded-lg bg-ink-950/75 px-2 py-1 text-center text-[11px] font-semibold leading-snug text-white backdrop-blur-sm">
      {clip.captionWords.map((word, i) => (
        <span
          key={i}
          className={i === clip.captionHighlight ? "text-ember-400" : undefined}
        >
          {word}
          {i < clip.captionWords.length - 1 ? " " : ""}
        </span>
      ))}
    </span>
  );
}

export default function ClipCard({
  clip,
  score,
  selected,
  onSelect,
}: {
  clip: Clip;
  score: number;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-expanded={selected}
      className={`group relative flex flex-col overflow-hidden rounded-2xl border bg-ink-850/80 text-left transition-all duration-200 hover:-translate-y-0.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400 ${
        selected
          ? "border-brand-400/60 ring-1 ring-brand-400/40"
          : "border-line hover:border-brand-400/40 hover:ring-1 hover:ring-brand-400/25"
      }`}
    >
      <span className="relative block overflow-hidden">
        <span
          aria-hidden
          className={`block aspect-[9/16] w-full ${clip.gradient} transition-transform duration-500 group-hover:scale-[1.04]`}
        />
        {/* key-light glow suggesting a subject */}
        <span
          aria-hidden
          className="pointer-events-none absolute left-1/2 top-1/4 h-24 w-24 -translate-x-1/2 rounded-full bg-white/10 blur-2xl"
        />
        {/* bottom vignette for caption readability */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-0 h-2/5 bg-gradient-to-t from-ink-950/85 to-transparent"
        />
        <span className="absolute right-2.5 top-2.5 rounded-md bg-ink-950/70 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200 backdrop-blur-sm">
          {clip.duration}
        </span>
        <span className="absolute inset-0 flex items-center justify-center opacity-0 transition-opacity duration-200 group-hover:opacity-100">
          <span className="flex h-11 w-11 items-center justify-center rounded-full border border-white/25 bg-ink-950/55 backdrop-blur-sm">
            <IconPlay className="h-4 w-4 translate-x-[1px] text-white" />
          </span>
        </span>
        <span className="absolute inset-x-3 bottom-3">
          <StaticCaption clip={clip} />
        </span>
      </span>

      <span className="flex flex-1 flex-col gap-2.5 p-3.5">
        <span className="flex items-start justify-between gap-2.5">
          <span className="block min-w-0">
            <span className="block text-[13px] font-medium leading-snug text-white sm:text-sm">
              {clip.title}
            </span>
            <span className="mt-1 block font-mono text-[11px] text-zinc-500">
              from {clip.sourceAt}
            </span>
          </span>
          <ScoreRing score={score} size={40} className="shrink-0" />
        </span>
        <span className="mt-auto flex flex-wrap gap-1.5">
          {clip.platforms.map((platform) => (
            <Badge key={platform} tone="neutral">
              {platform}
            </Badge>
          ))}
        </span>
      </span>
    </button>
  );
}

export function ProcessingCard() {
  return (
    <div className="relative flex flex-col overflow-hidden rounded-2xl border border-line bg-ink-850/50">
      <div className="relative">
        <div
          aria-hidden
          className="aspect-[9/16] w-full animate-pulse-soft bg-gradient-to-br from-brand-700/35 via-ink-800 to-spark-500/15"
        />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <span
            aria-hidden
            className="h-9 w-9 animate-spin rounded-full border-2 border-white/15 border-t-brand-400"
          />
          <span className="font-mono text-[11px] text-zinc-300">
            Rendering… {PROCESSING_CLIP.eta}
          </span>
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500">
            {PROCESSING_CLIP.stage}
          </span>
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3.5">
        <p className="text-sm font-medium leading-snug text-zinc-300">
          {PROCESSING_CLIP.title}
        </p>
        <p className="font-mono text-[11px] text-zinc-500">
          from {PROCESSING_CLIP.sourceAt}
        </p>
        {/* indeterminate progress: seamless leftward shimmer */}
        <div
          className="mt-auto h-1 overflow-hidden rounded-full bg-white/10"
          role="img"
          aria-label="Rendering in progress"
        >
          <div
            aria-hidden
            className="h-full w-[200%] animate-marquee"
            style={{ animationDuration: "1.4s" }}
          >
            <div className="flex h-full">
              <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-400 to-transparent" />
              <div className="h-full w-1/2 bg-gradient-to-r from-transparent via-brand-400 to-transparent" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
