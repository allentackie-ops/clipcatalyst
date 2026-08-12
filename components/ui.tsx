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

/**
 * `Eyebrow` used to live here: a mono uppercase kicker above a section
 * heading. It was removed rather than restyled, because every one of its
 * usages was the decorative "EARLY ACCESS" chip the refresh deletes, and a
 * component whose only job is banned decoration is an invitation to put it
 * back. `SectionHeading` lost its `eyebrow` prop with it, so a leftover
 * kicker is a compile error rather than something that quietly renders.
 * Headings stand on their own.
 */
export function SectionHeading({
  title,
  lede,
  align = "center",
}: {
  title: ReactNode;
  lede?: ReactNode;
  align?: "center" | "left";
}) {
  const alignCls =
    align === "center" ? "text-center mx-auto items-center" : "text-left";
  return (
    <div className={`flex max-w-3xl flex-col ${alignCls}`}>
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

/**
 * The accent span inside a headline. The name is historical: it renders one
 * solid brand violet now, not a gradient. Kept as a component (rather than
 * inlined at each call site) so the accent stays one decision in one place,
 * and so no caller had to change when the gradient went.
 */
export function GradientText({ children }: { children: ReactNode }) {
  return <span className="text-brand-300">{children}</span>;
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
  // transition-colors, not transition-all: the only thing that moves on hover
  // now is colour. The old transition-all existed to ease the glow shadow and
  // the brightness filter, and both are gone.
  const base =
    "inline-flex items-center justify-center gap-2 rounded-full font-medium transition-colors duration-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400";
  const sizes = {
    md: "px-5 py-2.5 text-sm",
    lg: "px-7 py-3.5 text-base",
  };
  const variants = {
    primary: "bg-brand-600 text-white hover:bg-brand-500",
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
/**
 * The ClipCatalyst mark: one wide video cut into three tall clips, sliding
 * apart. Outer corners are rounded and the cut edges are square, so the three
 * pieces still read as one rectangle that was divided.
 *
 * Drawn on a 1.5-unit module, which puts every edge on a whole pixel at 16px —
 * the size it spends most of its life at. Fills with `currentColor`, so the
 * caller owns the colour and there is no gradient id to collide when the mark
 * appears more than once on a page.
 */
export function Mark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden>
      <path d="M3.75 3 H7.5 V15 H3.75 A2.25 2.25 0 0 1 1.5 12.75 V5.25 A2.25 2.25 0 0 1 3.75 3 Z" />
      <path d="M9 6 H15 V18 H9 Z" />
      <path d="M16.5 9 H20.25 A2.25 2.25 0 0 1 22.5 11.25 V18.75 A2.25 2.25 0 0 1 20.25 21 H16.5 Z" />
    </svg>
  );
}

export function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <Mark className="h-[26px] w-[26px] text-brand-400" />
      <span className="font-display text-lg font-semibold tracking-tight text-white">
        Clip<span className="text-brand-400">Catalyst</span>
      </span>
    </span>
  );
}
