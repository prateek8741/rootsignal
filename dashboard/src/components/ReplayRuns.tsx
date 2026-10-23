import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ReplayRun } from "../api/client";
import { formatTime, shortId } from "../lib/format";
import { Badge, Code, ErrorBox, Loading, type Tone } from "./ui";

const VERDICTS: Record<string, { tone: Tone; hint: string }> = {
  failing: { tone: "red", hint: "A 'reproduces' assertion failed: the failure is present in this code" },
  passing: { tone: "green", hint: "Every evaluated assertion passed: the failure is absent in this code" },
  broken: { tone: "orange", hint: "A guard failed: the code changed behaviour in another way" },
  diverged: { tone: "amber", hint: "The run asked for something the recording doesn't have: nothing is concluded" },
  inconclusive: { tone: "amber", hint: "No 'reproduces' assertion can be evaluated in this replay mode" },
  crashed: { tone: "gray", hint: "The replay itself failed (bad entrypoint, import error, timeout)" },
};

interface Result {
  id: string; description: string; role: string; evaluated: boolean;
  passed?: boolean; observed?: string; reason?: string;
}

interface Report {
  served_tools?: number; served_model_calls?: number; live_tools?: number;
  stubs?: { tool: string }[]; exception?: { type: string; message: string } | null;
}

function command(caseId: string, purpose: string) {
  return `rootsignal replay ${caseId} --purpose ${purpose} --stubs your_package.replay_stubs`;
}

function RunRow({ run }: { run: ReplayRun }) {
  const verdict = run.verdict ? VERDICTS[run.verdict] : undefined;
  const report = run.report as Report;
  const results = run.results as unknown as Result[];
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {run.verdict ? <Badge tone={verdict?.tone ?? "gray"} title={verdict?.hint}>{run.verdict}</Badge>
          : <Badge>{run.status.replace("_", " ")}…</Badge>}
        <span className="font-medium">{run.purpose}</span>
        <span className="text-slate-500">v{run.case_version} · {run.mode} · {run.matching}</span>
        <span className="text-slate-500">{formatTime(run.created_at)} · {run.requested_by}</span>
        {run.trace_id && <Link to={`/traces/${run.trace_id}`} className="font-mono text-xs text-sky-700 hover:underline">
          trace {shortId(run.trace_id)}</Link>}
      </div>
      {run.summary && <p className="mt-1 text-sm text-slate-700">{run.summary}</p>}
      {results.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs">
          {results.map((r) => (
            <li key={r.id} className="flex gap-2">
              <span className={`w-10 font-mono ${!r.evaluated ? "text-slate-400" : r.passed ? "text-emerald-700" : "text-red-700"}`}>
                {!r.evaluated ? "skip" : r.passed ? "pass" : "FAIL"}
              </span>
              <span className="font-mono text-slate-500">{r.id}</span>
              <span className="text-slate-700">{r.description}</span>
              <span className="text-slate-500">({r.evaluated ? r.observed : r.reason})</span>
            </li>
          ))}
        </ul>
      )}
      {run.reported_at && (
        <p className="mt-1 text-xs text-slate-500">
          {report.served_tools ?? 0} tool result(s) and {report.served_model_calls ?? 0} model output(s) served from the recording
          {report.stubs?.length ? `, ${report.stubs.length} write call(s) stubbed (never executed)` : ""}
          {report.live_tools ? `, ${report.live_tools} live read(s)` : ""}
          {report.exception ? ` · the agent raised ${report.exception.type}` : ""}
        </p>
      )}
    </li>
  );
}

export function ReplayRuns({ caseId, status }: { caseId: string; status: string }) {
  const query = useQuery({
    queryKey: ["case-replays", caseId],
    queryFn: () => api.caseReplays(caseId),
    refetchInterval: (q) => q.state.data?.items.some((r) => !r.verdict) ? 2000 : false,
  });
  const next = status === "draft" || status === "not_reproduced" ? "reproduce" : "verify";
  return (
    <div className="space-y-3">
      <div className="text-sm text-slate-600">
        <p>
          Replays run your agent code locally against the recording: recorded tool results and model outputs are served,
          write tools are never executed, and network access outside Rootsignal is blocked. Run it with a key that has
          the <Code>replay</Code> scope:
        </p>
        <pre className="mt-2 overflow-x-auto rounded-md bg-slate-900 p-2 font-mono text-xs text-slate-100">{command(caseId, next)}</pre>
        <p className="mt-1 text-xs text-slate-500">
          First <Code>--purpose reproduce</Code> on the unfixed code (must fail), then <Code>--purpose verify</Code> after the fix.
          A case only counts as passing once it has reproduced the failure.
        </p>
      </div>
      {query.isPending ? <Loading /> : query.isError ? <ErrorBox error={query.error} /> :
        query.data.items.length === 0 ? <p className="text-sm text-slate-500">No replays yet.</p> : (
          <ul className="divide-y divide-slate-100">
            {query.data.items.map((run) => <RunRow key={run.id} run={run} />)}
          </ul>
        )}
    </div>
  );
}
