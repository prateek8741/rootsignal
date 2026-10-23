import type { Span } from "../api/client";

/** One row of the waterfall: a span plus its place in the tree and on the time axis. */
export interface SpanRow {
  span: Span;
  depth: number;
  /** Parent was never received (lost or not yet ingested); shown at top level. */
  orphan: boolean;
  /** Offset from the trace start and width, both as fractions of the trace duration. */
  offset: number;
  width: number;
  childCount: number;
}

function startMs(span: Span): number {
  return new Date(span.started_at).getTime();
}

function endMs(span: Span): number {
  if (span.ended_at) return new Date(span.ended_at).getTime();
  return startMs(span) + (span.duration_ms ?? 0);
}

/**
 * Flatten spans into display order: depth-first, children by start time (then
 * call index), so the list reads top to bottom in execution order. Robust to
 * missing parents and to cycles in malformed input.
 */
export function buildSpanRows(spans: readonly Span[]): SpanRow[] {
  if (spans.length === 0) return [];
  const ids = new Set(spans.map((s) => s.span_id));
  const children = new Map<string, Span[]>();
  const roots: Span[] = [];
  for (const span of spans) {
    const parent = span.parent_span_id;
    if (parent && ids.has(parent) && parent !== span.span_id) {
      const list = children.get(parent) ?? [];
      list.push(span);
      children.set(parent, list);
    } else {
      roots.push(span);
    }
  }
  const order = (a: Span, b: Span) =>
    startMs(a) - startMs(b) || (a.call_index ?? 0) - (b.call_index ?? 0);
  roots.sort(order);
  for (const list of children.values()) list.sort(order);

  const traceStart = Math.min(...spans.map(startMs));
  const traceEnd = Math.max(...spans.map(endMs));
  const total = Math.max(traceEnd - traceStart, 1);

  const rows: SpanRow[] = [];
  const visited = new Set<string>();
  const visit = (span: Span, depth: number, orphan: boolean) => {
    if (visited.has(span.span_id)) return; // cycle guard
    visited.add(span.span_id);
    const kids = children.get(span.span_id) ?? [];
    rows.push({
      span,
      depth,
      orphan,
      offset: (startMs(span) - traceStart) / total,
      width: Math.max((endMs(span) - startMs(span)) / total, 0.002),
      childCount: kids.length,
    });
    for (const child of kids) visit(child, depth + 1, false);
  };
  for (const root of roots) visit(root, 0, root.parent_span_id !== null);
  // Anything unreachable (only possible with cycles) is appended so nothing is hidden.
  for (const span of spans) if (!visited.has(span.span_id)) visit(span, 0, true);
  return rows;
}

/** Span IDs referenced as evidence by the given findings. */
export function evidenceSpanIds(findings: readonly { evidence_span_ids: string[] }[]): Set<string> {
  return new Set(findings.flatMap((f) => f.evidence_span_ids));
}
