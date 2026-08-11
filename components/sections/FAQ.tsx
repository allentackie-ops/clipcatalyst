import { Container, SectionHeading } from "@/components/ui";

const faqs: { q: string; a: string }[] = [
  {
    q: "How long does a run actually take?",
    a: "In the browser, render time tracks the clips you asked for rather than the video you fed in: clips are captured in real time, so two 30-second clips take at least a minute to write, plus transcription up front. There is no upload and no queue, so that is the entire wait. The cloud pipeline is designed for roughly 2–3 minutes on a T4 GPU — that is an engineering target, not a measurement. It has never been run on real hardware, so we do not quote a median.",
  },
  {
    q: "How accurate is the clip selection?",
    a: "Every candidate window is scored 0–100 on six components: hook strength, idea density, vocal energy, whether it ends on a finished thought, whether it starts inside the intro, and how much dead air it carries. The scoring is deterministic — the same video always yields the same clips — and each clip ships with the reason it scored that way plus the single fix that would help it most. When it misses, one message like “start 2 seconds later” moves it.",
  },
  {
    q: "What can I feed it, and what comes out?",
    a: "MP4, MOV, and WebM. The browser holds the whole file in memory, so sources cap at 20 minutes and 1.4 GB — trim a long episode first. Out come 1 to 3 clips per run at 15, 30, or 60 seconds, reframed to 9:16 with face tracking, at up to 1080×1920. There is no YouTube-link import and no Drive or Dropbox connection: you pick a file off your own disk.",
  },
  {
    q: "Do exported clips have a watermark?",
    a: "Yes. Every clip the browser Studio produces carries the ClipCatalyst mark, signed in or not. Turning it off is an entitlement on the paid cloud plans, and cloud rendering is not switched on yet — so today the honest answer is watermarked, unlimited, and free.",
  },
  {
    q: "What can't it do yet?",
    a: "No timeline or XML export to Premiere, Resolve, or Final Cut. No B-roll, no brand kit, no custom fonts or colors. No team accounts, analytics, or A/B testing. No posting or scheduling. No public API. If a capability is not described on this page, assume it is not built — we would rather you find that out here than after paying.",
  },
  {
    q: "Which platforms can I publish to?",
    a: "None directly — there is no posting or scheduling integration. You download the MP4, or press share, which opens your device's native share sheet; on a phone that puts TikTok, YouTube, and Instagram one tap away. Clips come out 9:16, so they fit all three without further work.",
  },
  {
    q: "What does the free Studio do right now?",
    a: "Everything on this page that is not labelled cloud, with unlimited runs and no account: Whisper transcription, speaker separation, moment scoring, face-tracked 9:16 reframing, word-level captions, hook suggestions, and chat editing — all inside the tab. It needs a desktop browser (Chrome, Edge, or Firefox). Phones and tablets get a card explaining why, rather than a run that dies halfway through.",
  },
  {
    q: "Who owns my footage?",
    a: "You do. In the browser Studio the question barely arises: the file never leaves your device, there is no account, and nothing is transmitted to us — so there is nothing for us to hold, share, or resell. When cloud rendering goes live, uploads and rendered clips are reaped automatically 48 hours after a job. Nothing trains a model either way; the scoring engine is a fixed weighting, not something that learns from your video.",
  },
];

export default function FAQ() {
  return (
    <section id="faq" className="relative py-24 md:py-32">
      <Container>
        <SectionHeading
          eyebrow="Questions"
          title="Straight answers, no fine print"
          lede="The eight things creators ask before their first upload."
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
