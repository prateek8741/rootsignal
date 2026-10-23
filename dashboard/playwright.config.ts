import { defineConfig } from "@playwright/test";

// Smoke test against a running dashboard (npm run dev) + API with seeded data.
// Uses the locally installed Google Chrome, so no browser download is needed.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  use: { baseURL: process.env.DASHBOARD_URL ?? "http://127.0.0.1:5173", channel: "chrome" },
});
