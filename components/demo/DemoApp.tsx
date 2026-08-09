"use client";

import { useEffect, useState } from "react";
import { CLIPS } from "./data";
import ClipCard, { ProcessingCard } from "./ClipCard";
import DetailPanel from "./DetailPanel";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import { IconChevronDown, IconInfo, IconX } from "./icons";

export default function DemoApp() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showBanner, setShowBanner] = useState(true);
  const [scores, setScores] = useState<Record<string, number>>(() =>
    Object.fromEntries(CLIPS.map((c) => [c.id, c.score] as const))
  );

  const selected = selectedId
    ? (CLIPS.find((c) => c.id === selectedId) ?? null)
    : null;

  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSelectedId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  function bump(id: string, delta: number) {
    setScores((s) => ({ ...s, [id]: Math.min(99, (s[id] ?? 0) + delta) }));
  }

  return (
    <div className="flex h-screen overflow-hidden bg-ink-950 text-zinc-100">
      <h1 className="sr-only">ClipCatalyst live demo — sample project</h1>

      <Sidebar />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />

        {showBanner ? (
          <div className="flex shrink-0 items-center gap-2.5 border-b border-brand-500/20 bg-brand-500/10 px-4 py-2 sm:px-5">
            <IconInfo className="h-4 w-4 shrink-0 text-brand-300" />
            <p className="flex-1 text-xs leading-relaxed text-brand-300 sm:text-[13px]">
              <span className="font-semibold text-white">Demo mode</span> —
              you’re viewing a sample project. Uploads are disabled.
            </p>
            <button
              type="button"
              onClick={() => setShowBanner(false)}
              aria-label="Dismiss demo mode banner"
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-brand-300 transition hover:bg-white/10 hover:text-white"
            >
              <IconX className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : null}

        <div className="flex min-h-0 flex-1">
          <main className="cc-scroll relative min-w-0 flex-1 overflow-y-auto overscroll-contain">
            <div
              aria-hidden
              className="pointer-events-none absolute -top-32 left-1/2 h-72 w-72 -translate-x-1/2 rounded-full bg-brand-600/10 blur-[100px]"
            />

            <div className="relative px-4 py-6 sm:px-6 lg:px-8">
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-baseline gap-3">
                  <h2 className="font-display text-lg font-semibold tracking-tight text-white">
                    All clips
                  </h2>
                  <span className="font-mono text-xs text-zinc-500">
                    6 ready · 1 rendering
                  </span>
                </div>
                <button
                  type="button"
                  className="inline-flex items-center gap-1.5 rounded-full border border-line-strong bg-white/5 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-brand-400/50 hover:text-white"
                >
                  Sort: Score
                  <IconChevronDown className="h-3.5 w-3.5" />
                </button>
              </div>

              <div
                className={`grid gap-5 ${
                  selected
                    ? "grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2"
                    : "grid-cols-1 sm:grid-cols-2 xl:grid-cols-3"
                }`}
              >
                {CLIPS.map((clip) => (
                  <ClipCard
                    key={clip.id}
                    clip={clip}
                    score={scores[clip.id] ?? clip.score}
                    selected={clip.id === selectedId}
                    onSelect={() => setSelectedId(clip.id)}
                  />
                ))}
                <ProcessingCard />
              </div>

              <p className="mt-8 text-center font-mono text-[11px] text-zinc-600">
                Sample data · nothing here uploads or posts
              </p>
            </div>
          </main>

          {selected ? (
            <DetailPanel
              key={selected.id}
              clip={selected}
              score={scores[selected.id] ?? selected.score}
              onClose={() => setSelectedId(null)}
              onBump={(delta) => bump(selected.id, delta)}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
