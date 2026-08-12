import { BrandLoader, DelayedLoader } from "@/components/BrandLoader";

/**
 * The demo is the other heavy client bundle. Same loader, one line of
 * context; the line is `aria-hidden` because the loader's own `role="status"`
 * already announces it.
 */
export default function DemoLoading() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-ink-950 px-6">
      <DelayedLoader>
        <div className="flex flex-col items-center gap-5">
          <BrandLoader label="Loading the tour" />
          <p
            aria-hidden
            className="font-display text-sm font-medium tracking-tight text-zinc-400"
          >
            Loading the tour…
          </p>
        </div>
      </DelayedLoader>
    </div>
  );
}
