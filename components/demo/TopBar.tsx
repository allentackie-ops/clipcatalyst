import Link from "next/link";
import { Badge, Button } from "@/components/ui";
import { IconArrowLeft, IconDownload } from "./icons";
import { PROJECT } from "./data";

export default function TopBar() {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-ink-900/70 px-4 sm:gap-4 sm:px-5">
      <Link
        href="/"
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg text-sm text-zinc-400 transition hover:text-white focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-400"
      >
        <IconArrowLeft className="h-4 w-4" />
        <span className="hidden sm:inline">Back to site</span>
        <span className="sr-only sm:hidden">Back to site</span>
      </Link>

      <span aria-hidden className="h-5 w-px shrink-0 bg-white/10" />

      <div className="flex min-w-0 flex-1 items-baseline gap-2.5">
        <span className="truncate font-mono text-[13px] text-white">
          {PROJECT.filename}
        </span>
        <span className="hidden shrink-0 font-mono text-[11px] text-zinc-500 md:inline">
          {PROJECT.runtime} · {PROJECT.clipCount} clips
        </span>
      </div>

      <Badge tone="signal" className="hidden shrink-0 sm:inline-flex">
        <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-signal-400" />
        Processing complete
      </Badge>

      <Button className="shrink-0">
        <IconDownload className="h-4 w-4" />
        Export all
      </Button>
    </header>
  );
}
