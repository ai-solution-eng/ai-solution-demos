import type { NextConfig } from "next";

const domain = process.env.DOMAIN || "localhost";
const fullDomain = `defence-ops.${domain}`;

const nextConfig: NextConfig = {
  /* config options here */
};

export default nextConfig;
