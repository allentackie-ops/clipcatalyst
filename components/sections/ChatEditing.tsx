import { Badge, Card, Container, GradientText, Mark, SectionHeading } from "@/components/ui";

/**
 * Real commands the parser accepts, and replies in the shape the editor
 * actually produces — applyCommand's summary sentence is used verbatim as the
 * chat reply, so nothing here can promise an edit the code cannot perform.
 */
const EXCHANGES: {
  command: string;
  reply: string;
  length: string;
}[] = [
  {
    command: "Make clip 2 more energetic",
    reply: "Tightened 3 pauses and added 2 punch-in zooms.",
    length: "27.4s",
  },
  {
    command: "Remove the pause at 0:14",
    reply: "Cut the 1.2 s pause at 0:14.",
    length: "26.2s",
  },
  {
    command: "Punch in when he says funnels",
    reply: "Added a 1.6 s zoom at 0:09.",
    length: "26.2s",
  },
];

const SUGGESTIONS = [
  "Remove all the pauses",
  "Trim the first 2 seconds",
  "Try another hook",
];

function CatalystAvatar() {
  return (
    <span
      aria-hidden
      className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-spark-500 shadow-[0_0_12px_rgba(139,92,246,0.4)]"
    >
      <Mark className="h-3 w-3 text-white" />
    </span>
  );
}

/**
 * The clip is re-rendered after an edit and its new duration is shown. It is
 * NOT re-scored — nothing in the editor recomputes the virality score — so
 * this chip reports the one thing that actually changes.
 */
function RerenderChip({ length }: { length: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-md border border-signal-500/20 bg-signal-500/10 px-1.5 py-0.5 font-mono text-[10px] text-signal-400"
      aria-label={`Clip re-rendered, now ${length} long`}
    >
      <svg
        width="9"
        height="9"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <path d="M3 12a9 9 0 1 0 3-6.7" />
        <path d="M3 4v5h5" />
      </svg>
      re-rendered · {length}
    </span>
  );
}

export default function ChatEditing() {
  return (
    <section id="chat-editing" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-spark-500/15 blur-[120px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="Plain-language editing"
          title={
            <>
              Edit by <GradientText>typing</GradientText>, not by timeline
            </>
          }
          lede="Say what you want changed. Catalyst parses it, applies it, and re-renders the clip on your machine. Free, in the browser, no account — not a paid upgrade."
        />

        <div className="mx-auto mt-16 max-w-2xl">
          <Card className="overflow-hidden shadow-[0_24px_80px_-32px_rgba(139,92,246,0.25)]">
            {/* Title bar */}
            <div className="flex items-center justify-between gap-4 border-b border-line bg-white/[0.02] px-5 py-3.5">
              <div className="flex items-center gap-2" aria-hidden>
                <span className="h-2.5 w-2.5 rounded-full bg-[#ff5f57]/80" />
                <span className="h-2.5 w-2.5 rounded-full bg-[#febc2e]/80" />
                <span className="h-2.5 w-2.5 rounded-full bg-[#28c840]/80" />
              </div>
              <p className="font-mono text-xs text-zinc-400">
                Catalyst <span className="text-zinc-600">—</span> clip 2 of 3
              </p>
              <Badge tone="brand">Free</Badge>
            </div>

            {/* Conversation */}
            <div className="flex flex-col gap-5 px-4 py-6 sm:px-6">
              {EXCHANGES.map((x) => (
                <div key={x.command} className="flex flex-col gap-3">
                  {/* User bubble */}
                  <div className="flex justify-end">
                    <p className="max-w-[85%] rounded-2xl rounded-br-md border border-brand-500/30 bg-brand-500/15 px-4 py-2.5 text-sm leading-snug text-white sm:max-w-[75%]">
                      {x.command}
                    </p>
                  </div>
                  {/* Assistant bubble */}
                  <div className="flex items-start gap-2.5">
                    <CatalystAvatar />
                    <div className="max-w-[85%] rounded-2xl rounded-bl-md border border-line bg-white/[0.04] px-4 py-3 sm:max-w-[80%]">
                      <p className="text-sm leading-relaxed text-zinc-300">
                        {x.reply}
                      </p>
                      <div className="mt-2.5 flex flex-wrap items-center gap-2">
                        <RerenderChip length={x.length} />
                        <span className="font-mono text-[11px] text-zinc-400">
                          on your machine
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              ))}

              {/* Typing indicator */}
              <div className="flex items-start gap-2.5">
                <CatalystAvatar />
                <div
                  className="flex items-center gap-1.5 rounded-2xl rounded-bl-md border border-line bg-white/[0.04] px-4 py-3.5"
                  role="status"
                  aria-label="Catalyst is applying the next edit"
                >
                  <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-zinc-400" />
                  <span
                    className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-zinc-400"
                    style={{ animationDelay: "200ms" }}
                  />
                  <span
                    className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-zinc-400"
                    style={{ animationDelay: "400ms" }}
                  />
                </div>
              </div>
            </div>
          </Card>

          {/* Quick suggestions */}
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-600">
              Try
            </span>
            {SUGGESTIONS.map((s) => (
              <Badge key={s} tone="neutral">
                &ldquo;{s}&rdquo;
              </Badge>
            ))}
          </div>

          <p className="mx-auto mt-8 max-w-xl text-center text-sm leading-relaxed text-zinc-500">
            Every command runs on the real edit list — pauses, pace, zooms,
            trims, captions, hooks — and the clip is rebuilt from it. Say
            &ldquo;undo&rdquo; to step back or &ldquo;reset&rdquo; to start
            over. It is a phrase parser, not a chatbot: the same sentence always
            does the same thing, and anything it can&rsquo;t do it says so.
          </p>
        </div>
      </Container>
    </section>
  );
}
