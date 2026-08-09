import Link from "next/link";
import type { ReactNode } from "react";

export function Container({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`mx-auto w-full max-w-6xl px-5 sm:px-8 ${className}`}>
      {children}
    </div>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <p className="mb-4 font-mono text-xs font-medium uppercase tracking-[0.2em] text-brand-400">
      {children}
    </p>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  lede,
  align = "center",
}: {
  eyebrow?: string;
  title: ReactNode;
  lede?: ReactNode;
  align?: "center" | "left";
}) {
  const alignCls =
    align === "center" ? "text-center mx-auto items-center" : "text-left";
  return (
    <div className={`flex max-w-3xl flex-col ${alignCls}`}>
      {eyebrow ? <Eyebrow>{eyebrow}</Eyebrow> : null}
      <h2 className="font-display text-3xl font-semibold tracking-tight text-white sm:text-4xl md:text-[2.75rem] md:leading-[1.1]">
        {title}
      </h2>
      {lede ? (
        <p className="mt-5 text-base leading-relaxed text-zinc-400 sm:text-lg">
          {lede}
        </p>
      ) : null}
    </div>
  );
}

export function GradientText({ children }: { children: ReactNode }) {
  return (
    <span className="bg-gradient-to-r from-brand-400 via-spark-400 to-ember-400 bg-clip-text text-transparent">
      {children}
    </span>
  );
}

type ButtonProps = {
  children: ReactNode;
  href?: string;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "ghost";
  size?: "md" | "lg";
  className?: string;
  type?: "button" | "submit";
};

export function Button({
  children,
  href,
  onClick,
  variant = "primary",
  size = "md",
  className = "",
  type = "button",
}: ButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-full font-medium transition-all duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";
  const sizes = {
    md: "px-5 py-2.5 text-sm",
    lg: "px-7 py-3.5 text-base",
  };
  const variants = {
    primary:
      "bg-gradient-to-r from-brand-600 to-spark-500 text-white shadow-[0_0_24px_rgba(139,92,246,0.35)] hover:shadow-[0_0_36px_rgba(139,92,246,0.55)] hover:brightness-110",
    secondary:
      "border border-line-strong bg-white/5 text-white hover:border-brand-400/60 hover:bg-white/10",
    ghost: "text-zinc-300 hover:text-white",
  };
  const cls = `${base} ${sizes[size]} ${variants[variant]} ${className}`;
  if (href) {
    return (
      <Link href={href} className={cls}>
        {children}
      </Link>
    );
  }
  return (
    <button type={type} onClick={onClick} className={cls}>
      {children}
    </button>
  );
}

export function Badge({
  children,
  tone = "brand",
  className = "",
}: {
  children: ReactNode;
  tone?: "brand" | "spark" | "ember" | "signal" | "neutral";
  className?: string;
}) {
  const tones = {
    brand: "border-brand-500/30 bg-brand-500/10 text-brand-300",
    spark: "border-spark-500/30 bg-spark-500/10 text-spark-400",
    ember: "border-ember-500/30 bg-ember-500/10 text-ember-400",
    signal: "border-signal-500/30 bg-signal-500/10 text-signal-400",
    neutral: "border-line-strong bg-white/5 text-zinc-300",
  };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${tones[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-2xl border border-line bg-ink-850/80 ${className}`}
    >
      {children}
    </div>
  );
}

/** Circular 0–100 virality score indicator (SVG, no external deps). */
export function ScoreRing({
  score,
  size = 48,
  className = "",
}: {
  score: number;
  size?: number;
  className?: string;
}) {
  const stroke = size >= 56 ? 4 : 3;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const filled = (Math.min(Math.max(score, 0), 100) / 100) * c;
  const color =
    score >= 80 ? "#34d399" : score >= 60 ? "#fbbf24" : "#a1a1aa";
  return (
    <div
      className={`relative inline-flex items-center justify-center ${className}`}
      style={{ width: size, height: size }}
      role="img"
      aria-label={`Virality score ${score} out of 100`}
    >
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="rgba(255,255,255,0.1)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${c - filled}`}
        />
      </svg>
      <span
        className="absolute font-mono font-semibold text-white"
        style={{ fontSize: size / 3.2 }}
      >
        {score}
      </span>
    </div>
  );
}

/** Brand wordmark: bolt glyph + name. */
export function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-brand-500 to-spark-500 shadow-[0_0_16px_rgba(139,92,246,0.5)]">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M13 2 4.5 13.5H11L9.5 22 19 10h-6.5L13 2Z"
            fill="white"
          />
        </svg>
      </span>
      <span className="font-display text-lg font-semibold tracking-tight text-white">
        Clip<span className="text-brand-400">Catalyst</span>
      </span>
    </span>
  );
}
