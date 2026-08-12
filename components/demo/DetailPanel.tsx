"use client";

import { useEffect, useRef, useState } from "react";
import { Badge, ScoreRing } from "@/components/ui";
import type { Clip } from "./data";
import CatalystChat from "./CatalystChat";
import { IconCheck, IconPlay, IconX } from "./icons";

function AnimatedCaption({
  words,
  highlight,
}: {
  words: string[];
  highlight: number;
}) {
  const [active, setActive] = useState(0);
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setReducedMotion(true);
      return;
    }
    const timer = setInterval(
      () => setActive((v) => (v + 1) % words.length),
      440
    );
    return () => clearInterval(timer);
  }, [words.length]);

  const activeWord = reducedMotion ? highlight : active;

  return (
    <p className="rounded-lg bg-ink-950/80 px-2.5 py-1.5 text-center text-xs font-bold leading-snug backdrop-blur-sm">
      {words.map((word, i) => (
        <span
          key={i}
          className={`transition-colors duration-150 ${
            i === activeWord ? "text-ember-400" : "text-white"
          }`}
        >
          {word}
          {i < words.length - 1 ? " " : ""}
        </span>
      ))}
    </p>
  );
}

export default function DetailPanel({
  clip,
  score,
  onClose,
  onBump,
}: {
  clip: Clip;
  score: number;
  onClose: () => void;
  onBump: (delta: number) => void;
}) {
  const [selectedHook, setSelectedHook] = useState(clip.recommendedHook);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButtonRef.current?.focus();
  }, []);

  return (
    <aside
      role="dialog"
      aria-modal="true"
      aria-label={`${clip.title}: clip details`}
      className="fixed inset-0 z-50 flex animate-rise flex-col bg-ink-900 lg:static lg:z-auto lg:w-[420px] lg:shrink-0 lg:border-l lg:border-line lg:bg-ink-900/60"
    >
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-line px-5">
        <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
          Clip detail
        </span>
        <button
          ref={closeButtonRef}
          type="button"
          onClick={onClose}
          aria-label="Close clip details"
          className="flex h-8 w-8 items-center justify-center rounded-lg text-zinc-400 transition hover:bg-white/5 hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
        >
          <IconX className="h-4 w-4" />
        </button>
      </div>

      <div className="cc-scroll flex-1 overflow-y-auto overscroll-contain px-5 py-5">
        {/* 9:16 preview */}
        <div className="relative mx-auto w-full max-w-[230px] overflow-hidden rounded-2xl border border-line-strong shadow-[0_12px_48px_rgba(0,0,0,0.45)]">
          <div aria-hidden className={`aspect-[9/16] w-full ${clip.tint}`} />
          <span
            aria-hidden
            className="pointer-events-none absolute left-1/2 top-1/4 h-28 w-28 -translate-x-1/2 rounded-full bg-white/10 blur-2xl"
          />
          <span className="absolute left-2.5 top-2.5 rounded-md bg-ink-950/70 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 backdrop-blur-sm">
            4K · 9:16
          </span>
          <span className="absolute right-2.5 top-2.5 rounded-md bg-ink-950/70 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200 backdrop-blur-sm">
            {clip.duration}
          </span>
          <span aria-hidden className="absolute inset-0 flex items-center justify-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full border border-white/25 bg-ink-950/50 backdrop-blur-sm transition hover:bg-ink-950/70">
              <IconPlay className="h-4 w-4 translate-x-[1px] text-white" />
            </span>
          </span>
          <div className="absolute inset-x-3 bottom-6">
            <AnimatedCaption
              words={clip.captionWords}
              highlight={clip.captionHighlight}
            />
          </div>
          <div className="absolute inset-x-3 bottom-2.5 flex items-center gap-2">
            <div
              aria-hidden
              className="h-0.5 flex-1 overflow-hidden rounded-full bg-white/20"
            >
              <div className="h-full w-1/3 rounded-full bg-white" />
            </div>
            <span className="font-mono text-[9px] text-zinc-400">0:12</span>
          </div>
        </div>

        {/* title + score */}
        <div className="mt-5 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="font-display text-lg font-semibold leading-snug tracking-tight text-white">
              {clip.title}
            </h2>
            <p className="mt-1 font-mono text-[11px] text-zinc-500">
              {clip.duration} · 4K · from {clip.sourceAt}
            </p>
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {clip.platforms.map((platform) => (
                <Badge key={platform} tone="neutral">
                  {platform}
                </Badge>
              ))}
            </div>
          </div>
          <ScoreRing score={score} size={56} className="shrink-0" />
        </div>

        {/* AI hooks */}
        <section className="mt-6">
          <div className="flex items-baseline justify-between">
            <h3 className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
              AI hooks
            </h3>
            <span className="font-mono text-[11px] text-zinc-400">
              5 generated
            </span>
          </div>
          <ul className="mt-3 space-y-2">
            {clip.hooks.map((hook, i) => {
              const isSelected = selectedHook === i;
              return (
                <li key={i}>
                  <button
                    type="button"
                    onClick={() => setSelectedHook(i)}
                    aria-pressed={isSelected}
                    className={`flex w-full items-center gap-2.5 rounded-xl border px-3 py-2.5 text-left transition ${
                      isSelected
                        ? "border-brand-400/50 bg-brand-500/10"
                        : "border-line bg-white/[0.02] hover:border-line-strong hover:bg-white/5"
                    }`}
                  >
                    <span
                      aria-hidden
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
                        isSelected
                          ? "border-brand-400 bg-brand-500"
                          : "border-line-strong"
                      }`}
                    >
                      {isSelected ? (
                        <IconCheck className="h-2.5 w-2.5 text-white" />
                      ) : null}
                    </span>
                    <span className="min-w-0 flex-1 text-[13px] leading-snug text-zinc-300">
                      {hook}
                    </span>
                    {i === clip.recommendedHook ? (
                      <Badge tone="signal" className="shrink-0">
                        Recommended
                      </Badge>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>

        {/* Catalyst chat */}
        <section className="mt-6 pb-2">
          <h3 className="sr-only">Chat with Catalyst</h3>
          <CatalystChat score={score} onBump={onBump} />
        </section>
      </div>
    </aside>
  );
}
