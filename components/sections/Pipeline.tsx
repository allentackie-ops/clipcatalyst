import { Container, GradientText, SectionHeading } from "@/components/ui";

type Layer = {
  id: string;
  name: string;
  fn: string;
  tech: string;
  chip: string;
};

const BRAND_CHIP = "border-brand-500/30 bg-brand-500/10 text-brand-300";

const LAYERS: Layer[] = [
  {
    id: "L1",
    name: "Topic detection",
    fn: "Transcribes every word, maps every speaker, and flags the moments worth cutting.",
    tech: "WhisperX · speaker diarization",
    chip: BRAND_CHIP,
  },
  {
    id: "L2",
    name: "Creative scoring",
    fn: "A multimodal model rates each candidate out of 100 on hook, arc, and payoff.",
    tech: "Multimodal LLM · retention priors",
    chip: BRAND_CHIP,
  },
  {
    id: "L3",
    name: "Visual processing",
    fn: "Reframes to 9:16 and keeps every face locked dead-center in frame.",
    tech: "OpenCV · MediaPipe face tracking",
    chip: BRAND_CHIP,
  },
  {
    id: "L4",
    name: "Audio",
    fn: "Strips noise, levels dialogue, and lays music under the moment.",
    tech: "FFmpeg · AI noise reduction",
    chip: BRAND_CHIP,
  },
  {
    id: "L5",
    name: "Post-production",
    fn: "Applies animated captions, transitions, and matched B-roll. No timeline required.",
    tech: "Caption templates · B-roll matcher",
    chip: BRAND_CHIP,
  },
  {
    id: "L6",
    name: "Distribution",
    fn: "Publishes everywhere in one click, sized, captioned, and scheduled per platform.",
    tech: "TikTok · Shorts · Reels APIs",
    chip: BRAND_CHIP,
  },
  {
    id: "L7",
    name: "Optimization",
    fn: "A/B tests every clip in the wild and feeds the results back into scoring.",
    tech: "A/B testing · reinforcement learning",
    chip: "border-signal-500/40 bg-signal-500/10 text-signal-400",
  },
];

export default function Pipeline() {
  return (
    <section id="pipeline" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          title={
            <>
              Seven layers, <GradientText>one pipeline</GradientText>
            </>
          }
          lede="From raw footage to published clip, every frame passes through seven specialized layers. The last one feeds results back into scoring, so every clip you ship makes the engine smarter."
        />

        <div className="mx-auto mt-16 max-w-3xl">
          {/* Input marker */}
          <div className="relative flex items-center gap-4 pb-8 sm:gap-6">
            <div
              aria-hidden
              className="absolute bottom-0 left-6 top-4 w-px bg-brand-500/40"
            />
            <div className="flex w-12 shrink-0 justify-center">
              <span aria-hidden className="h-2 w-2 rounded-full bg-brand-400" />
            </div>
            <p className="min-w-0 truncate font-mono text-xs text-zinc-500">
              <span className="uppercase tracking-[0.18em] text-zinc-600">
                Input
              </span>{" "}
              <span className="text-zinc-700">·</span> founder-podcast-ep42.mp4{" "}
              <span className="text-zinc-700">·</span> 1:24:06
            </p>
          </div>

          <ol className="flex flex-col">
            {LAYERS.map((layer) => {
              const isLoop = layer.id === "L7";
              return (
                <li
                  key={layer.id}
                  className={`relative flex gap-4 sm:gap-6 ${
                    isLoop ? "" : "pb-9"
                  }`}
                >
                  {isLoop ? null : (
                    <div
                      aria-hidden
                      className="absolute bottom-0 left-6 top-12 w-px bg-brand-500/40"
                    />
                  )}

                  {/* Layer chip */}
                  <span
                    className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border bg-ink-900 font-mono text-sm font-semibold ${layer.chip}`}
                  >
                    {layer.id}
                  </span>

                  {/* Layer content */}
                  {isLoop ? (
                    <div className="min-w-0 flex-1 rounded-2xl border border-signal-500/25 bg-signal-500/[0.05] p-5">
                      <h3 className="font-display text-lg font-semibold tracking-tight text-white">
                        {layer.name}
                      </h3>
                      <p className="mt-1.5 text-sm leading-relaxed text-zinc-400">
                        {layer.fn}
                      </p>
                      <p className="mt-2 font-mono text-xs text-zinc-500">
                        {layer.tech}
                      </p>
                      <p className="mt-4 flex items-center gap-2 border-t border-signal-500/15 pt-3.5 font-mono text-[11px] text-signal-400">
                        <svg
                          width="12"
                          height="12"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden
                          className="shrink-0"
                        >
                          <path d="M3 12a9 9 0 1 0 3-6.7" />
                          <path d="M3 4v5h5" />
                        </svg>
                        results feed back into L2 scoring
                      </p>
                    </div>
                  ) : (
                    <div className="min-w-0 flex-1 pt-0.5">
                      <h3 className="font-display text-lg font-semibold tracking-tight text-white">
                        {layer.name}
                      </h3>
                      <p className="mt-1 text-sm leading-relaxed text-zinc-400">
                        {layer.fn}
                      </p>
                      <p className="mt-2 font-mono text-xs text-zinc-500">
                        {layer.tech}
                      </p>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      </Container>
    </section>
  );
}
