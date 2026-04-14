import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  /* config options here */
  allowedDevOrigins: ['meetings.ai-application.garage.dubai'],
};

export default nextConfig;
