"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { IconSend, IconSpark, IconTrendUp } from "./icons";

type Msg = {
  id: number;
  role: "user" | "assistant";
  text: string;
  scoreFrom?: number;
  scoreTo?: number;
};

const QUICK_ACTIONS = [
  {
    label: "Remove filler words",
    delta: 3,
    reply:
      "Cut 11 filler words (“um”, “so”, “like”) and closed the gaps. The clip runs 2.6s tighter with no visible jump cuts.",
  },
  {
    label: "Add zoom on laugh",
    delta: 2,
    reply:
      "Added a 1.15× punch-in on the laugh spike at 0:19, easing out over 400ms. The reaction lands harder now.",
  },
  {
    label: "More energetic pacing",
    delta: 4,
    reply:
      "Tightened 6 cuts, nudged mid-section speech to 1.04×, and lifted the music 2dB under the payoff. Faster without feeling rushed.",
  },
] as const;

const TYPED_REPLY =
  "Applied. I re-cut the clip against that note and re-scored it — the retention curve is stronger through the first 3 seconds.";

function ScoreChip({ from, to }: { from: number; to: number }) {
  return (
    <span className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-signal-500/30 bg-signal-500/10 px-2 py-1 font-mono text-[11px] text-signal-400">
      <IconTrendUp className="h-3 w-3" />
      Score {from} → {to}
    </span>
  );
}

function AssistantAvatar() {
  return (
    <span className="flex h-4 w-4 items-center justify-center rounded bg-gradient-to-br from-brand-500 to-spark-500">
      <IconSpark className="h-2.5 w-2.5 text-white" />
    </span>
  );
}

export default function CatalystChat({
  score,
  onBump,
}: {
  score: number;
  onBump: (delta: number) => void;
}) {
  const idRef = useRef(3);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [pending, setPending] = useState(false);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>(() => [
    {
      id: 1,
      role: "user",
      text: "Punch up the hook and tighten the first 2 seconds",
    },
    {
      id: 2,
      role: "assistant",
      text: "Done — swapped in a sharper opening line, trimmed 1.8s of preamble, and re-timed the captions so the payoff lands at 0:04.",
      scoreFrom: Math.max(40, score - 17),
      scoreTo: score,
    },
  ]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, pending]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    []
  );

  function applyEdit(text: string, reply: string, delta: number) {
    if (pending) return;
    const from = score;
    const to = Math.min(99, from + delta);
    const userId = idRef.current++;
    const assistantId = idRef.current++;
    setMessages((m) => [...m, { id: userId, role: "user", text }]);
    setPending(true);
    timerRef.current = setTimeout(() => {
      setMessages((m) => [
        ...m,
        {
          id: assistantId,
          role: "assistant",
          text: reply,
          scoreFrom: from,
          scoreTo: to,
        },
      ]);
      setPending(false);
      if (to > from) onBump(to - from);
    }, 900);
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const text = input.trim();
    if (!text || pending) return;
    setInput("");
    applyEdit(text, TYPED_REPLY, 3);
  }

  return (
    <div className="rounded-2xl border border-line bg-ink-950/50">
      <div className="flex items-center gap-2 border-b border-line px-3.5 py-2.5">
        <span className="flex h-5 w-5 items-center justify-center rounded-md bg-gradient-to-br from-brand-500 to-spark-500 shadow-[0_0_12px_rgba(139,92,246,0.4)]">
          <IconSpark className="h-3 w-3 text-white" />
        </span>
        <span className="text-sm font-medium text-white">Catalyst</span>
        <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-zinc-400">
          AI editor
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-[10px] text-zinc-500">
          <span
            aria-hidden
            className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-signal-400"
          />
          live
        </span>
      </div>

      <div
        ref={listRef}
        className="cc-scroll max-h-[320px] space-y-3 overflow-y-auto overscroll-contain px-3.5 py-4"
        aria-live="polite"
      >
        {messages.map((m) =>
          m.role === "user" ? (
            <div
              key={m.id}
              className="ml-auto w-fit max-w-[85%] rounded-2xl rounded-br-md border border-brand-500/25 bg-brand-600/20 px-3.5 py-2.5 text-[13px] leading-relaxed text-zinc-100"
            >
              {m.text}
            </div>
          ) : (
            <div key={m.id} className="max-w-[90%]">
              <div className="mb-1 flex items-center gap-1.5">
                <AssistantAvatar />
                <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-zinc-500">
                  Catalyst
                </span>
              </div>
              <div className="rounded-2xl rounded-tl-md border border-line bg-white/5 px-3.5 py-2.5 text-[13px] leading-relaxed text-zinc-300">
                {m.text}
                {m.scoreFrom != null && m.scoreTo != null ? (
                  <span className="block">
                    <ScoreChip from={m.scoreFrom} to={m.scoreTo} />
                  </span>
                ) : null}
              </div>
            </div>
          )
        )}

        {pending ? (
          <div className="max-w-[90%]">
            <div className="mb-1 flex items-center gap-1.5">
              <AssistantAvatar />
              <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-zinc-500">
                Catalyst is editing…
              </span>
            </div>
            <div className="flex w-fit items-center gap-1.5 rounded-2xl rounded-tl-md border border-line bg-white/5 px-3.5 py-3">
              {[0, 150, 300].map((delay) => (
                <span
                  key={delay}
                  aria-hidden
                  className="h-1.5 w-1.5 animate-bounce rounded-full bg-zinc-400"
                  style={{ animationDelay: `${delay}ms` }}
                />
              ))}
              <span className="sr-only">Catalyst is typing</span>
            </div>
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-1.5 px-3.5 pb-3">
        {QUICK_ACTIONS.map((action) => (
          <button
            key={action.label}
            type="button"
            disabled={pending}
            onClick={() => applyEdit(action.label, action.reply, action.delta)}
            className="rounded-full border border-line-strong bg-white/5 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-brand-400/50 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {action.label}
          </button>
        ))}
      </div>

      <form
        onSubmit={onSubmit}
        className="flex items-center gap-2 border-t border-line p-3"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Tell Catalyst what to change…"
          aria-label="Message Catalyst"
          className="min-w-0 flex-1 rounded-full border border-line bg-ink-950/60 px-4 py-2.5 text-[13px] text-zinc-300 placeholder:text-zinc-600 focus:border-brand-400/70 focus:outline-none focus:ring-2 focus:ring-brand-400/40"
        />
        <button
          type="submit"
          disabled={pending}
          aria-label="Send message to Catalyst"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-brand-600 to-spark-500 text-white shadow-[0_0_16px_rgba(139,92,246,0.35)] transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <IconSend className="h-4 w-4" />
        </button>
      </form>
    </div>
  );
}
