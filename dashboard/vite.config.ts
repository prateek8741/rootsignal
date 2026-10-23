/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.ROOTSIGNAL_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The dashboard calls /v1 on its own origin; the dev server forwards to the API,
  // so no CORS configuration is needed on the API.
  // Loopback only: a dev server with API keys in the browser should not face the network.
  server: { host: "127.0.0.1", port: 5173, proxy: { "/v1": api } },
  preview: { host: "127.0.0.1", port: 4173, proxy: { "/v1": api } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
