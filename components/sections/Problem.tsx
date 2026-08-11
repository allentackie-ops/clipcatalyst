import { Badge, Card, Container } from "@/components/ui";

const OLD_WAY = [
  "Scrub hours of footage to find one 60-second moment",
  "Upload the whole file before anything can start",
  "Wait in a queue behind everyone else's video",
  "“Finished” clips that still need re-editing",
];

const NEW_WAY = [
  ["Find", "the moments worth clipping, scored and ranked"],
  ["Cut", "on sentence boundaries, reframed to 9:16"],
  ["Caption", "every word, colored per speaker"],
  ["Score", "each clip 0–100, with the one fix that helps most"],
  ["Download", "the MP4, or share it straight from your phone"],
] as const;

function XMark() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="mt-1 shrink-0 text-zinc-600"
    >
      <path
        d="M4 4l8 8M12 4l-8 8"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}

function Check() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="mt-1 shrink-0 text-signal-400"
    >
      <path
        d="M3 8.5l3.2 3.2L13 5"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function Problem() {
  return (
    <section id="problem" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
      />
      <Container className="relative">
        {/* Giant stat */}
        <div className="mx-auto max-w-3xl text-center">
          <p className="mb-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-brand-400">
            The problem
          </p>
          <h2 className="font-display tracking-tight">
            <span className="block bg-gradient-to-r from-brand-400 via-spark-400 to-ember-400 bg-clip-text text-7xl font-semibold leading-none text-transparent sm:text-8xl md:text-[10rem]">
              Hours
            </span>
            <span className="mt-4 block text-2xl font-semibold text-white sm:text-3xl md:mt-6 md:text-4xl">
              of scrubbing to find one sixty-second moment
            </span>
          </h2>
          <p className="mt-5 text-base leading-relaxed text-zinc-400 sm:text-lg">
            That is the job today: scrub, cut, reframe, caption, repeat. And the
            tools that offer to do it for you want the whole file uploaded to
            someone else&rsquo;s server first.
          </p>
        </div>

        {/* Contrast cards */}
        <div className="mx-auto mt-14 grid max-w-4xl gap-5 md:mt-20 md:grid-cols-2 md:gap-6">
          <Card className="flex h-full flex-col p-7 sm:p-8">
            <div className="flex items-center justify-between gap-3">
              <h3 className="font-display text-lg font-semibold text-zinc-300">
                The old way
              </h3>
              <span className="font-mono text-xs uppercase tracking-[0.15em] text-zinc-600">
                hrs / clip
              </span>
            </div>
            <ul className="mt-6 mb-6 space-y-4">
              {OLD_WAY.map((item) => (
                <li key={item} className="flex items-start gap-3">
                  <XMark />
                  <span className="text-sm leading-relaxed text-zinc-500">
                    {item}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-auto border-t border-line pt-5 font-mono text-xs uppercase tracking-[0.15em] text-zinc-600">
              Manually. Every time.
            </p>
          </Card>

          <Card className="relative overflow-hidden border-signal-500/25 p-7 sm:p-8">
            <div
              aria-hidden
              className="pointer-events-none absolute -top-16 -right-16 h-48 w-48 rounded-full bg-signal-500/10 blur-[80px]"
            />
            <div className="relative flex items-center justify-between gap-3">
              <h3 className="font-display text-lg font-semibold text-white">
                With ClipCatalyst
              </h3>
              <Badge tone="signal">
                <span className="font-mono">$0</span> · in your browser
              </Badge>
            </div>
            <ul className="relative mt-6 space-y-4">
              {NEW_WAY.map(([verb, rest]) => (
                <li key={verb} className="flex items-start gap-3">
                  <Check />
                  <span className="text-sm leading-relaxed text-zinc-400">
                    <span className="font-medium text-white">{verb}</span>{" "}
                    {rest}
                  </span>
                </li>
              ))}
            </ul>
            <p className="relative mt-6 border-t border-line pt-5 font-mono text-xs uppercase tracking-[0.15em] text-signal-400">
              Automatically. Every clip.
            </p>
          </Card>
        </div>
      </Container>
    </section>
  );
}
