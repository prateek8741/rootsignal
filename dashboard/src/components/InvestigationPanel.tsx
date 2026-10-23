import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Claim, type Investigation } from "../api/client";
import { useSession } from "../lib/auth";
import { formatTime, shortId } from "../lib/format";
import { Badge, Button, Card, ErrorBox, Loading, type Tone } from "./ui";

const SECTIONS: { kind: string; title: string; hint: string }[] = [
  { kind: "observed", title: "Observed", hint: "Read directly off the traces and statistics" },
  { kind: "hypothesis", title: "Hypotheses", hint: "Possible causes. Not confirmed" },
  { kind: "alternative", title: "Alternative explanations", hint: "Competing explanations to rule out" },
  { kind: "missing_evidence", title: "Missing evidence", hint: "What would tell the hypotheses apart" },
  { kind: "next_step", title: "Next steps", hint: "" },
];

const LLM_STATUS: Record<string, { text: string; tone: Tone }> = {
  not_requested: { text: "rules only", tone: "gray" },
  disabled: { text: "AI step unavailable", tone: "gray" },
  queued: { text: "AI step running", tone: "blue" },
  budget_exhausted: { text: "AI budget exhausted", tone: "amber" },
  failed: { text: "AI step failed", tone: "red" },
  completed: { text: "with AI suggestions", tone: "violet" },
};

function RefChip({ reference }: { reference: Record<string, unknown> }) {
  const type = String(reference["type"]);
  const trace = reference["trace_id"] ? String(reference["trace_id"]) : null;
  const chip = "rounded px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-inset";
  if (type === "span" && trace) {
    const span = String(reference["span_id"]);
    return <Link to={`/traces/${trace}?span=${span}`} className={`${chip} text-sky-700 ring-sky-200 hover:bg-sky-50`}>span {shortId(span)}</Link>;
  }
  if ((type === "trace" || type === "finding") && trace) {
    return <Link to={`/traces/${trace}`} className={`${chip} text-sky-700 ring-sky-200 hover:bg-sky-50`}>{type} {shortId(trace)}</Link>;
  }
  if (type === "stat") {
    return <span className={`${chip} text-slate-600 ring-slate-200`} title="A statistic computed from this environment's traces">stat: {String(reference["name"])}</span>;
  }
  return null;
}

function ClaimItem({ claim }: { claim: Claim }) {
  return (
    <li className="text-sm">
      <div className="flex items-start gap-2">
        {claim.source === "llm" && <Badge tone="violet" title="Suggested by the AI model, checked against the evidence">AI</Badge>}
        <span className="text-slate-800">{claim.statement}</span>
      </div>
      {claim.refs.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {claim.refs.map((r, i) => <RefChip key={i} reference={r} />)}
        </div>
      )}
    </li>
  );
}

function InvestigationView({ investigation }: { investigation: Investigation }) {
  const llm = LLM_STATUS[investigation.llm_status] ?? { text: investigation.llm_status, tone: "gray" as Tone };
  if (investigation.status === "failed") {
    return <ErrorBox error={new Error(`Investigation failed: ${investigation.error ?? "unknown error"}`)} />;
  }
  if (investigation.status !== "completed") return <Loading label="Investigating…" />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <Badge tone={llm.tone}>{llm.text}</Badge>
        <span>by {investigation.requested_by} · {formatTime(investigation.completed_at)}</span>
        {investigation.llm_error && <span title={investigation.llm_error}>({investigation.llm_error})</span>}
        {investigation.llm_status === "completed" && (
          <span>{investigation.llm_model} · {investigation.llm_input_tokens + investigation.llm_output_tokens} tokens</span>
        )}
      </div>
      {SECTIONS.map(({ kind, title, hint }) => {
        const claims = investigation.claims.filter((c) => c.kind === kind);
        if (claims.length === 0) return null;
        return (
          <section key={kind} aria-label={title}>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {title} {hint && <span className="font-normal normal-case tracking-normal text-slate-400">· {hint}</span>}
            </h3>
            <ul className="mt-2 space-y-2">{claims.map((c, i) => <ClaimItem key={i} claim={c} />)}</ul>
          </section>
        );
      })}
      {investigation.rejected_claims.length > 0 && (
        <details className="text-xs text-slate-500">
          <summary className="cursor-pointer">
            {investigation.rejected_claims.length} suggestion(s) discarded by validation
          </summary>
          <ul className="mt-1 list-disc pl-5">
            {investigation.rejected_claims.map((r, i) => (
              <li key={i}>{String(r["reason"])}: “{String((r["claim"] as Record<string, unknown>)?.["statement"] ?? "")}”</li>
            ))}
          </ul>
        </details>
      )}
      <p className="text-xs text-slate-500">
        Nothing here is a confirmed root cause. A cause counts as verified only when a replay of
        the failing trace passes after a fix.
      </p>
    </div>
  );
}

export function InvestigationPanel({ issueId }: { issueId: string }) {
  const context = useSession();
  const queryClient = useQueryClient();
  const canTriage = context.scopes.includes("triage");
  const [useLlm, setUseLlm] = useState(false);

  const list = useQuery({ queryKey: ["issue", issueId, "investigations"], queryFn: () => api.investigations(issueId) });
  const latestId = list.data?.items[0]?.id ?? null;
  const latest = useQuery({
    queryKey: ["investigation", latestId],
    queryFn: () => api.investigation(latestId as string),
    enabled: latestId !== null,
    refetchInterval: (q) => (q.state.data && ["completed", "failed"].includes(q.state.data.status) ? false : 1500),
  });
  const start = useMutation({
    mutationFn: () => api.startInvestigation(issueId, useLlm && context.llm_available),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["issue", issueId] });
    },
  });
  const running = latest.data !== undefined && !["completed", "failed"].includes(latest.data.status);

  return (
    <Card title="Investigation" actions={
      <div className="flex items-center gap-3">
        {context.llm_available && (
          <label className="flex items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
            Also ask the AI model
          </label>
        )}
        <Button variant="primary" disabled={!canTriage || start.isPending || running}
          title={canTriage ? undefined : "Needs the 'triage' scope"} onClick={() => start.mutate()}>
          {running ? "Investigating…" : latestId ? "Investigate again" : "Run investigation"}
        </Button>
      </div>
    }>
      {start.isError && <ErrorBox error={start.error} />}
      {list.isPending ? <Loading /> : list.isError ? <ErrorBox error={list.error} /> :
        latestId === null ? (
          <p className="text-sm text-slate-600">
            An investigation gathers evidence from this issue's traces and from other agents and
            versions in the same window, then lists what was observed, possible causes,
            alternatives, missing evidence and next steps, each linked to the spans it rests on.
            {!context.llm_available && " (No AI model is configured; rules only.)"}
          </p>
        ) : latest.isPending ? <Loading /> : latest.isError ? <ErrorBox error={latest.error} /> :
          <InvestigationView investigation={latest.data} />}
    </Card>
  );
}
