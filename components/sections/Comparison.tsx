import { Badge, Card, Container, SectionHeading } from "@/components/ui";

type Cell =
  | { kind: "yes" }
  | { kind: "no" }
  | { kind: "text"; value: string; mono?: boolean };

type Row = {
  feature: string;
  cells: [Cell, Cell, Cell, Cell, Cell]; // ClipCatalyst, OpusClip, Klap, VEED, Descript
};

const ROWS: Row[] = [
  {
    feature: "Processing speed",
    cells: [
      { kind: "text", value: "~90 sec", mono: true },
      { kind: "text", value: "~12 min", mono: true },
      { kind: "text", value: "~18 min", mono: true },
      { kind: "text", value: "10–20 min", mono: true },
      { kind: "text", value: "Manual" },
    ],
  },
  {
    feature: "Chat-based editing",
    cells: [
      { kind: "yes" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
    ],
  },
  {
    feature: "Max export quality",
    cells: [
      { kind: "text", value: "4K", mono: true },
      { kind: "text", value: "1080p", mono: true },
      { kind: "text", value: "1080p", mono: true },
      { kind: "text", value: "1080p", mono: true },
      { kind: "text", value: "1080p", mono: true },
    ],
  },
  {
    feature: "XML export (Premiere / Resolve / Final Cut)",
    cells: [
      { kind: "yes" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
    ],
  },
  {
    feature: "Purpose-built for clipping",
    cells: [
      { kind: "yes" },
      { kind: "yes" },
      { kind: "yes" },
      { kind: "no" },
      { kind: "no" },
    ],
  },
  {
    feature: "All features on every paid plan",
    cells: [
      { kind: "yes" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
      { kind: "no" },
    ],
  },
];

const GAPS: { name: string; gap: string }[] = [
  {
    name: "OpusClip",
    gap: "Key features sit behind higher Pro tiers, and processing runs 10–20 minutes per video.",
  },
  {
    name: "Klap",
    gap: "Frame detection misses speakers, the editor runs slow, and export options are limited.",
  },
  {
    name: "VEED",
    gap: "A general-purpose video suite — clipping is one feature among many, not the focus.",
  },
  {
    name: "Descript",
    gap: "Deep editing power, but a steep learning curve before your first clip ships.",
  },
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
          eyebrow="Why switch"
          title={
            <>
              Built to beat <span className="whitespace-nowrap">&ldquo;good enough&rdquo;</span>
            </>
          }
          lede="Every clipping tool promises viral moments. Here is where they actually stand — speed, quality, and what your plan really unlocks."
        />

        <div className="mt-14 overflow-x-auto rounded-2xl border border-line bg-ink-900/60 md:mt-16">
          <table className="w-full min-w-[640px] text-left">
            <caption className="sr-only">
              Feature comparison: ClipCatalyst versus OpusClip, Klap, VEED, and
              Descript
            </caption>
            <thead>
              <tr className="border-b border-line-strong">
                <th
                  scope="col"
                  className="px-5 py-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-zinc-500"
                >
                  Feature
                </th>
                <th scope="col" className={`px-4 py-4 text-center ${highlightCell}`}>
                  <Badge tone="brand">ClipCatalyst</Badge>
                </th>
                <th
                  scope="col"
                  className="px-4 py-4 text-center text-sm font-medium text-zinc-400"
                >
                  OpusClip
                </th>
                <th
                  scope="col"
                  className="px-4 py-4 text-center text-sm font-medium text-zinc-400"
                >
                  Klap
                </th>
                <th
                  scope="col"
                  className="px-4 py-4 text-center text-sm font-medium text-zinc-400"
                >
                  VEED
                </th>
                <th
                  scope="col"
                  className="px-4 py-4 text-center text-sm font-medium text-zinc-400"
                >
                  Descript
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
        <p className="mt-4 text-xs text-zinc-600">
          Based on publicly listed plans and typical processing times for a
          60-minute source video.
        </p>

        <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {GAPS.map(({ name, gap }) => (
            <Card key={name} className="p-5">
              <p className="font-mono text-[0.65rem] font-medium uppercase tracking-[0.2em] text-zinc-500">
                Where it falls short
              </p>
              <h3 className="mt-2 font-display text-base font-semibold text-white">
                {name}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-zinc-400">{gap}</p>
            </Card>
          ))}
        </div>
      </Container>
    </section>
  );
}
