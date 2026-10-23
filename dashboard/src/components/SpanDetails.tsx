import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Span } from "../api/client";
import { formatCost, formatDuration, formatTime, formatTokens, prettyPayload } from "../lib/format";
import { useActiveConnection } from "../lib/connection";
import { Badge, Button, ErrorBox, KindBadge, Loading, Stat } from "./ui";

function PayloadViewer({ label, sha256 }: { label: string; sha256: string | null }) {
  const { apiKey } = useActiveConnection();
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["payload", apiKey, sha256],
    queryFn: () => api.payload(apiKey, sha256 as string),
    enabled: open && sha256 !== null,
    staleTime: Infinity, // content-addressed: never changes
  });
  if (!sha256) return <p className="text-xs text-slate-400">{label}: not captured</p>;
  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
        <Button className="!px-2 !py-0.5 text-xs" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"}
        </Button>
      </div>
      {open && (query.isPending ? <Loading /> : query.isError ? <ErrorBox error={query.error} /> : (
        <div className="mt-1">
          <div className="mb-1 flex gap-1">
            {query.data.truncated && <Badge tone="amber">truncated</Badge>}
            <Badge title="Who redacted this payload">redaction: {query.data.redaction_state}</Badge>
            <Badge>{query.data.byte_size.toLocaleString()} bytes</Badge>
          </div>
          <pre className="max-h-80 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
            {prettyPayload(query.data.body, query.data.content_type)}
          </pre>
        </div>
      ))}
    </div>
  );
}

export function SpanDetails({ span }: { span: Span }) {
  const exceptions = span.events.filter((e) => e["name"] === "exception");
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <KindBadge kind={span.kind} />
        <span className="font-medium">{span.name}</span>
        {span.status === "error" && <Badge tone="red">{span.error_type ?? "error"}</Badge>}
      </div>
      <dl className="grid grid-cols-2 gap-3">
        <Stat label="Duration" value={formatDuration(span.duration_ms)} />
        <Stat label="Started" value={formatTime(span.started_at)} />
        {span.model && <Stat label="Model" value={`${span.model}${span.provider ? ` (${span.provider})` : ""}`} />}
        {span.kind === "llm" && (
          <Stat label="Tokens" value={`${formatTokens(span.input_tokens)} in · ${formatTokens(span.output_tokens)} out`} />
        )}
        {span.kind === "llm" && <Stat label="Cost" value={formatCost(span.cost_usd, span.cost_usd ? 0 : 1)} />}
        {span.tool_name && <Stat label="Side effect" value={span.side_effect ?? "unknown"}
          hint="Declared by the tool; replay never executes write/unknown tools" />}
        {span.args_hash && <Stat label="Args hash" value={<span className="font-mono">{span.args_hash}</span>} />}
        {span.call_index !== null && <Stat label="Call #" value={`${span.call_index}${(span.repeat_index ?? 0) > 0 ? ` (repeat ${span.repeat_index})` : ""}`} />}
        <Stat label="Span ID" value={<span className="font-mono text-xs">{span.span_id}</span>} />
      </dl>
      {span.status_message && (
        <p className="rounded bg-red-50 p-2 text-xs text-red-800">{span.status_message}</p>
      )}
      <PayloadViewer label="Input" sha256={span.input_payload_hash} />
      <PayloadViewer label="Output" sha256={span.output_payload_hash} />
      {exceptions.map((e, i) => {
        const attrs = (e["attributes"] ?? {}) as Record<string, string>;
        return (
          <details key={i} className="rounded border border-red-200 bg-red-50 p-2">
            <summary className="cursor-pointer text-xs font-medium text-red-800">
              {attrs["exception.type"]}: {attrs["exception.message"]}
            </summary>
            <pre className="mt-2 max-h-60 overflow-auto text-xs text-red-900">{attrs["exception.stacktrace"]}</pre>
          </details>
        );
      })}
      <details>
        <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-slate-500">
          Attributes ({Object.keys(span.attributes).length})
        </summary>
        <pre className="mt-1 max-h-60 overflow-auto rounded bg-slate-50 p-2 text-xs">
          {JSON.stringify(span.attributes, null, 2)}
        </pre>
      </details>
    </div>
  );
}
