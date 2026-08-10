import type { Metadata } from "next";
import AccountProvider from "@/components/account/AccountProvider";
import StudioApp from "@/components/studio/StudioApp";

export const metadata: Metadata = {
  title: "Studio — ClipCatalyst",
  description:
    "Turn a video into vertical, captioned, scored clips — right in your browser.",
};

export default function StudioPage() {
  return (
    <main id="main" className="contents">
      {/* Studio reads the signed-in plan's entitlements (watermark) from
          /v1/me; with NEXT_PUBLIC_CLOUD_API unset the provider never touches
          the network and Studio behaves exactly as before. */}
      <AccountProvider>
        <StudioApp />
      </AccountProvider>
    </main>
  );
}
