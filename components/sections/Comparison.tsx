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
      { kind: "text", value: "10-20 min", mono: true },
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
      { kind: "text", value: "4K (paid)" },
      { kind: "text", value: "4K (paid)" },
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
    feature: "AI hook variants per clip",
    cells: [
      { kind: "text", value: "5", mono: true },
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
    gap: "Key features sit behind higher Pro tiers, and processing runs 10 to 20 minutes per video.",
  },
  {
    name: "Klap",
    gap: "Frame detection misses speakers, the editor runs slow, and export options are limited.",
  },
  {
    name: "VEED",
    gap: "A general-purpose video suite where clipping is one feature among many, not the focus.",
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

/**
 * "Not supported". A cross rather than the usual horizontal rule, because a
 * table of dash glyphs reads as punctuation scattered through the page, which
 * is the thing this design pass exists to remove.
 */
function NoMark() {
  return (
    <span className="inline-flex items-center justify-center">
      <svg
        width="16"
        height="16"
        viewBox="0 0 20 20"
        fill="none"
        aria-hidden
        className="text-zinc-600"
      >
        <path
          d="M6 6l8 8M14 6l-8 8"
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
  if (cell.kind === "no") return <NoMark />;
  const tone = highlight ? "font-semibold text-white" : "text-zinc-400";
  const mono = cell.mono ? "font-mono" : "";
  return <span className={`text-sm ${mono} ${tone}`.trim()}>{cell.value}</span>;
}

const highlightCell = "border-x border-brand-500/20 bg-brand-500/[0.07]";

/** Column order after the feature name, mirroring Row["cells"] indices 1..4. */
const RIVALS = ["OpusClip", "Klap", "VEED", "Descript"] as const;

/**
 * The phone view of ROWS: one card per feature, our answer stated plainly and
 * the four rivals underneath. Same data as the table, no sideways scroll, and
 * every value sits inside the viewport at 320 px.
 */
function MobileComparison() {
  return (
    <div className="mt-10 flex flex-col gap-3 md:hidden">
      {ROWS.map((row) => (
        <div
          key={row.feature}
          className="rounded-2xl border border-line bg-ink-900/60 p-4"
        >
          <p className="text-sm text-zinc-300">{row.feature}</p>

          <div className="mt-3 flex items-center gap-3 rounded-xl border border-brand-500/25 bg-brand-500/[0.07] px-3 py-2.5">
            <span className="text-sm font-semibold text-white">ClipCatalyst</span>
            <span className="ml-auto shrink-0">
              <CellValue cell={row.cells[0]} highlight />
            </span>
          </div>

          <dl className="mt-2 grid grid-cols-2 gap-x-4">
            {RIVALS.map((name, i) => (
              <div key={name} className="flex items-center gap-2 py-1.5">
                <dt className="min-w-0 truncate text-xs text-zinc-500">{name}</dt>
                <dd className="ml-auto shrink-0">
                  <CellValue cell={row.cells[i + 1]} highlight={false} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

export default function Comparison() {
  return (
    <section id="compare" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          title={
            <>
              Built to beat <span className="whitespace-nowrap">&ldquo;good enough&rdquo;</span>
            </>
          }
          lede="Every clipping tool promises viral moments. Here is where they actually stand on speed, quality, and what your plan really unlocks."
        />

        {/* Six columns need 640 px, so on a phone the table is hidden rather
            than parked in a sideways scroller: a scroller technically contains
            it, but it still reads as a page sliced off mid-word at the screen
            edge. MobileComparison below renders the same ROWS in a shape that
            fits. */}
        <div className="mt-14 hidden md:mt-16 md:block">
          <div className="overflow-x-auto rounded-2xl border border-line bg-ink-900/60">
            <table className="w-full min-w-[640px] text-left">
              <caption className="sr-only">
                Feature comparison: ClipCatalyst versus OpusClip, Klap, VEED,
                and Descript
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
        </div>
        <MobileComparison />

        <p className="mt-4 text-xs text-zinc-400">
          Based on publicly listed plans and typical processing times for a
          60-minute source video.
        </p>

        {/* One label for the group instead of the same mono-uppercase kicker
            repeated on all four cards, which read as decoration. */}
        <h3 className="mt-14 font-display text-lg font-semibold text-white">
          Where each one falls short
        </h3>

        <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {GAPS.map(({ name, gap }) => (
            <Card key={name} className="p-5">
              <h4 className="font-display text-base font-semibold text-white">
                {name}
              </h4>
              <p className="mt-2 text-sm leading-relaxed text-zinc-400">{gap}</p>
            </Card>
          ))}
        </div>
      </Container>
    </section>
  );
}
