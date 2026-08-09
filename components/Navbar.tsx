import Link from "next/link";
import { Button, Container, Logo } from "@/components/ui";

const links = [
  { href: "#virality-engine", label: "Virality Engine" },
  { href: "#features", label: "Features" },
  { href: "#pricing", label: "Pricing" },
  { href: "#compare", label: "Compare" },
  { href: "/demo", label: "Live demo" },
];

export default function Navbar() {
  return (
    <header className="fixed inset-x-0 top-0 z-50 border-b border-line bg-ink-950/70 backdrop-blur-xl">
      <Container className="flex h-16 items-center justify-between">
        <Link href="/" aria-label="ClipCatalyst home">
          <Logo />
        </Link>
        <nav className="hidden items-center gap-7 md:flex" aria-label="Main">
          {links.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="text-sm text-zinc-400 transition-colors hover:text-white"
            >
              {l.label}
            </Link>
          ))}
        </nav>
        <div className="flex items-center gap-3">
          <Button href="/demo" variant="ghost" className="hidden sm:inline-flex">
            Sign in
          </Button>
          <Button href="#waitlist">Start free</Button>
        </div>
      </Container>
    </header>
  );
}
