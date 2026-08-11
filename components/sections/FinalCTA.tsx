import Link from "next/link";
import { Button, Container, Eyebrow } from "@/components/ui";

/**
 * Closing CTA. This used to be a waitlist form that collected an email and did
 * nothing with it — no storage, no send, just a success message. The product
 * works today, so the ask is now the product itself.
 *
 * The anchor id stays `waitlist` ONLY as a landing pad for links already in
 * the wild; nothing on the site points at it any more.
 */
export default function FinalCTA() {
  return (
    <section id="waitlist" className="relative py-24 md:py-32">
      <Container>
        <div className="relative overflow-hidden rounded-3xl border border-line-strong bg-ink-900 px-6 py-16 text-center sm:px-12 md:py-24">
          <div
            aria-hidden
            className="pointer-events-none absolute -top-32 left-1/2 h-96 w-[42rem] max-w-full -translate-x-1/2 rounded-full bg-brand-600/25 blur-[120px]"
          />
          <div className="relative">
            <Eyebrow>Start clipping</Eyebrow>
            <h2 className="mx-auto max-w-2xl font-display text-3xl font-semibold tracking-tight text-white sm:text-4xl md:text-5xl md:leading-[1.08]">
              Your next viral clip is already in your footage.
            </h2>
            <p className="mx-auto mt-5 max-w-xl text-base leading-relaxed text-zinc-400 sm:text-lg">
              Pick a video. Ninety seconds later, you&rsquo;re posting.
            </p>

            <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row sm:gap-4">
              <Button href="/studio" size="lg">
                Get started
              </Button>
              <Button href="/demo" variant="secondary" size="lg">
                Watch the tour &rarr;
              </Button>
            </div>

            <p className="mt-6 font-mono text-xs tracking-wide text-zinc-500">
              Free · No account · Your video never leaves your device
            </p>
            <p className="mt-4 text-sm">
              <Link
                href="/#pricing"
                className="text-brand-300 transition-colors hover:text-white"
              >
                See what the paid plans add &rarr;
              </Link>
            </p>
          </div>
        </div>
      </Container>
    </section>
  );
}
