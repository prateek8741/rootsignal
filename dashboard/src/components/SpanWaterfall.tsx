import type { Span } from "../api/client";
import { formatDuration } from "../lib/format";
import { buildSpanRows } from "../lib/spanTree";
import { Badge, KindBadge } from "./ui";

interface Props {
  spans: readonly Span[];
  selectedId: string | null;
  highlighted: ReadonlySet<string>;
  onSelect: (spanId: string) => void;
}

const BAR = { error: "bg-red-500", llm: "bg-sky-500", tool: "bg-amber-500", mcp: "bg-amber-500",
  retrieval: "bg-emerald-500", agent: "bg-violet-400" } as Record<string, string>;

/** Execution tree with a timeline bar per span (relative to the whole trace). */
export function SpanWaterfall({ spans, selectedId, highlighted, onSelect }: Props) {
  const rows = buildSpanRows(spans);
  return (
    <div role="tree" aria-label="Spans" className="divide-y divide-slate-100 text-sm">
      {rows.map(({ span, depth, orphan, offset, width }) => {
        const failed = span.status === "error";
        const selected = span.span_id === selectedId;
        const evidence = highlighted.has(span.span_id);
        return (
          <div key={span.span_id} role="treeitem" aria-level={depth + 1} aria-selected={selected}
            data-testid={`span-${span.span_id}`}
            onClick={() => onSelect(span.span_id)}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(span.span_id)}
            tabIndex={0}
            className={`grid cursor-pointer grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_4.5rem] items-center gap-3 px-3 py-1.5
              ${selected ? "bg-sky-50" : evidence ? "bg-amber-50" : "hover:bg-slate-50"}
              ${failed ? "border-l-2 border-l-red-500" : "border-l-2 border-l-transparent"}`}>
            {/* Badges wrap onto a second line rather than squeezing the name or
                spilling into the timeline column. */}
            <div className="flex min-w-0 flex-wrap items-center gap-1.5" style={{ paddingLeft: depth * 16 }}>
              <KindBadge kind={span.kind} />
              <span className={`max-w-full truncate ${failed ? "font-medium text-red-700" : ""}`} title={span.name}>
                {span.tool_name ?? span.model ?? span.name}
              </span>
              {failed && <Badge tone="red">{span.error_type ?? "error"}</Badge>}
              {(span.repeat_index ?? 0) > 0 && (
                <Badge tone="orange" title="Identical arguments to an earlier call in this run">
                  repeat #{span.repeat_index}
                </Badge>
              )}
              {span.side_effect === "write" && <Badge tone="violet" title="Declared side effect: write">write</Badge>}
              {orphan && <Badge title="Parent span was never received">orphan</Badge>}
              {evidence && <span className="sr-only">(evidence)</span>}
            </div>
            <div className="relative h-3 rounded bg-slate-100" aria-hidden>
              <div className={`absolute top-0 h-3 rounded ${failed ? BAR.error : (BAR[span.kind] ?? "bg-slate-400")}`}
                style={{ left: `${offset * 100}%`, width: `${Math.min(width, 1 - offset) * 100}%` }} />
            </div>
            <div className="text-right tabular-nums text-slate-600">{formatDuration(span.duration_ms)}</div>
          </div>
        );
      })}
    </div>
  );
}
