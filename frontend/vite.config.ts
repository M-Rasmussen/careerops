/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local mode proxies /api to FastAPI (host: 127.0.0.1:8001, Compose: http://api:8000).
// `vite preview` reuses server.proxy, so the Docker image serves the production build with the same proxy.
const target = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8001";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": target } },
  preview: { port: 5173 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
