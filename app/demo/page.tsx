import type { Metadata } from "next";
import DemoApp from "@/components/demo/DemoApp";

export const metadata: Metadata = {
  title: "Live demo — ClipCatalyst",
  description:
    "Explore a sample ClipCatalyst project: six scored clips cut from a founder podcast, AI hooks, and chat-based editing with Catalyst.",
};

export default function DemoPage() {
  return <DemoApp />;
}
