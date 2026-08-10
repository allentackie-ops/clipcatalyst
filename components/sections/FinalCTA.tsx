"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { Button, Container, Eyebrow } from "@/components/ui";

function WaitlistForm() {
  const [submitted, setSubmitted] = useState(false);

  function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitted(true);
  }

  if (submitted) {
    return (
      <div
        role="status"
        className="mx-auto mt-10 flex w-full max-w-xl items-center justify-center gap-3 rounded-full border border-signal-500/30 bg-signal-500/10 px-6 py-4"
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 20 20"
          fill="none"
          aria-hidden
          className="shrink-0 text-signal-400"
        >
          <circle
            cx="10"
            cy="10"
            r="9"
            stroke="currentColor"
            strokeWidth="1.5"
          />
          <path
            d="m6 10.2 2.6 2.6L14 7.4"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <p className="text-sm font-medium text-white sm:text-base">
          You&rsquo;re on the list — first clip&rsquo;s on us.
        </p>
      </div>
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mx-auto mt-10 flex w-full max-w-xl flex-col gap-3 sm:flex-row"
    >
      <label htmlFor="waitlist-email" className="sr-only">
        Email address
      </label>
      <input
        id="waitlist-email"
        type="email"
        required
        placeholder="you@studio.com"
        autoComplete="email"
        className="w-full flex-1 rounded-full border border-line-strong bg-white/5 px-5 py-3.5 text-sm text-white outline-none transition-colors placeholder:text-zinc-500 focus:border-brand-400/70 focus:ring-2 focus:ring-brand-400/40"
      />
      <Button type="submit" size="lg" className="shrink-0">
        Join the waitlist
      </Button>
    </form>
  );
}

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
            <Eyebrow>Get early access</Eyebrow>
            <h2 className="mx-auto max-w-2xl font-display text-3xl font-semibold tracking-tight text-white sm:text-4xl md:text-5xl md:leading-[1.08]">
              Your next viral clip is already in your footage.
            </h2>
            <p className="mx-auto mt-5 max-w-xl text-base leading-relaxed text-zinc-400 sm:text-lg">
              Paste a link. Ninety seconds later, you&rsquo;re posting.
            </p>
            <WaitlistForm />
            <p className="mt-6 font-mono text-xs tracking-wide text-zinc-500">
              Free tier at launch · No credit card · First clip in 90 seconds
            </p>
            <p className="mt-4 text-sm">
              <Link
                href="/studio"
                className="text-brand-300 transition-colors hover:text-white"
              >
                Can&rsquo;t wait? Try the Studio beta now &rarr;
              </Link>
            </p>
          </div>
        </div>
      </Container>
    </section>
  );
}
