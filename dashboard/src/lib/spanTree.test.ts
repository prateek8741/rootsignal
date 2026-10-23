import { describe, expect, it } from "vitest";
import { span } from "../test/fixtures";
import { buildSpanRows, evidenceSpanIds } from "./spanTree";

const at = (seconds: number) => new Date(Date.UTC(2026, 9, 23, 10, 0, seconds)).toISOString();

describe("buildSpanRows", () => {
  it("orders depth-first by start time and computes depth", () => {
    const rows = buildSpanRows([
      span({ span_id: "c", parent_span_id: "a", started_at: at(5), ended_at: at(6) }),
      span({ span_id: "a", kind: "agent", started_at: at(0), ended_at: at(10) }),
      span({ span_id: "b", parent_span_id: "a", started_at: at(1), ended_at: at(4) }),
      span({ span_id: "b1", parent_span_id: "b", started_at: at(2), ended_at: at(3) }),
    ]);
    expect(rows.map((r) => [r.span.span_id, r.depth])).toEqual([["a", 0], ["b", 1], ["b1", 2], ["c", 1]]);
    expect(rows[0]?.childCount).toBe(2);
  });

  it("breaks ties on identical start times by call index", () => {
    const rows = buildSpanRows([
      span({ span_id: "root", started_at: at(0), ended_at: at(9) }),
      span({ span_id: "second", parent_span_id: "root", started_at: at(1), call_index: 1 }),
      span({ span_id: "first", parent_span_id: "root", started_at: at(1), call_index: 0 }),
    ]);
    expect(rows.map((r) => r.span.span_id)).toEqual(["root", "first", "second"]);
  });

  it("places spans proportionally on the trace timeline", () => {
    const rows = buildSpanRows([
      span({ span_id: "a", started_at: at(0), ended_at: at(10), duration_ms: 10_000 }),
      span({ span_id: "b", parent_span_id: "a", started_at: at(5), ended_at: at(10), duration_ms: 5000 }),
    ]);
    expect(rows[1]?.offset).toBeCloseTo(0.5);
    expect(rows[1]?.width).toBeCloseTo(0.5);
  });

  it("shows spans whose parent never arrived as orphans at the top level", () => {
    const rows = buildSpanRows([
      span({ span_id: "x", parent_span_id: "missing" }),
      span({ span_id: "y", parent_span_id: "x" }),
    ]);
    expect(rows.map((r) => [r.span.span_id, r.depth, r.orphan])).toEqual([["x", 0, true], ["y", 1, false]]);
  });

  it("never loops or drops spans on cyclic input", () => {
    const rows = buildSpanRows([
      span({ span_id: "p", parent_span_id: "q" }),
      span({ span_id: "q", parent_span_id: "p" }),
      span({ span_id: "self", parent_span_id: "self" }),
    ]);
    expect(rows.map((r) => r.span.span_id).sort()).toEqual(["p", "q", "self"]);
  });

  it("handles an empty trace and zero-length spans", () => {
    expect(buildSpanRows([])).toEqual([]);
    const [row] = buildSpanRows([span({ span_id: "z", started_at: at(0), ended_at: at(0), duration_ms: 0 })]);
    expect(row?.width).toBeGreaterThan(0); // still visible
  });
});

describe("evidenceSpanIds", () => {
  it("collects evidence across findings", () => {
    expect([...evidenceSpanIds([{ evidence_span_ids: ["a", "b"] }, { evidence_span_ids: ["b", "c"] }])])
      .toEqual(["a", "b", "c"]);
  });
});
