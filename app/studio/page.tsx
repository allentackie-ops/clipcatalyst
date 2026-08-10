import type { Metadata } from "next";
import StudioApp from "@/components/studio/StudioApp";

export const metadata: Metadata = {
  title: "Studio — ClipCatalyst",
  description:
    "Turn a video into vertical, captioned, scored clips — right in your browser.",
};

export default function StudioPage() {
  return (
    <main id="main" className="contents">
      <StudioApp />
    </main>
  );
}
