import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: 'standalone', // For production Docker builds
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: process.env.BACKEND_URL || 'http://backend:8000/:path*',
      },
    ]
  },
  reactCompiler: true,
  experimental: {
    middlewareClientMaxBodySize: '30mb', // or choose a larger size string or bytes number
  }
};

export default nextConfig;
