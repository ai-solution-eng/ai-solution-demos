import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig({
  server: {
    host: "::",
    port: 8080,
    proxy: {
      // Proxy API requests to the Node.js proxy server (keeping /api prefix)
      '/api': {
        target: 'http://localhost:3001',
        changeOrigin: true,
        // Keep the /api prefix in the path when forwarding to the backend
        rewrite: (path) => path,
        configure: (proxy, _options) => {
          proxy.on('error', (err, _req, _res) => {
            console.error('Proxy error:', err);
          });
        }
      }
    }
  },
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src")
    }
  },
  define: {
    'import.meta.env.DEV': 'true',
    'import.meta.env.VITE_DEV_SERVER_PORT': '8080'
  }
});
