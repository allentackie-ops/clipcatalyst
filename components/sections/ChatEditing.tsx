import { Badge, Card, Container, GradientText, SectionHeading } from "@/components/ui";

const EXCHANGES: {
  command: string;
  reply: string;
  from: number;
  to: number;
  applied: string;
}[] = [
  {
    command: "Make clip 2 more energetic",
    reply:
      "Tightened 3 cuts, boosted pacing 12%, re-timed captions to land on the beat.",
    from: 74,
    to: 88,
    applied: "3.8s",
  },
  {
    command: "Remove the awkward pause in clip 4",
    reply:
      "Cut 2.4s of dead air at 0:31, crossfaded the audio, re-flowed the caption line.",
    from: 71,
    to: 79,
    applied: "2.1s",
  },
  {
    command: "Add a zoom on the host's face when he laughs",
    reply:
      "Added a 1.3× face-tracked punch-in at 0:42, eased over 6 frames.",
    from: 79,
    to: 86,
    applied: "4.2s",
  },
];

const SUGGESTIONS = [
  "Remove filler words",
  "Punch up the hook",
  "Cut the intro to 3 seconds",
];

function CatalystAvatar() {
  return (
    <span
      aria-hidden
      className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-spark-500 shadow-[0_0_12px_rgba(139,92,246,0.4)]"
    >
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
        <path d="M13 2 4.5 13.5H11L9.5 22 19 10h-6.5L13 2Z" fill="white" />
      </svg>
    </span>
  );
}

function ScoreDelta({ from, to }: { from: number; to: number }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-md border border-signal-500/20 bg-signal-500/10 px-1.5 py-0.5 font-mono text-[10px] text-signal-400"
      aria-label={`Virality score improved from ${from} to ${to}`}
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
        <path d="M12 19V5" />
        <path d="m5 12 7-7 7 7" />
      </svg>
      score {from} → {to}
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
          eyebrow="Copilot for clips"
          title={
            <>
              Edit by <GradientText>typing</GradientText>, not by timeline
            </>
          }
          lede="Say what you want changed. Catalyst applies the edit, shows the result, and re-scores the clip — the edit loop drops from hours to minutes."
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
                Catalyst <span className="text-zinc-600">—</span> clip 2 of 6
              </p>
              <Badge tone="brand">Pro</Badge>
            </div>

            {/* Conversation */}
            <div className="flex flex-col gap-5 px-4 py-6 sm:px-6">
              {EXCHANGES.map((x) => (
                <div key={x.command} className="flex flex-col gap-3">
                  {/* User bubble */}
                  <div className="flex justify-end">
                    <p className="max-w-[85%] rounded-2xl rounded-br-md border border-brand-500/30 bg-brand-500/15 px-4 py-2.5 text-sm leading-snug text-brand-100 sm:max-w-[75%]">
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
                        <ScoreDelta from={x.from} to={x.to} />
                        <span className="font-mono text-[10px] text-zinc-600">
                          applied in {x.applied}
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
            Every command runs on the real timeline — cuts, zooms, captions,
            pacing — and the virality score updates the second it lands. Undo
            with a word.
          </p>
        </div>
      </Container>
    </section>
  );
}
