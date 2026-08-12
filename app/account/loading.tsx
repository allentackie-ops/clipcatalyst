import { BrandLoader, DelayedLoader } from "@/components/BrandLoader";

/**
 * The account route waits on the signed-in plan before it has anything true
 * to show. Same loader, one line of context; the line is `aria-hidden`
 * because the loader's own `role="status"` already announces it.
 */
export default function AccountLoading() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-ink-950 px-6">
      <DelayedLoader>
        <div className="flex flex-col items-center gap-5">
          <BrandLoader label="Opening your account" />
          <p
            aria-hidden
            className="font-display text-sm font-medium tracking-tight text-zinc-400"
          >
            Opening your account…
          </p>
        </div>
      </DelayedLoader>
    </div>
  );
}
