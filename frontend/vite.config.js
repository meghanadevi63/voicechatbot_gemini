import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// In development the Vite server forwards API calls to FastAPI, so the app
// uses same-origin URLs and needs no CORS setup. In production FastAPI serves
// the built files from dist/ itself.
const backend = process.env.BACKEND_URL || "http://localhost:8002";
const apiPaths = ["/health", "/chat", "/ingest"];

export default defineConfig({
  // Self-signed HTTPS so the page is a secure context (needed for microphone
  // access) when opened from other machines on the network.
  plugins: [react(), basicSsl()],
  server: {
    port: 5174,
    strictPort: true,
    // Listen on all interfaces so other machines on the network can open it
    host: true,
    proxy: Object.fromEntries(apiPaths.map((path) => [path, backend])),
  },
});
