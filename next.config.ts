import type { NextConfig } from "next";

// GITHUB_PAGES=true → static export served under /clipcatalyst on github.io.
// NEXT_OUTPUT=export → plain static export (local preview, other static hosts).
const onPages = process.env.GITHUB_PAGES === "true";
const staticExport = onPages || process.env.NEXT_OUTPUT === "export";

const nextConfig: NextConfig = {
  ...(staticExport
    ? { output: "export" as const, images: { unoptimized: true } }
    : {}),
  ...(onPages ? { basePath: "/clipcatalyst", trailingSlash: true } : {}),
};

export default nextConfig;
