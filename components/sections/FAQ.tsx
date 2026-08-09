import { Container, SectionHeading } from "@/components/ui";

const faqs: { q: string; a: string }[] = [
  {
    q: "How is 90-second processing actually possible?",
    a: "We run the whole pipeline — transcription, scoring, reframing, captioning — on optimized GPU clusters in parallel, not one stage at a time. Intelligent caching means repeat runs on the same footage are even faster. Median time from upload to first finished clip: under 90 seconds, versus 10–20 minutes for OpusClip or Klap.",
  },
  {
    q: "How accurate is the clip selection?",
    a: "Every candidate moment is scored 0–100 by the virality engine using audio analysis (laughter spikes, dramatic pauses), transcript semantics, and platform trend data — and each clip ships with concrete improvement tips, so nothing is a black box. When it does miss, one chat message like “cut in two seconds later” fixes it.",
  },
  {
    q: "What formats and lengths can I upload?",
    a: "MP4, MOV, AVI, and WebM up to 10GB per file — enough for a 3-hour podcast. You can also paste a YouTube link or connect Drive and Dropbox. Output clips come in 15, 30, or 60-second cuts, reframed to 9:16 with face tracking.",
  },
  {
    q: "Do exported clips have a watermark?",
    a: "Only on the Free tier — $0 for 3 clips a month at 720p, available at launch. Starter at $19/mo removes the watermark and unlocks 1080p plus your brand kit; Pro at $49/mo exports up to 4K.",
  },
  {
    q: "How does XML export work with Premiere or Resolve?",
    a: "Pro plans export a timeline XML alongside every clip — cuts, reframe keyframes, and caption layers intact. Open it in Premiere, Resolve, or Final Cut and keep working on your own timeline. The AI does the first 95%; you keep full control of the last 5%.",
  },
  {
    q: "Which platforms can I publish to?",
    a: "One-click publish to TikTok, YouTube Shorts, and Instagram Reels, with platform-specific optimization applied per destination. You can also schedule posts, download files up to 4K, or share a review link with your team before anything goes live.",
  },
  {
    q: "Who owns my footage?",
    a: "You do — always. Source video is never shared or resold, and finished clips inform the virality model only in aggregate, with a one-click opt-out. Delete a project and it is gone from our servers.",
  },
];

export default function FAQ() {
  return (
    <section id="faq" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          eyebrow="Questions"
          title="Straight answers, no fine print"
          lede="The seven things creators ask before their first upload."
        />
        <div className="mx-auto mt-14 max-w-3xl divide-y divide-line rounded-2xl border border-line bg-ink-900/40">
          {faqs.map((faq) => (
            <details key={faq.q} className="group px-6 sm:px-8">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-6 py-5 [&::-webkit-details-marker]:hidden">
                <span className="text-sm font-medium text-white sm:text-base">
                  {faq.q}
                </span>
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 16 16"
                  fill="none"
                  aria-hidden
                  className="shrink-0 text-zinc-500 transition-transform duration-200 group-open:rotate-180 group-open:text-brand-400"
                >
                  <path
                    d="M3.5 6 8 10.5 12.5 6"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </summary>
              <p className="pb-6 pr-2 text-sm leading-relaxed text-zinc-400 sm:pr-10">
                {faq.a}
              </p>
            </details>
          ))}
        </div>
      </Container>
    </section>
  );
}
