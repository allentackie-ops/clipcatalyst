"use client";

import { useState } from "react";
import Link from "next/link";
import { Button, Container, Logo } from "@/components/ui";

// Section anchors are root-relative ("/#pricing", not "#pricing") because the
// bar also sits on /account, where those sections don't exist — from there the
// link has to go home first.
const links = [
  { href: "/#virality-engine", label: "Virality Engine" },
  { href: "/#features", label: "Features" },
  { href: "/#pricing", label: "Pricing" },
  { href: "/#compare", label: "Compare" },
  { href: "/studio", label: "Studio" },
  { href: "/demo", label: "Product tour" },
  { href: "/account", label: "Account" },
];

export default function Navbar() {
  const [open, setOpen] = useState(false);

  return (
    <header className="fixed inset-x-0 top-0 z-50 border-b border-line bg-ink-950/70 backdrop-blur-xl">
      <Container className="flex h-16 items-center justify-between">
        <Link href="/" aria-label="ClipCatalyst home">
          <Logo />
        </Link>
        {/* Seven links + the CTA no longer fit at md — the inline bar starts
            at lg, and md falls back to the same menu the phone gets. */}
        <nav className="hidden items-center gap-7 lg:flex" aria-label="Main">
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
          <Button href="/studio">Get started</Button>
          <button
            type="button"
            aria-label="Open menu"
            aria-expanded={open}
            aria-controls="mobile-menu"
            onClick={() => setOpen((o) => !o)}
            className="flex h-10 w-10 items-center justify-center rounded-lg text-zinc-300 transition-colors hover:text-white lg:hidden"
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              aria-hidden
            >
              {open ? (
                <path d="M6 6l12 12M18 6 6 18" />
              ) : (
                <path d="M4 7h16M4 12h16M4 17h16" />
              )}
            </svg>
          </button>
        </div>
      </Container>
      {open && (
        <nav
          id="mobile-menu"
          aria-label="Main"
          className="absolute inset-x-0 top-16 border-b border-line bg-ink-950/95 backdrop-blur-xl lg:hidden"
        >
          <Container>
            {links.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                onClick={() => setOpen(false)}
                className="block border-t border-line py-3 text-sm text-zinc-400 transition-colors first:border-t-0 hover:text-white"
              >
                {l.label}
              </Link>
            ))}
          </Container>
        </nav>
      )}
    </header>
  );
}
