import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type Finding } from "../api/client";
import { SpanDetails } from "../components/SpanDetails";
import { SpanWaterfall } from "../components/SpanWaterfall";
import { Card, EmptyState, ErrorBox, Loading, SeverityBadge, Stat, TraceStatusBadge } from "../components/ui";
import { formatCost, formatDuration, formatTime, formatTokens } from "../lib/format";
import { evidenceSpanIds } from "../lib/spanTree";

function FindingsPanel({ findings, focused, onFocus }: {
  findings: Finding[]; focused: string | null; onFocus: (finding: Finding | null) => void;
}) {
  if (findings.length === 0) return null;
  return (
    <Card title={`Findings (${findings.length})`}>
      <ul className="space-y-2">
        {findings.map((f) => (
          <li key={f.id}>
            <button type="button" onClick={() => onFocus(focused === f.id ? null : f)}
              className={`w-full rounded-md border px-3 py-2 text-left text-sm ${focused === f.id ? "border-amber-400 bg-amber-50" : "border-slate-200 hover:bg-slate-50"}`}>
              <div className="flex items-center gap-2">
                <SeverityBadge severity={f.severity} />
                <span className="font-medium">{f.title}</span>
                <span className="ml-auto text-xs text-slate-500">{f.detector}</span>
              </div>
              <p className="mt-1 text-slate-600">{f.summary}</p>
              <p className="mt-1 text-xs text-slate-500">
                {focused === f.id ? "Evidence highlighted below" : `${f.evidence_span_ids.length} evidence span(s) · click to highlight`}
              </p>
            </button>
            {f.issue_id && (
              <Link to={`/issues/${f.issue_id}`} className="ml-3 text-xs text-sky-700 hover:underline">
                View issue →
              </Link>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-slate-500">
        Findings state what the trace shows; they don't claim a cause.
      </p>
    </Card>
  );
}

export function TraceDetailPage() {
  const { traceId = "" } = useParams();
  const [params] = useSearchParams();
  const query = useQuery({ queryKey: ["trace", traceId], queryFn: () => api.trace(traceId) });
  // ?span=<id> (e.g. from an investigation claim) preselects and reveals that span.
  const [selectedId, setSelectedId] = useState<string | null>(params.get("span"));
  useEffect(() => {
    const target = params.get("span");
    if (target && query.data) {
      setSelectedId(target);
      document.querySelector(`[data-testid="span-${CSS.escape(target)}"]`)?.scrollIntoView({ block: "center" });
    }
  }, [params, query.data]);
  const [focused, setFocused] = useState<Finding | null>(null);
  const highlighted = useMemo(() => evidenceSpanIds(focused ? [focused] : []), [focused]);

  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const { trace, spans, findings } = query.data;
  const selected = spans.find((s) => s.span_id === selectedId) ?? null;

  return (
    <div className="space-y-4">
      <div>
        <Link to="/traces" className="text-sm text-sky-700 hover:underline">← Traces</Link>
        <h1 className="mt-1 flex items-center gap-2 text-xl font-semibold">
          {trace.agent_name ?? "Trace"}
          {trace.agent_version && <span className="text-base font-normal text-slate-500">{trace.agent_version}</span>}
          <TraceStatusBadge status={trace.status} />
        </h1>
        <p className="font-mono text-xs text-slate-500">{trace.trace_id}</p>
      </div>
      <Card>
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4 lg:grid-cols-7">
          <Stat label="Started" value={formatTime(trace.started_at)} />
          <Stat label="Duration" value={formatDuration(trace.duration_ms)} />
          <Stat label="Spans" value={`${trace.span_count} (${trace.error_count} errored)`} />
          <Stat label="Model calls" value={trace.llm_call_count} />
          <Stat label="Tool calls" value={trace.tool_call_count} />
          <Stat label="Tokens" value={`${formatTokens(trace.input_tokens)} in · ${formatTokens(trace.output_tokens)} out`} />
          <Stat label="Cost" value={formatCost(trace.cost_usd, trace.unpriced_llm_call_count)}
            hint={trace.unpriced_llm_call_count > 0 ? `${trace.unpriced_llm_call_count} model call(s) had no price` : undefined} />
        </dl>
        {trace.completion_reason === "idle_timeout" && (
          <p className="mt-3 text-xs text-amber-700">
            The root span never arrived; this trace was completed after an idle timeout and may be partial.
          </p>
        )}
      </Card>
      <FindingsPanel findings={findings} focused={focused?.id ?? null} onFocus={setFocused} />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card title="Execution">
          {spans.length === 0 ? <EmptyState title="No spans stored for this trace" /> : (
            <SpanWaterfall spans={spans} selectedId={selectedId} highlighted={highlighted}
              onSelect={setSelectedId} />
          )}
        </Card>
        <Card title="Span">
          {selected ? <SpanDetails span={selected} /> : (
            <p className="text-sm text-slate-500">Select a span to see its inputs, outputs and errors.</p>
          )}
        </Card>
      </div>
    </div>
  );
}
