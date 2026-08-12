import type { Metadata } from "next";
import { Inter, JetBrains_Mono, Space_Grotesk } from "next/font/google";
import RouteProgress from "@/components/RouteProgress";
import "./globals.css";

const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-space-grotesk",
});

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
});

const TITLE = "ClipCatalyst — Studio-quality AI clips in 90 seconds";
const DESCRIPTION =
  "ClipCatalyst turns long-form video into viral-ready vertical clips in under 90 seconds. Virality Engine, chat-based editing, 4K export, one-click publish.";

export const metadata: Metadata = {
  title: TITLE,
  description: DESCRIPTION,
  applicationName: "ClipCatalyst",
  // ORIGIN ONLY. Next already prefixes the image path with basePath
  // (/clipcatalyst on Pages), so putting the project path here too yields
  // .../clipcatalyst/clipcatalyst/opengraph-image.png and every shared link
  // previews with a broken image.
  metadataBase: new URL("https://allentackie-ops.github.io"),
  openGraph: {
    type: "website",
    siteName: "ClipCatalyst",
    title: TITLE,
    description: DESCRIPTION,
  },
  twitter: {
    card: "summary_large_image",
    title: TITLE,
    description: DESCRIPTION,
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${spaceGrotesk.variable} ${inter.variable} ${jetbrainsMono.variable}`}
    >
      <body className="bg-ink-950 text-zinc-100 antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-full focus:bg-brand-600 focus:px-5 focus:py-2.5 focus:text-sm focus:text-white"
        >
          Skip to content
        </a>
        {/* Renders null unless a client-side navigation outlives 180 ms. */}
        <RouteProgress />
        {children}
      </body>
    </html>
  );
}
