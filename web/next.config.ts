import type { NextConfig } from "next";

// A static export: `npm run build` writes out/, which `veil ui` serves next to its API.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
