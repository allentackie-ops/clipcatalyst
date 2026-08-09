import { Badge, Button, Card, Container, SectionHeading } from "@/components/ui";

type Tier = {
  name: string;
  tagline: string;
  price: string;
  per?: string;
  fromPrefix?: boolean;
  features: string[];
  limitations?: string[];
  cta: string;
  href: string;
  highlight?: boolean;
};

const tiers: Tier[] = [
  {
    name: "Free",
    tagline: "Test the engine.",
    price: "$0",
    features: ["3 clips / month"],
    limitations: ["720p export, watermarked"],
    cta: "Get early access",
    href: "#waitlist",
  },
  {
    name: "Starter",
    tagline: "For solo creators.",
    price: "$19",
    per: "/mo",
    features: [
      "30 clips / month",
      "1080p export",
      "No watermark",
      "Brand kit",
    ],
    cta: "Reserve Starter",
    href: "#waitlist",
  },
  {
    name: "Pro",
    tagline: "For serious publishers.",
    price: "$49",
    per: "/mo",
    features: [
      "100 clips / month",
      "4K export",
      "Chat-based editing",
      "AI hook generator",
      "Auto B-roll",
    ],
    cta: "Reserve Pro",
    href: "#waitlist",
    highlight: true,
  },
  {
    name: "Enterprise",
    tagline: "For teams & agencies.",
    price: "$99",
    per: "/mo",
    fromPrefix: true,
    features: [
      "Unlimited clips",
      "API access",
      "Team workspace",
      "Custom branding",
    ],
    cta: "Talk to us",
    href: "#waitlist",
  },
];

function CheckIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="mt-0.5 shrink-0"
    >
      <circle cx="8" cy="8" r="8" className="fill-brand-500/15" />
      <path
        d="m4.5 8.2 2.3 2.3 4.7-5"
        stroke="#a78bfa"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function DashIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="mt-0.5 shrink-0 text-zinc-600"
    >
      <path
        d="M4.5 8h7"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function TierBody({ tier }: { tier: Tier }) {
  return (
    <div className="flex h-full flex-col p-6 lg:p-7">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-display text-lg font-semibold tracking-tight text-white">
          {tier.name}
        </h3>
        {tier.highlight ? <Badge tone="ember">Most popular</Badge> : null}
      </div>
      <p className="mt-1 text-sm text-zinc-500">{tier.tagline}</p>

      <div className="mt-6 flex items-baseline gap-1">
        {tier.fromPrefix ? (
          <span className="font-mono text-xs uppercase tracking-wider text-zinc-500">
            from
          </span>
        ) : null}
        <span className="font-display text-4xl font-semibold tracking-tight text-white lg:text-[2.75rem] lg:leading-none">
          {tier.price}
        </span>
        {tier.per ? (
          <span className="font-mono text-sm text-zinc-500">{tier.per}</span>
        ) : null}
      </div>

      <ul className="mt-6 flex flex-col gap-3 border-t border-line pt-6">
        {tier.features.map((feature) => (
          <li
            key={feature}
            className="flex items-start gap-2.5 text-sm text-zinc-300"
          >
            <CheckIcon />
            <span>{feature}</span>
          </li>
        ))}
        {tier.limitations?.map((limitation) => (
          <li
            key={limitation}
            className="flex items-start gap-2.5 text-sm text-zinc-500"
          >
            <DashIcon />
            <span>{limitation}</span>
          </li>
        ))}
      </ul>

      <div className="mt-auto pt-8">
        <Button
          href={tier.href}
          variant={tier.highlight ? "primary" : "secondary"}
          className="w-full"
        >
          {tier.cta}
        </Button>
      </div>
    </div>
  );
}

export default function Pricing() {
  return (
    <section id="pricing" className="relative py-24 md:py-32">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-24 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/20 blur-[120px]"
      />
      <Container className="relative">
        <SectionHeading
          eyebrow="Pricing"
          title="Start free. Scale when the clips hit."
          lede="Three clips a month on the house — no credit card, no surprise limits at checkout. Upgrade when your pipeline does."
        />

        <div className="mt-14 grid grid-cols-1 items-stretch gap-5 sm:grid-cols-2 lg:mt-16 lg:grid-cols-4 lg:gap-6">
          {tiers.map((tier) =>
            tier.highlight ? (
              <div
                key={tier.name}
                className="relative rounded-2xl bg-gradient-to-b from-brand-400 via-spark-500 to-ember-400 p-[1px] shadow-[0_0_40px_rgba(139,92,246,0.25)] lg:z-10 lg:scale-[1.04]"
              >
                <div className="h-full rounded-[calc(1rem-1px)] bg-ink-850">
                  <TierBody tier={tier} />
                </div>
              </div>
            ) : (
              <Card
                key={tier.name}
                className="transition-colors duration-200 hover:border-line-strong"
              >
                <TierBody tier={tier} />
              </Card>
            ),
          )}
        </div>

        <Card className="mx-auto mt-10 max-w-3xl lg:mt-12">
          <div className="flex flex-col items-start gap-3 px-6 py-5 sm:flex-row sm:items-center sm:gap-4">
            <Badge tone="ember" className="shrink-0">
              Add-on
            </Badge>
            <p className="text-sm leading-relaxed text-zinc-400">
              Need more? <span className="text-white">50 extra credits</span>{" "}
              for <span className="font-mono text-white">$20</span> —{" "}
              <span className="font-mono">1 credit = 1 clip</span>. Credits
              never expire.
            </p>
          </div>
        </Card>
      </Container>
    </section>
  );
}
