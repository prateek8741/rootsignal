import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type CaseAssertion, type CaseDetail, type CaseEdit, type CaseSpec, type RecordedValue } from "../api/client";
import { ReplayRuns } from "../components/ReplayRuns";
import { Badge, Button, Card, Code, ErrorBox, Loading, Stat, type Tone } from "../components/ui";
import { useSession } from "../lib/auth";
import { formatTime, shortId } from "../lib/format";

const REQUIRES: Record<string, { label: string; tone: Tone; hint: string }> = {
  recorded: { label: "recorded", tone: "green", hint: "Can pass when replayed with the recorded tool results and model outputs" },
  live_tools: { label: "live tools", tone: "amber", hint: "Replaying a recorded tool error can't make the tool succeed: needs the real tool" },
  live_model: { label: "live model", tone: "amber", hint: "Depends on what the model does: needs a live model call" },
  live: { label: "live", tone: "amber", hint: "Depends on real timing: needs live tools and model" },
};

const CASE_STATUS: Record<string, Tone> = {
  reproduced: "red", failing: "red", passing: "green", not_reproduced: "amber",
};

function short(value: unknown, max = 80): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text === undefined ? "—" : text.length > max ? `${text.slice(0, max)}…` : text;
}

function Fidelity({ value }: { value: RecordedValue | null | undefined }) {
  if (!value || !value.captured) return <Badge tone="amber">not captured</Badge>;
  return (
    <span className="inline-flex gap-1">
      {value.truncated && <Badge tone="amber">truncated</Badge>}
      {value.redacted && <Badge tone="violet">redacted</Badge>}
    </span>
  );
}

function download(document: Record<string, unknown>, filename: string) {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = Object.assign(window.document.createElement("a"), { href: url, download: filename });
  anchor.click();
  URL.revokeObjectURL(url);
}

function EditForms({ detail, spec, onSaved }: { detail: CaseDetail; spec: CaseSpec; onSaved: () => void }) {
  const [entrypoint, setEntrypoint] = useState(spec.entrypoint ?? "");
  const [input, setInput] = useState(spec.input.kind === "missing" ? "" : JSON.stringify(spec.input.value, null, 2));
  const [note, setNote] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (change: CaseEdit) => api.editCase(detail.id, change),
    onSuccess: () => {
      setNote("");
      onSaved();
    },
  });

  function submit(event: FormEvent, change: CaseEdit) {
    event.preventDefault();
    save.mutate({ ...change, ...(note.trim() ? { note: note.trim() } : {}) });
  }

  function submitInput(event: FormEvent) {
    try {
      const value: unknown = JSON.parse(input);
      setInputError(null);
      submit(event, { input: value });
    } catch {
      event.preventDefault();
      setInputError("Input must be valid JSON (an object becomes keyword arguments).");
    }
  }

  return (
    <div className="space-y-4">
      <form className="space-y-1" onSubmit={(e) => submit(e, { entrypoint: entrypoint.trim() || null })}>
        <label className="text-xs font-medium uppercase tracking-wide text-slate-500" htmlFor="entrypoint">Entrypoint</label>
        <div className="flex gap-2">
          <input id="entrypoint" value={entrypoint} onChange={(e) => setEntrypoint(e.target.value)}
            placeholder="package.module:function" spellCheck={false}
            className="flex-1 rounded-md border border-slate-300 px-2 py-1 font-mono text-sm" />
          <Button type="submit" disabled={save.isPending || entrypoint.trim() === (spec.entrypoint ?? "")}>Save</Button>
        </div>
      </form>
      <form className="space-y-1" onSubmit={submitInput}>
        <label className="text-xs font-medium uppercase tracking-wide text-slate-500" htmlFor="case-input">Input (JSON)</label>
        <textarea id="case-input" rows={4} value={input} onChange={(e) => setInput(e.target.value)} spellCheck={false}
          placeholder='{"request": "…"}' className="w-full rounded-md border border-slate-300 px-2 py-1 font-mono text-xs" />
        {inputError && <p role="alert" className="text-xs text-red-700">{inputError}</p>}
        <Button type="submit" disabled={save.isPending || !input.trim()}>Save input</Button>
      </form>
      <label className="block text-xs text-slate-500">
        Change note (optional)
        <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500}
          className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </label>
      <p className="text-xs text-slate-500">Every change creates a new version; earlier versions stay available.</p>
      {save.isError && <ErrorBox error={save.error} />}
    </div>
  );
}

