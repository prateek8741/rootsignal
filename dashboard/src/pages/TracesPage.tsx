import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, type TraceQuery } from "../api/client";
import { Badge, Button, Code, EmptyState, ErrorBox, Loading, TraceStatusBadge } from "../components/ui";
import { formatCost, formatDuration, formatRelative, formatTokens, shortId } from "../lib/format";
import { useActiveConnection } from "../lib/connection";

const RANGES: Record<string, number | null> = { "1h": 1, "24h": 24, "7d": 24 * 7, all: null };

function sinceFor(range: string): string | undefined {
  const hours = RANGES[range];
  return hours ? new Date(Date.now() - hours * 3600_000).toISOString() : undefined;
}

export function TracesPage() {
  const { apiKey, context } = useActiveConnection();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const agent = params.get("agent") ?? "";
  const version = params.get("version") ?? "";
  // Time range is opt-in: defaulting to "24h" would show "no matches" instead of
  // onboarding to new users, and hide older traces without saying so.
  const range = params.get("range") ?? "all";
  const filtered = Boolean(status || agent || version || range !== "all");

  const query = useInfiniteQuery({
    queryKey: ["traces", apiKey, status, agent, version, range],
    queryFn: ({ pageParam }) => {
      const q: TraceQuery = { status, agent_name: agent, agent_version: version,
        since: sinceFor(range), cursor: pageParam, limit: 50 };
      return api.traces(apiKey, q);
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  function update(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  }

  const traces = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Traces</h1>
        <p className="text-sm text-slate-500">{context.project_name} · {context.environment}</p>
      </div>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">Status</span>
          <select value={status} onChange={(e) => update("status", e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5">
            <option value="">Any</option>
            <option value="error">Error</option>
            <option value="ok">OK</option>
            <option value="unset">Unset</option>
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">Agent</span>
          <input value={agent} placeholder="support-agent" onChange={(e) => update("agent", e.target.value)}
            className="w-44 rounded-md border border-slate-300 px-2 py-1.5" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">Version</span>
          <input value={version} placeholder="v2" onChange={(e) => update("version", e.target.value)}
            className="w-24 rounded-md border border-slate-300 px-2 py-1.5" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-slate-500">Started</span>
          <select value={range} onChange={(e) => update("range", e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5">
            <option value="1h">Last hour</option>
            <option value="24h">Last 24 hours</option>
            <option value="7d">Last 7 days</option>
            <option value="all">All time</option>
          </select>
        </label>
      </div>

      {query.isPending ? <Loading /> : query.isError ? <ErrorBox error={query.error} /> :
        traces.length === 0 ? (
          filtered ? (
            <EmptyState title="No traces match these filters">
              Try a longer time range or clear the filters.
            </EmptyState>
          ) : (
            <EmptyState title="No traces yet">
              <p>Instrument an agent with the Python SDK and run it:</p>
              <pre className="mt-3 overflow-x-auto rounded bg-slate-900 p-3 text-left text-xs text-slate-100">
{`import rootsignal as rs
rs.init(api_key="rs_…", endpoint="http://127.0.0.1:8000")

@rs.agent_run("my-agent", agent_version="v1")
def handle(request): ...`}
              </pre>
              <p className="mt-2">Detection runs in the worker: <Code>uv run rootsignal-server worker</Code></p>
            </EmptyState>
          )
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2">Trace</th><th className="px-3 py-2">Agent</th>
                  <th className="px-3 py-2">Status</th><th className="px-3 py-2">Findings</th>
                  <th className="px-3 py-2 text-right">Duration</th><th className="px-3 py-2 text-right">Spans</th>
                  <th className="px-3 py-2 text-right">Tokens</th><th className="px-3 py-2 text-right">Cost</th>
                  <th className="px-3 py-2">Started</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {traces.map((t) => (
                  <tr key={t.trace_id} className="hover:bg-slate-50">
                    <td className="px-3 py-2 font-mono text-xs">
                      <Link to={`/traces/${t.trace_id}`} className="text-sky-700 hover:underline">{shortId(t.trace_id)}</Link>
                    </td>
                    <td className="px-3 py-2">
                      {t.agent_name ?? <span className="text-slate-400">—</span>}
                      {t.agent_version && <span className="ml-1 text-xs text-slate-500">{t.agent_version}</span>}
                    </td>
                    <td className="px-3 py-2"><TraceStatusBadge status={t.status} /></td>
                    <td className="px-3 py-2">
                      {t.finding_count > 0 ? <Badge tone="orange">{t.finding_count}</Badge>
                        : t.completed_at ? <span className="text-slate-400">0</span>
                        : <span className="text-xs text-slate-400" title="Detection runs once the trace completes">pending</span>}
                      {t.error_count > 0 && <span className="ml-2 text-xs text-red-600">{t.error_count} errored span{t.error_count > 1 ? "s" : ""}</span>}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatDuration(t.duration_ms)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{t.span_count}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatTokens(t.input_tokens + t.output_tokens)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatCost(t.cost_usd, t.unpriced_llm_call_count)}</td>
                    <td className="px-3 py-2 text-slate-500" title={t.started_at}>{formatRelative(t.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      {query.hasNextPage && (
        <Button onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
          {query.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
    </div>
  );
}
