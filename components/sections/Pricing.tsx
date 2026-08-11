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
  /**
   * Where the button goes. Free opens the Studio, which needs no account and
   * works immediately. The cloud tiers open /account, which reports the real
   * status: with no cloud API configured it says accounts aren't live, and
   * with billing switched off checkout is unavailable. Nothing here promises
   * a purchase, because no purchase can currently complete.
   */
  href: string;
  highlight?: boolean;
};

/**
 * Two engines, stated separately on purpose.
 *
 * The browser Studio is the shipped free product: it runs entirely in-tab, has
 * no quota, and needs no account. The cloud tiers mirror PLANS in
 * api/clipcatalyst_api/plans.py exactly — clips per month, max height, and
 * whether a watermark is required. Prices are deliberately absent: no amount
 * is configured anywhere in this product, so quoting one would be a guess.
 */
const tiers: Tier[] = [
  {
    name: "Free",
    tagline: "Everything that ships today.",
    price: "$0",
    features: [
      "Browser Studio: unlimited runs, no account",
      "1–3 clips per run · 15, 30, or 60s",
      "Export up to 1080p (1080×1920)",
      "Chat editing, hook suggestions, 0–100 scores",
      "Cloud allowance: 3 clips / month at 720p",
    ],
    limitations: [
      "Watermark on every clip",
      "Desktop only · sources up to 20 min / 1.4 GB",
    ],
    cta: "Open Studio",
    href: "/studio",
  },
  {
    name: "Starter",
    tagline: "Cloud renders, watermark off.",
    price: "TBA",
    features: [
      "30 cloud clips / month",
      "1080p cloud export",
      "No watermark",
      "Browser Studio stays free and unlimited",
    ],
    limitations: ["Cloud rendering is not live yet"],
    cta: "Check availability",
    href: "/account?plan=starter",
  },
  {
    name: "Pro",
    tagline: "The highest cloud ceiling.",
    price: "TBA",
    features: [
      "100 cloud clips / month",
      "Up to 4K cloud export",
      "No watermark",
      "Browser Studio stays free and unlimited",
    ],
    limitations: [
      "Cloud rendering is not live yet",
      "4K has not yet been run on hardware",
    ],
    cta: "Check availability",
    href: "/account?plan=pro",
    highlight: true,
  },
  {
    name: "Enterprise",
    tagline: "Volume without a monthly cap.",
    price: "TBA",
    features: [
      "Unlimited cloud clips",
      "Same 4K ceiling as Pro",
      "No watermark",
      "Browser Studio stays free and unlimited",
    ],
    limitations: ["Cloud rendering is not live yet"],
    cta: "Check availability",
    href: "/account",
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
  // Single destination per tier now — see the Tier type.
  const { cta, href } = tier;
  return (
    <div className="flex h-full flex-col p-6 lg:p-7">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-display text-lg font-semibold tracking-tight text-white">
          {tier.name}
        </h3>
        {tier.highlight ? <Badge tone="ember">Recommended</Badge> : null}
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
          href={href}
          variant={tier.highlight ? "primary" : "secondary"}
          className="w-full"
        >
          {cta}
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
          title="Free is the product. Cloud is next."
          lede="Unlimited clipping in your browser — free, account-less, watermarked — is what ships today. The cloud tiers below are the entitlements the pipeline is built to enforce; it is not switched on yet, and nothing on this page can be bought."
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
              Status
            </Badge>
            <p className="text-sm leading-relaxed text-zinc-400">
              <span className="text-white">Cloud rendering is not live.</span>{" "}
              Accounts and billing are switched off, so no plan above can be
              purchased today and no price is set. The browser Studio needs
              none of it — it is free, unlimited, and works right now.
            </p>
          </div>
        </Card>
      </Container>
    </section>
  );
}