function AssertionsTable({ detail, spec, editable, onSaved }: {
  detail: CaseDetail; spec: CaseSpec; editable: boolean; onSaved: () => void;
}) {
  const [disabled, setDisabled] = useState<Set<string>>(new Set(spec.assertions.filter((a) => !a.enabled).map((a) => a.id)));
  const save = useMutation({
    mutationFn: () => api.editCase(detail.id, { disabled_assertions: [...disabled] }),
    onSuccess: onSaved,
  });
  const dirty = spec.assertions.some((a) => disabled.has(a.id) === a.enabled);
  const toggle = (a: CaseAssertion) => {
    const next = new Set(disabled);
    if (next.has(a.id)) next.delete(a.id);
    else next.add(a.id);
    setDisabled(next);
  };
  return (
    <div className="space-y-2">
      <table className="w-full text-sm">
        <tbody className="divide-y divide-slate-100">
          {spec.assertions.map((a) => {
            const req = REQUIRES[a.requires] ?? { label: a.requires, tone: "gray" as Tone, hint: "" };
            return (
              <tr key={a.id} className={disabled.has(a.id) ? "opacity-50" : ""}>
                <td className="w-8 py-2 align-top">
                  <input type="checkbox" aria-label={`Enable ${a.id}`} checked={!disabled.has(a.id)}
                    disabled={!editable} onChange={() => toggle(a)} />
                </td>
                <td className="py-2 align-top font-mono text-xs text-slate-500">{a.id}</td>
                <td className="py-2 align-top">
                  <div className="text-slate-800">{a.description}</div>
                  <div className="mt-0.5 font-mono text-[11px] text-slate-500">{a.kind} {JSON.stringify(a.params)}</div>
                </td>
                <td className="py-2 text-right align-top">
                  <span className="inline-flex gap-1">
                    <Badge tone={a.role === "reproduces" ? "red" : "blue"}
                      title={a.role === "reproduces" ? "Fails on the source run: captures the bug" : "Passes on the source run: protects against side effects of a fix"}>
                      {a.role}
                    </Badge>
                    <Badge tone={req.tone} title={req.hint}>{req.label}</Badge>
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {editable && dirty && <Button onClick={() => save.mutate()} disabled={save.isPending}>Save assertion selection</Button>}
      {save.isError && <ErrorBox error={save.error} />}
      {spec.quality.dropped_assertions.length > 0 && (
        <details className="text-xs text-slate-500">
          <summary className="cursor-pointer">{spec.quality.dropped_assertions.length} candidate assertion(s) not used</summary>
          <ul className="mt-1 list-disc pl-5">
            {spec.quality.dropped_assertions.map((d, i) => <li key={i}>{d.description}: {d.reason}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}

export function CaseDetailPage() {
  const { caseId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const requested = params.get("version") ? Number(params.get("version")) : undefined;
  const context = useSession();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["case", caseId, requested ?? "current"],
    queryFn: () => api.regressionCase(caseId, requested) });
  const exporting = useMutation({
    mutationFn: (version: number) => api.caseSpecDocument(caseId, version),
    onSuccess: (document, version) => download(document, `regression-case-${shortId(caseId)}-v${version}.json`),
  });

  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const detail = query.data;
  const spec = detail.spec as unknown as CaseSpec;
  const isCurrent = detail.version === detail.current_version;
  const editable = isCurrent && context.scopes.includes("triage");
  const refresh = () => {
    setParams({}, { replace: true });
    void queryClient.invalidateQueries({ queryKey: ["case", caseId] });
  };

  return (
    <div className="space-y-4">
      <div>
        <Link to={`/issues/${detail.issue_id}`} className="text-sm text-sky-700 hover:underline">← Issue</Link>
        <h1 className="mt-1 flex flex-wrap items-center gap-2 text-xl font-semibold">
          {spec.name}
          <Badge tone={CASE_STATUS[detail.status] ?? "gray"}>{detail.status.replace("_", " ")}</Badge>
          <Badge>v{detail.version}{isCurrent ? "" : ` of ${detail.current_version}`}</Badge>
          {spec.quality.replayable_recorded ? <Badge tone="green">replayable</Badge> : <Badge tone="amber">needs attention</Badge>}
        </h1>
        {!isCurrent && <p className="text-sm text-amber-700">You're viewing an older version (read-only).</p>}
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card title="Case">
          <dl className="grid grid-cols-2 gap-4 md:grid-cols-3">
            <Stat label="Source trace" value={<Link to={`/traces/${spec.source.trace_id}`} className="font-mono text-sky-700 hover:underline">{shortId(spec.source.trace_id)}</Link>} />
            <Stat label="Agent" value={`${spec.source.agent_name ?? "—"} ${spec.source.agent_version ?? ""}`} />
            <Stat label="Recorded" value={formatTime(spec.source.started_at)} />
            <Stat label="Entrypoint" value={spec.entrypoint ? <span className="break-all"><Code>{spec.entrypoint}</Code></span> : <span className="text-amber-700">not set</span>} />
            <Stat label="Input" value={spec.input.kind === "missing" ? <span className="text-amber-700">missing</span>
              : <span title={JSON.stringify(spec.input.value)}>{short(spec.input.value, 40)}{spec.input.supplied_by_user ? " (supplied)" : ""}</span>} />
            <Stat label="Created by" value={detail.created_by} />
          </dl>
          {spec.quality.warnings.length > 0 && (
            <ul className="mt-4 space-y-1 rounded-md bg-amber-50 p-3 text-sm text-amber-900">
              {spec.quality.warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}
            </ul>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Button onClick={() => exporting.mutate(detail.version)} disabled={exporting.isPending}>Download JSON</Button>
          </div>
          {exporting.isError && <ErrorBox error={exporting.error} />}
        </Card>
        <Card title="Edit">
          {editable ? <EditForms key={detail.version} detail={detail} spec={spec} onSaved={refresh} />
            : <p className="text-sm text-slate-500">{isCurrent ? "Read-only: needs the 'triage' scope." : "Open the current version to edit."}</p>}
        </Card>
      </div>

      <Card title="Replays">
        <ReplayRuns caseId={detail.id} status={detail.status} />
      </Card>

      <Card title={`Assertions (${spec.assertions.filter((a) => a.enabled).length} enabled)`}>
        <AssertionsTable key={detail.version} detail={detail} spec={spec} editable={editable} onSaved={refresh} />
      </Card>

      <Card title={`Recorded tool calls (${spec.fixtures.tools.length}) · model outputs (${spec.fixtures.llm.length})`}>
        {spec.fixtures.tools.length === 0 ? <p className="text-sm text-slate-500">No tool calls in this run.</p> : (
          <table className="w-full text-sm">
            <tbody className="divide-y divide-slate-100">
              {spec.fixtures.tools.map((t) => (
                <tr key={t.call_index}>
                  <td className="py-1.5 font-mono text-xs text-slate-500">#{t.call_index}</td>
                  <td className="py-1.5">
                    {t.tool} {t.side_effect === "write" || t.side_effect === "unknown"
                      ? <Badge tone="violet" title="Never executed during replay: stubbed">{t.side_effect} · stubbed</Badge> : null}
                    {(t.repeat_index ?? 0) > 0 && <Badge tone="orange">repeat #{t.repeat_index}</Badge>}
                  </td>
                  <td className="py-1.5 font-mono text-xs text-slate-600" title={JSON.stringify(t.arguments.value)}>
                    {short(t.arguments.value, 50)} <Fidelity value={t.arguments} />
                  </td>
                  <td className="py-1.5 text-right">
                    {t.outcome === "error" ? <Badge tone="red" title={t.error_message ?? ""}>{t.error_type ?? "error"}</Badge>
                      : <span className="inline-flex items-center gap-1 text-xs text-slate-600">result <Fidelity value={t.result} /></span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Versions">
        <ul className="space-y-1 text-sm">
          {detail.versions.map((v) => (
            <li key={v.version} className="flex flex-wrap gap-2">
              <button type="button" className={`font-mono ${v.version === detail.version ? "font-semibold" : "text-sky-700 underline"}`}
                onClick={() => setParams(v.version === detail.current_version ? {} : { version: String(v.version) })}>
                v{v.version}
              </button>
              <span className="text-slate-500">{formatTime(v.created_at)} · {v.created_by}</span>
              {v.change_note && <span className="text-slate-700">“{v.change_note}”</span>}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
