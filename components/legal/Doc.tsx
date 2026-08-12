// Shared shell for the two legal documents (/privacy and /terms).
//
// These are DOCUMENTS, not landing sections: the job is a readable measure, a
// clear heading hierarchy and a table of contents somebody can link into —
// platform reviewers cite section anchors, so every section gets a stable id.
// The visual language is the rest of the site's (Container, the display face,
// the violet accent); the decoration is deliberately thin, because a policy
// page that looks designed reads as marketing rather than as a statement.
//
// Both pages are built from a SECTIONS array so the contents list and the body
// are rendered from one source and can never drift apart.

import Link from "next/link";
import type { ReactNode } from "react";
import Footer from "@/components/Footer";
import Navbar from "@/components/Navbar";
import { Container, Eyebrow } from "@/components/ui";

/**
 * Where to write about anything on either page.
 *
 * NOT verified against anything in the codebase — it is the one fact on these
 * pages that no source file can back. The mailbox and its domain must exist
 * before the YouTube / TikTok / Meta applications quote these URLs.
 */
export const CONTACT_EMAIL = "support@clipcatalyst.io";

/**
 * The date both documents carry. One constant, because they were written
 * together and describe the same build; a page edited on its own gets its own
 * date at that point.
 */
export const LAST_UPDATED = "12 August 2026";

/** One numbered section: its anchor, its heading, and its prose. */
export type DocSection = {
  /** Stable anchor — quoted in applications, so it does not change casually. */
  id: string;
  title: string;
  body: ReactNode;
};

/** A paragraph of document body copy. */
export function P({ children }: { children: ReactNode }) {
  return <p className="text-[0.9375rem] leading-[1.75] text-zinc-300">{children}</p>;
}

/** A lead-in for a passage inside a section. */
export function Sub({ children }: { children: ReactNode }) {
  return (
    <h3 className="pt-2 font-display text-base font-semibold tracking-tight text-white">
      {children}
    </h3>
  );
}

/** A bulleted list. Items are nodes so they can carry emphasis and links. */
export function List({ items }: { items: ReactNode[] }) {
  return (
    <ul className="flex flex-col gap-2.5">
      {items.map((item, i) => (
        <li
          key={i}
          className="relative pl-5 text-[0.9375rem] leading-[1.75] text-zinc-300"
        >
          <span
            aria-hidden
            className="absolute left-0 top-[0.72em] h-1.5 w-1.5 rounded-full bg-brand-400/80"
          />
          {item}
        </li>
      ))}
    </ul>
  );
}

/** The handful of statements that carry the most weight, set apart. */
export function Note({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-ink-850/80 px-5 py-4">
      <p className="text-[0.9375rem] leading-[1.7] text-zinc-200">{children}</p>
    </div>
  );
}

/** Emphasis inside body copy — brighter, never bolder than it needs to be. */
export function Em({ children }: { children: ReactNode }) {
  return <strong className="font-medium text-white">{children}</strong>;
}

/** `mailto:` for CONTACT_EMAIL, styled like the rest of the document links. */
export function MailLink() {
  return (
    <a
      href={`mailto:${CONTACT_EMAIL}`}
      className="text-brand-300 underline decoration-brand-400/40 underline-offset-4 transition-colors hover:text-brand-200"
    >
      {CONTACT_EMAIL}
    </a>
  );
}

/**
 * An in-site link inside body copy (the two documents point at each other).
 *
 * A route link goes through next/link, which is what applies `basePath` — on
 * GitHub Pages the site lives under /clipcatalyst, and a raw `<a href="/terms">`
 * exports as a 404 there. A pure `#section` anchor stays a plain anchor: it is
 * the same document, and a client-side navigation to it would only cost a
 * re-render to end up in the same place.
 */
export function DocLink({ href, children }: { href: string; children: ReactNode }) {
  const className =
    "text-brand-300 underline decoration-brand-400/40 underline-offset-4 transition-colors hover:text-brand-200";
  if (href.startsWith("#")) {
    return (
      <a href={href} className={className}>
        {children}
      </a>
    );
  }
  return (
    <Link href={href} className={className}>
      {children}
    </Link>
  );
}

/** A link off the site — the platform policies these documents must cite. */
export function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-brand-300 underline decoration-brand-400/40 underline-offset-4 transition-colors hover:text-brand-200"
    >
      {children}
    </a>
  );
}

function num(index: number): string {
  return String(index + 1).padStart(2, "0");
}

/**
 * The whole page: nav, header, contents, body, footer.
 *
 * `scroll-mt-24` on every section clears the fixed 4 rem bar, so a link from
 * the contents list (or from an application form) lands with the heading
 * visible rather than tucked under the navbar.
 */
export default function DocPage({
  eyebrow,
  title,
  lede,
  sections,
}: {
  eyebrow: string;
  title: string;
  lede: string;
  sections: DocSection[];
}) {
  return (
    <>
      <Navbar />
      <main id="main" className="pt-16">
        <article className="relative py-16 md:py-24">
          <div
            aria-hidden
            className="pointer-events-none absolute -top-32 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]"
          />
          <Container className="relative">
            {/* Same measure as the body column below, so the title, the lede
                and the first paragraph of the document share a left AND a
                right edge on a wide screen. */}
            <header className="max-w-[36rem]">
              <Eyebrow>{eyebrow}</Eyebrow>
              <h1 className="font-display text-3xl font-semibold tracking-tight text-white sm:text-4xl md:text-[2.75rem] md:leading-[1.1]">
                {title}
              </h1>
              <p className="mt-5 text-base leading-relaxed text-zinc-400">
                {lede}
              </p>
              <p className="mt-6 font-mono text-xs uppercase tracking-[0.18em] text-zinc-500">
                Last updated {LAST_UPDATED}
              </p>
            </header>

            {/* Contents FIRST in the DOM — on a phone it belongs at the top of
                the document, and it is the first thing a screen reader should
                meet. On a wide screen `order` moves it to the right-hand rail,
                which puts the prose back on the same left edge as the title
                instead of indenting the whole document by the rail's width. */}
            <div className="mt-12 grid gap-12 md:mt-16 lg:grid-cols-[minmax(0,36rem)_14rem] lg:gap-16">
              <nav
                aria-label="On this page"
                className="lg:sticky lg:top-24 lg:order-2 lg:self-start"
              >
                <h2 className="font-mono text-xs uppercase tracking-[0.18em] text-zinc-500">
                  Contents
                </h2>
                <ol className="mt-4 flex flex-col gap-2 border-l border-line pl-4">
                  {sections.map((section, i) => (
                    <li key={section.id}>
                      <a
                        href={`#${section.id}`}
                        className="flex gap-2.5 text-sm leading-snug text-zinc-400 transition-colors hover:text-white"
                      >
                        <span className="font-mono text-xs text-zinc-600">
                          {num(i)}
                        </span>
                        <span>{section.title}</span>
                      </a>
                    </li>
                  ))}
                </ol>
              </nav>

              <div className="flex flex-col gap-14 lg:order-1">
                {sections.map((section, i) => (
                  <section
                    key={section.id}
                    id={section.id}
                    className="scroll-mt-24"
                  >
                    <h2 className="flex gap-3 font-display text-xl font-semibold tracking-tight text-white sm:text-2xl">
                      <span className="pt-1 font-mono text-sm font-normal text-brand-400">
                        {num(i)}
                      </span>
                      <span>{section.title}</span>
                    </h2>
                    <div className="mt-4 flex flex-col gap-4">{section.body}</div>
                  </section>
                ))}
              </div>
            </div>
          </Container>
        </article>
      </main>
      <Footer />
    </>
  );
}
