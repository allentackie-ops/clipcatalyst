import Link from "next/link";
import { Logo } from "@/components/ui";
import {
  IconChart,
  IconFilm,
  IconFolder,
  IconSliders,
  IconSwatch,
} from "./icons";

type NavItem = {
  label: string;
  icon: (props: { className?: string }) => React.JSX.Element;
  active?: boolean;
  count?: string;
};

const NAV: NavItem[] = [
  { label: "Projects", icon: IconFolder },
  { label: "Clips", icon: IconFilm, active: true, count: "6" },
  { label: "Analytics", icon: IconChart },
  { label: "Brand Kit", icon: IconSwatch },
  { label: "Settings", icon: IconSliders },
];

export default function Sidebar() {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-line bg-ink-900/70 md:flex">
      <div className="flex h-14 shrink-0 items-center border-b border-line px-4">
        <Link
          href="/"
          aria-label="ClipCatalyst home"
          className="rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
        >
          <Logo />
        </Link>
      </div>

      <nav aria-label="Workspace" className="flex-1 space-y-1 px-3 py-4">
        {NAV.map((item) => {
          const IconGlyph = item.icon;
          return (
            <button
              key={item.label}
              type="button"
              aria-current={item.active ? "page" : undefined}
              aria-disabled={item.active ? undefined : "true"}
              tabIndex={item.active ? undefined : -1}
              className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition ${
                item.active
                  ? "bg-brand-500/10 text-white ring-1 ring-inset ring-brand-500/30"
                  : "cursor-default text-zinc-400 hover:bg-white/5 hover:text-white"
              }`}
            >
              <IconGlyph
                className={`h-[18px] w-[18px] shrink-0 ${
                  item.active ? "text-brand-300" : "text-zinc-500"
                }`}
              />
              {item.label}
              {item.count ? (
                <span className="ml-auto rounded-md bg-brand-500/15 px-1.5 py-0.5 font-mono text-[10px] text-brand-300">
                  {item.count}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="shrink-0 border-t border-line p-4">
        <div className="flex items-baseline justify-between">
          <span className="font-mono text-[11px] uppercase tracking-[0.15em] text-zinc-500">
            Credits
          </span>
          <span className="font-mono text-xs text-white">
            82<span className="text-zinc-500"> / 100</span>
          </span>
        </div>
        <div
          className="mt-2 h-1 overflow-hidden rounded-full bg-white/10"
          role="img"
          aria-label="82 of 100 credits remaining"
        >
          <div className="h-full w-[82%] rounded-full bg-gradient-to-r from-brand-500 to-spark-500" />
        </div>
        <Link
          href="/#pricing"
          className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-brand-300 transition hover:text-white"
        >
          Upgrade
          <span aria-hidden>→</span>
        </Link>
      </div>
    </aside>
  );
}
