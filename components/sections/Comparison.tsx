import { Badge, Card, Container, SectionHeading } from "@/components/ui";

type Cell =
  | { kind: "yes" }
  | { kind: "no" }
  | { kind: "text"; value: string; mono?: boolean };

type Row = {
  feature: string;
  cells: [Cell, Cell]; // Browser Studio, Cloud pipeline
};

/**
 * This table used to compare ClipCatalyst against OpusClip, Klap, VEED and
 * Descript using processing times and plan limits we had no source for. A
 * comparison you cannot defend is a liability once you are advertising, so the
 * competitors are gone and the axis is now the one thing we can prove line by
 * line: our own two engines, which are genuinely different products.
 *
 * Browser column: components/studio + lib/studio (shipped).
 * Cloud column: api/clipcatalyst_api plan entitlements (built, not live).
 */
const ROWS: Row[] = [
  {
    feature: "Works today",
    cells: [{ kind: "yes" }, { kind: "no" }],
  },
  {
    feature: "Runs without an account",
    cells: [{ kind: "yes" }, { kind: "no" }],
  },
  {
    feature: "Video stays on your device",
    cells: [{ kind: "yes" }, { kind: "no" }],
  },
  {
    feature: "Face-tracked 9:16 reframing",
    cells: [{ kind: "yes" }, { kind: "yes" }],
  },
  {
    feature: "Word-level captions, coloured per speaker",
    cells: [{ kind: "yes" }, { kind: "yes" }],
  },
  {
    feature: "Hook suggestions + 0–100 scoring",
    cells: [{ kind: "yes" }, { kind: "yes" }],
  },
  {
    feature: "Chat-based editing",
    cells: [{ kind: "yes" }, { kind: "no" }],
  },
  {
    feature: "Clips per run",
    cells: [
      { kind: "text", value: "1–3", mono: true },
      { kind: "text", value: "1–3", mono: true },
    ],
  },
  {
    feature: "Monthly cap",
    cells: [
      { kind: "text", value: "None" },
      { kind: "text", value: "3 free → unlimited by plan" },
    ],
  },
  {
    feature: "Max export height",
    cells: [
      { kind: "text", value: "1080p", mono: true },
      { kind: "text", value: "720p free · up to 4K paid" },
    ],
  },
  {
    feature: "Watermark",
    cells: [
      { kind: "text", value: "Always on" },
      { kind: "text", value: "Off on paid plans" },
    ],
  },
  {
    feature: "Source limit",
    cells: [
      { kind: "text", value: "20 min · 1.4 GB" },
      { kind: "text", value: "2 GB per upload" },
    ],
  },
];

const SCOPE: { name: string; detail: string }[] = [
  { name: "Desktop", detail: "a phone tab cannot hold a transcription and a render" },
  { name: "Watermarked", detail: "removing it is a paid cloud entitlement" },
  { name: "20 min · 1.4 GB", detail: "the browser holds the whole file in memory" },
  { name: "No publishing", detail: "you download the MP4 or use your phone's share sheet" },
];

function CheckMark() {
  return (
    <span className="inline-flex items-center justify-center">
      <svg
        width="18"
        height="18"
        viewBox="0 0 20 20"
        fill="none"
        aria-hidden
        className="text-signal-400"
      >
        <path
          d="M4 10.5 8.2 14.7 16 6"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="sr-only">Yes</span>
    </span>
  );
}

function DashMark() {
  return (
    <span className="inline-flex items-center justify-center">
      <svg
        width="18"
        height="18"
        viewBox="0 0 20 20"
        fill="none"
        aria-hidden
        className="text-zinc-600"
      >
        <path
          d="M5 10h10"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
      <span className="sr-only">No</span>
    </span>
  );
}

function CellValue({ cell, highlight }: { cell: Cell; highlight: boolean }) {
  if (cell.kind === "yes") return <CheckMark />;
  if (cell.kind === "no") return <DashMark />;
  const tone = highlight ? "font-semibold text-white" : "text-zinc-400";
  const mono = cell.mono ? "font-mono" : "";
  return <span className={`text-sm ${mono} ${tone}`.trim()}>{cell.value}</span>;
}

const highlightCell = "border-x border-brand-500/20 bg-brand-500/[0.07]";

export default function Comparison() {
  return (
    <section id="compare" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="Two engines"
          title={
            <>
              Everything here runs{" "}
              <span className="whitespace-nowrap">on your machine</span>
            </>
          }
          lede="Transcription, scoring, face tracking, captions and chat editing all ship today and all run in your browser — no account, no upload, no queue. The cloud pipeline below is built and tested for longer footage and bigger exports; it is not switched on yet."
        />

        <div className="relative mt-14 md:mt-16">
          <div className="overflow-x-auto rounded-2xl border border-line bg-ink-900/60">
            <table className="w-full min-w-[640px] text-left">
              <caption className="sr-only">
                Capability comparison: the in-browser ClipCatalyst Studio
                versus the cloud pipeline
              </caption>
              <thead>
                <tr className="border-b border-line-strong">
                  <th
                    scope="col"
                    className="px-5 py-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-zinc-500"
                  >
                    Capability
                  </th>
                  <th scope="col" className={`px-4 py-4 text-center ${highlightCell}`}>
                    <Badge tone="brand">Browser Studio</Badge>
                  </th>
                  <th
                    scope="col"
                    className="px-4 py-4 text-center text-sm font-medium text-zinc-400"
                  >
                    Cloud pipeline
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {ROWS.map((row) => (
                  <tr key={row.feature}>
                    <th
                      scope="row"
                      className="px-5 py-4 text-sm font-normal text-zinc-300"
                    >
                      {row.feature}
                    </th>
                    {row.cells.map((cell, i) => (
                      <td
                        key={i}
                        className={`px-4 py-4 text-center ${i === 0 ? highlightCell : ""}`}
                      >
                        <CellValue cell={cell} highlight={i === 0} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div
            aria-hidden
            className="pointer-events-none absolute inset-y-0 right-0 w-12 rounded-r-2xl bg-gradient-to-l from-ink-900 to-transparent md:hidden"
          />
        </div>
        <p className="mt-3 font-mono text-xs text-zinc-400 md:hidden">
          Swipe to compare &rarr;
        </p>
        <p className="mt-4 text-xs text-zinc-400">
          Browser figures are the limits the shipped Studio enforces. Cloud
          figures are the entitlements the API is built to enforce once it is
          live.
        </p>

        <Card className="mt-8 p-5 sm:p-6">
          <p className="font-mono text-[0.65rem] font-medium uppercase tracking-[0.2em] text-zinc-500">
            Worth knowing before you start
          </p>
          <ul className="mt-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:gap-x-8 sm:gap-y-2">
            {SCOPE.map(({ name, detail }) => (
              <li key={name} className="flex items-baseline gap-2 text-sm">
                <span className="font-medium text-zinc-200">{name}</span>
                <span className="text-zinc-500">&mdash; {detail}</span>
              </li>
            ))}
          </ul>
        </Card>

      </Container>
    </section>
  );
}
