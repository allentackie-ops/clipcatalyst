import Link from "next/link";
import { Container, Logo } from "@/components/ui";

const columns: { title: string; links: { label: string; href: string }[] }[] = [
  {
    title: "Product",
    links: [
      { label: "Virality Engine", href: "#virality-engine" },
      { label: "Chat editing", href: "#chat-editing" },
      { label: "Features", href: "#features" },
      { label: "Pricing", href: "#pricing" },
      { label: "Studio (beta)", href: "/studio" },
      { label: "Product tour", href: "/demo" },
    ],
  },
  {
    title: "Compare",
    links: [
      { label: "vs. OpusClip", href: "#compare" },
      { label: "vs. Klap", href: "#compare" },
      { label: "vs. VEED", href: "#compare" },
      { label: "vs. Descript", href: "#compare" },
    ],
  },
  {
    title: "Resources",
    links: [
      { label: "How it works", href: "#how-it-works" },
      { label: "Under the hood", href: "#pipeline" },
      { label: "FAQ", href: "#faq" },
      { label: "Pricing", href: "#pricing" },
    ],
  },
  {
    title: "Company",
    links: [
      { label: "About", href: "#" },
      { label: "Blog", href: "#" },
      { label: "Affiliates (20% lifetime)", href: "#" },
      { label: "Contact", href: "#" },
      { label: "Privacy Policy", href: "/privacy" },
      { label: "Terms of Service", href: "/terms" },
    ],
  },
];

// Also in the bottom bar, not only in the Company column: these two URLs go
// into the YouTube, TikTok and Meta applications, and the bottom bar is where
// a reviewer (and everybody else) looks for them.
const legal: { label: string; href: string }[] = [
  { label: "Privacy", href: "/privacy" },
  { label: "Terms", href: "/terms" },
];

export default function Footer() {
  return (
    <footer className="border-t border-line bg-ink-900/60">
      <Container className="py-14">
        <div className="grid gap-10 md:grid-cols-[1.4fr_repeat(4,1fr)]">
          <div>
            <Logo />
            <p className="mt-4 max-w-xs text-sm leading-relaxed text-zinc-500">
              Studio-quality AI clips in 90 seconds. Built for podcasters,
              streamers, and the teams behind them.
            </p>
          </div>
          {columns.map((col) => (
            <div key={col.title}>
              <h3 className="mb-4 text-sm font-semibold text-white">
                {col.title}
              </h3>
              <ul className="space-y-2.5">
                {col.links.map((l) => (
                  <li key={l.label}>
                    <Link
                      href={l.href}
                      className="text-sm text-zinc-400 transition-colors hover:text-zinc-200"
                    >
                      {l.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="mt-12 flex flex-col items-center gap-4 border-t border-line pt-8 sm:flex-row sm:justify-between">
          <p className="text-xs text-zinc-500">
            © 2026 ClipCatalyst. All rights reserved.
          </p>
          <nav aria-label="Legal" className="flex items-center gap-5">
            {legal.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                className="text-xs text-zinc-400 transition-colors hover:text-zinc-200"
              >
                {l.label}
              </Link>
            ))}
          </nav>
          <p className="text-xs text-zinc-500">
            Made for creators who&apos;d rather create.
          </p>
        </div>
      </Container>
    </footer>
  );
}
