import { Badge, Container, GradientText, SectionHeading } from "@/components/ui";

type Layer = {
  id: string;
  name: string;
  fn: string;
  tech: string;
  chip: string;
  /** Gradient segment connecting this chip to the next one. Omit on L7. */
  connector?: string;
};

/**
 * The seven stages a run actually passes through, in order, all of them inside
 * the browser tab. Each `tech` line names the real implementation — no stage
 * is listed here that the shipped code does not perform.
 */
const LAYERS: Layer[] = [
  {
    id: "L1",
    name: "Audio analysis",
    fn: "Decodes the track and maps where the energy spikes and where the dead air sits.",
    tech: "Web Audio · energy envelope + silence map",
    chip: "border-brand-500/30 bg-gradient-to-br from-brand-500/25 to-brand-500/5 text-brand-300",
    connector: "from-brand-500/50 to-brand-500/45",
  },
  {
    id: "L2",
    name: "Transcription",
    fn: "Turns speech into text with a timestamp on every single word.",
    tech: "whisper-tiny.en · WebAssembly, in-tab",
    chip: "border-brand-500/30 bg-gradient-to-br from-brand-500/25 to-brand-500/5 text-brand-300",
    connector: "from-brand-500/45 to-spark-500/45",
  },
  {
    id: "L3",
    name: "Speaker separation",
    fn: "Groups the voices so captions can color each speaker differently.",
    tech: "Spectral speaker features · no cloud model",
    chip: "border-spark-500/30 bg-gradient-to-br from-spark-500/25 to-spark-500/5 text-spark-400",
    connector: "from-spark-500/45 to-spark-500/40",
  },
  {
    id: "L4",
    name: "Scoring & selection",
    fn: "Rates every candidate window 0–100, then takes the best without letting them overlap.",
    tech: "Deterministic score · hook, density, energy, completeness",
    chip: "border-spark-500/30 bg-gradient-to-br from-spark-500/25 to-spark-500/5 text-spark-400",
    connector: "from-spark-500/40 to-ember-500/40",
  },
  {
    id: "L5",
    name: "Reframing",
    fn: "Finds the faces, builds a smooth camera move, and crops the shot to 9:16.",
    tech: "face-api tiny detector · smoothed crop track",
    chip: "border-ember-500/30 bg-gradient-to-br from-ember-500/25 to-ember-500/5 text-ember-400",
    connector: "from-ember-500/40 to-ember-500/40",
  },
  {
    id: "L6",
    name: "Render",
    fn: "Draws the reframed video and word-timed captions to a canvas and records the file.",
    tech: "Canvas 2D · MediaRecorder · burned-in captions",
    chip: "border-ember-500/30 bg-gradient-to-br from-ember-500/25 to-ember-500/5 text-ember-400",
    connector: "from-ember-500/40 to-signal-500/50",
  },
  {
    id: "L7",
    name: "Edit by chat",
    fn: "Say what to change — pauses, pace, zooms, trims, hooks — and the clip is rebuilt.",
    tech: "Phrase parser · no LLM, no randomness",
    chip: "border-signal-500/40 bg-gradient-to-br from-signal-500/30 to-signal-500/5 text-signal-400 shadow-[0_0_24px_rgba(52,211,153,0.25)]",
  },
];

export default function Pipeline() {
  return (
    <section id="pipeline" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="Under the hood"
          title={
            <>
              Seven layers, <GradientText>one pipeline</GradientText>
            </>
          }
          lede="From dropped file to finished clip, every frame passes through seven stages — and all seven run inside this tab. The last one is a loop: say what to change and the clip is rebuilt on your machine."
        />

        <div className="mx-auto mt-16 max-w-3xl">
          {/* Input marker */}
          <div className="relative flex items-center gap-4 pb-8 sm:gap-6">
            <div
              aria-hidden
              className="absolute bottom-0 left-6 top-4 w-px bg-gradient-to-b from-brand-400/60 to-brand-500/50"
            />
            <div className="flex w-12 shrink-0 justify-center">
              <span
                aria-hidden
                className="h-2 w-2 rounded-full bg-brand-400 shadow-[0_0_10px_rgba(139,92,246,0.8)]"
              />
            </div>
            <p className="min-w-0 truncate font-mono text-xs text-zinc-500">
              <span className="uppercase tracking-[0.18em] text-zinc-600">
                Input
              </span>{" "}
              <span className="text-zinc-700">·</span> founder-podcast-ep42.mp4{" "}
              <span className="text-zinc-700">·</span> 18:42
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
                  {layer.connector ? (
                    <div
                      aria-hidden
                      className={`absolute bottom-0 left-6 top-12 w-px bg-gradient-to-b ${layer.connector}`}
                    />
                  ) : null}

                  {/* Layer chip */}
                  <span
                    className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl border bg-ink-900 font-mono text-sm font-semibold ${layer.chip}`}
                  >
                    {layer.id}
                  </span>

                  {/* Layer content */}
                  {isLoop ? (
                    <div className="min-w-0 flex-1 rounded-2xl border border-signal-500/25 bg-signal-500/[0.05] p-5">
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                        <h3 className="font-display text-lg font-semibold tracking-tight text-white">
                          {layer.name}
                        </h3>
                        <Badge tone="signal">Free, no account</Badge>
                      </div>
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
                        every edit re-runs L6 on your machine
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
