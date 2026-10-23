import { describe, expect, it } from "vitest";

// Guard for docs/THREAT_MODEL.md §3.3: no credential (or anything else) may be kept
// in JavaScript-readable browser storage. Sessions are httpOnly cookies set by the API.
const sources = import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../test/**"], {
  query: "?raw", import: "default", eager: true,
}) as Record<string, string>;

describe("browser storage", () => {
  it("is never used by dashboard code", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(10);
    const offenders = Object.entries(sources)
      .filter(([, code]) => /\b(localStorage|sessionStorage|indexedDB|document\.cookie)\b/.test(code))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
