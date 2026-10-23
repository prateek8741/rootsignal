import { describe, expect, it } from "vitest";
import { formatCost, formatDuration, formatRelative, formatTokens, prettyPayload } from "./format";

describe("format", () => {
  it("formats durations across scales", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0.4)).toBe("<1 ms");
    expect(formatDuration(250)).toBe("250 ms");
    expect(formatDuration(1234)).toBe("1.23 s");
    expect(formatDuration(45_000)).toBe("45.0 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
  });

  it("marks costs with unpriced calls as lower bounds", () => {
    expect(formatCost("0.000000")).toBe("$0");
    expect(formatCost("1.234567")).toBe("$1.23");
    expect(formatCost("0.004200")).toBe("$0.0042");
    expect(formatCost("1.50", 2)).toBe("≥ $1.50");
    expect(formatCost(null, 3)).toBe("unpriced");
    expect(formatCost(null)).toBe("—");
  });

  it("abbreviates token counts", () => {
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(4200)).toBe("4.2k");
    expect(formatTokens(150_000)).toBe("150k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
  });

  it("describes relative times", () => {
    const now = new Date("2026-10-23T12:00:00Z");
    expect(formatRelative("2026-10-23T11:59:30Z", now)).toBe("just now");
    expect(formatRelative("2026-10-23T11:15:00Z", now)).toBe("45 min ago");
    expect(formatRelative("2026-10-21T12:00:00Z", now)).toBe("2 d ago");
  });

  it("pretty-prints JSON payloads and leaves text alone", () => {
    expect(prettyPayload('{"a":1}', "json")).toBe('{\n  "a": 1\n}');
    expect(prettyPayload('{"a":1', "json")).toBe('{"a":1'); // truncated JSON stays as-is
    expect(prettyPayload("plain", "text")).toBe("plain");
  });
});
