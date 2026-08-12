import { DelayedLoader } from "@/components/BrandLoader";

/**
 * The root wait. The ink ground paints immediately — it is the same colour
 * the body already is, so it costs nothing — and the loader itself only
 * appears if the wait outlives 180 ms.
 */
export default function Loading() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-ink-950 px-6">
      <DelayedLoader />
    </div>
  );
}
