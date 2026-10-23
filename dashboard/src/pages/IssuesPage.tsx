import { useInfiniteQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, type IssueQuery } from "../api/client";
import { Badge, Button, EmptyState, ErrorBox, IssueStatusBadge, Loading, SeverityBadge } from "../components/ui";
import { formatRelative } from "../lib/format";
import { useActiveConnection } from "../lib/connection";

const STATUSES = ["open", "resolved", "ignored", "all"] as const;

export function IssuesPage() {
  const { apiKey, context } = useActiveConnection();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "open";
  const severity = params.get("severity") ?? "";
  const sort = (params.get("sort") ?? "last_seen") as NonNullable<IssueQuery["sort"]>;

  const query = useInfiniteQuery({
    queryKey: ["issues", apiKey, status, severity, sort],
    queryFn: ({ pageParam }) => api.issues(apiKey, {
      status: status === "all" ? undefined : status, severity, sort, cursor: pageParam, limit: 50,
    }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  function update(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  }

  const issues = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Issues</h1>
        <p className="text-sm text-slate-500">{context.project_name} · {context.environment}</p>
      </div>
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <div role="tablist" aria-label="Status" className="flex rounded-md ring-1 ring-slate-300">
          {STATUSES.map((s) => (
            <button key={s} type="button" role="tab" aria-selected={status === s}
              onClick={() => update("status", s === "open" ? "" : s)}
              className={`px-3 py-1.5 capitalize first:rounded-l-md last:rounded-r-md ${status === s ? "bg-slate-900 text-white" : "bg-white hover:bg-slate-50"}`}>
              {s}
            </button>
          ))}
        </div>
        <select aria-label="Severity" value={severity} onChange={(e) => update("severity", e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5">
          <option value="">Any severity</option>
          {["critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select aria-label="Sort" value={sort} onChange={(e) => update("sort", e.target.value === "last_seen" ? "" : e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5">
          <option value="last_seen">Most recent</option>
          <option value="occurrences">Most frequent</option>
          <option value="severity">Most severe</option>
        </select>
      </div>

      {query.isPending ? <Loading /> : query.isError ? <ErrorBox error={query.error} /> :
        issues.length === 0 ? (
          <EmptyState title={status === "open" ? "No open issues" : `No ${status === "all" ? "" : `${status} `}issues`}>
            Issues appear when the worker's detectors find failures in completed traces,
            grouped so repeated failures become one issue.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-3 py-2">Severity</th><th className="px-3 py-2">Issue</th>
                  <th className="px-3 py-2">Status</th><th className="px-3 py-2 text-right">Traces</th>
                  <th className="px-3 py-2">Versions</th><th className="px-3 py-2">Last seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {issues.map((i) => (
                  <tr key={i.id} className="hover:bg-slate-50">
                    <td className="px-3 py-2"><SeverityBadge severity={i.severity} /></td>
                    <td className="px-3 py-2">
                      <Link to={`/issues/${i.id}`} className="font-medium text-sky-700 hover:underline">{i.title}</Link>
                      <div className="text-xs text-slate-500">{i.detector}</div>
                    </td>
                    <td className="px-3 py-2"><IssueStatusBadge status={i.status} regression={i.is_regression} /></td>
                    <td className="px-3 py-2 text-right tabular-nums">{i.occurrence_count}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">{i.affected_versions.map((v) => <Badge key={v}>{v}</Badge>)}</div>
                    </td>
                    <td className="px-3 py-2 text-slate-500" title={i.last_seen_at}>{formatRelative(i.last_seen_at)}</td>
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
