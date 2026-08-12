import { BrandLoader, DelayedLoader } from "@/components/BrandLoader";

/**
 * Studio carries one of the two heaviest client bundles, so this is where a
 * real wait actually happens on a phone. A labelled wait feels shorter than a
 * bare one — the line is `aria-hidden` because the loader's own `role="status"`
 * already announces it.
 */
export default function StudioLoading() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-ink-950 px-6">
      <DelayedLoader>
        <div className="flex flex-col items-center gap-5">
          <BrandLoader label="Opening Studio" />
          <p
            aria-hidden
            className="font-display text-sm font-medium tracking-tight text-zinc-400"
          >
            Opening Studio…
          </p>
        </div>
      </DelayedLoader>
    </div>
  );
}
