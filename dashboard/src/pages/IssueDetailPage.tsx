import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type IssueEvent } from "../api/client";
import { IssueActions } from "../components/IssueActions";
import { Badge, Button, Card, ErrorBox, IssueStatusBadge, Loading, SeverityBadge, Stat } from "../components/ui";
import { formatRelative, formatTime, shortId } from "../lib/format";
import { useActiveConnection } from "../lib/connection";

const EVENT_TEXT: Record<string, string> = {
  created: "Issue created", status_changed: "Status changed",
  regressed: "Reopened as a regression", straggler: "Occurrence from an old version (straggler)",
};

function EventRow({ event }: { event: IssueEvent }) {
  const details = event.details as Record<string, string | undefined>;
  return (
    <li className="flex gap-3 text-sm">
      <span className="w-36 shrink-0 text-xs text-slate-500" title={event.created_at}>{formatTime(event.created_at)}</span>
      <div>
        <span className="font-medium">{EVENT_TEXT[event.kind] ?? event.kind}</span>
        {event.from_status && event.to_status && (
          <span className="text-slate-600"> · {event.from_status} → {event.to_status}</span>
        )}
        <span className="text-slate-500"> · by {event.actor}</span>
        {details["agent_version"] && <span className="text-slate-600"> · version {details["agent_version"]}</span>}
        {details["fixed_in_version"] && <span className="text-slate-600"> · fixed in {details["fixed_in_version"]}</span>}
        {details["note"] && <p className="mt-0.5 text-slate-700">“{details["note"]}”</p>}
        {details["trace_id"] && (
          <Link to={`/traces/${details["trace_id"]}`} className="ml-1 text-xs text-sky-700 hover:underline">trace</Link>
        )}
      </div>
    </li>
  );
}

export function IssueDetailPage() {
  const { issueId = "" } = useParams();
  const { apiKey } = useActiveConnection();
  const query = useQuery({ queryKey: ["issue", apiKey, issueId], queryFn: () => api.issue(apiKey, issueId) });
  const occurrences = useInfiniteQuery({
    queryKey: ["issue", apiKey, issueId, "occurrences"],
    queryFn: ({ pageParam }) => api.occurrences(apiKey, issueId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const { issue, versions, events } = query.data;
  const representative = [
    ["First", issue.first_trace_id], ["Latest", issue.latest_trace_id], ["Worst", issue.worst_trace_id],
  ] as const;

  return (
    <div className="space-y-4">
      <div>
        <Link to="/issues" className="text-sm text-sky-700 hover:underline">← Issues</Link>
        <h1 className="mt-1 flex flex-wrap items-center gap-2 text-xl font-semibold">
          <SeverityBadge severity={issue.severity} />
          {issue.title}
          <IssueStatusBadge status={issue.status} regression={issue.is_regression} />
        </h1>
        <p className="mt-1 text-sm text-slate-600">{issue.description}</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card>
          <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <Stat label="Traces" value={issue.occurrence_count} />
            <Stat label="First seen" value={formatRelative(issue.first_seen_at)} hint={issue.first_seen_at} />
            <Stat label="Last seen" value={formatRelative(issue.last_seen_at)} hint={issue.last_seen_at} />
            <Stat label="Regressions" value={issue.regression_count} />
            <Stat label="Detector" value={issue.detector} />
            {issue.tool_name && <Stat label="Tool" value={issue.tool_name} />}
            {issue.error_type && <Stat label="Error" value={issue.error_type} />}
            {issue.fixed_in_version && <Stat label="Fixed in" value={issue.fixed_in_version} />}
          </dl>
          <div className="mt-4 flex flex-wrap gap-2 text-sm">
            {representative.map(([label, id]) => id && (
              <Link key={label} to={`/traces/${id}`}
                className="rounded-md px-2 py-1 text-sky-700 ring-1 ring-slate-200 hover:bg-slate-50">
                {label} trace <span className="font-mono text-xs">{shortId(id)}</span>
              </Link>
            ))}
          </div>
        </Card>
        <Card title="Actions"><IssueActions issue={issue} /></Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Affected versions">
          {versions.length === 0 ? <p className="text-sm text-slate-500">No occurrences.</p> : (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-slate-100">
                {versions.map((v) => (
                  <tr key={v.agent_version ?? "none"}>
                    <td className="py-1.5">{v.agent_version ? <Badge>{v.agent_version}</Badge> : <span className="text-slate-400">no version</span>}</td>
                    <td className="py-1.5 text-right tabular-nums">{v.occurrences} trace{v.occurrences === 1 ? "" : "s"}</td>
                    <td className="py-1.5 pl-3 text-right text-slate-500">last {formatRelative(v.last_seen_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="History">
          <ol className="space-y-2">{events.map((e, i) => <EventRow key={i} event={e} />)}</ol>
        </Card>
      </div>
      <Card title="Occurrences">
        {occurrences.isPending ? <Loading /> : occurrences.isError ? <ErrorBox error={occurrences.error} /> : (
          <>
            <ul className="divide-y divide-slate-100 text-sm">
              {occurrences.data.pages.flatMap((p) => p.items).map((o) => (
                <li key={o.id} className="flex flex-wrap items-center gap-3 py-2">
                  <Link to={`/traces/${o.trace_id}`} className="font-mono text-xs text-sky-700 hover:underline">{shortId(o.trace_id)}</Link>
                  <SeverityBadge severity={o.severity} />
                  {o.agent_version && <Badge>{o.agent_version}</Badge>}
                  <span className="min-w-0 flex-1 truncate text-slate-700" title={o.finding_summary}>{o.finding_summary}</span>
                  <span className="text-xs text-slate-500" title={o.occurred_at}>{formatRelative(o.occurred_at)}</span>
                </li>
              ))}
            </ul>
            {occurrences.hasNextPage && (
              <Button className="mt-3" onClick={() => void occurrences.fetchNextPage()} disabled={occurrences.isFetchingNextPage}>
                Load more
              </Button>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
