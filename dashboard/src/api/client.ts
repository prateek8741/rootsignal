import type { components } from "./schema";

type Schemas = components["schemas"];
export type Context = Schemas["Context"];
export type TraceSummary = Schemas["TraceSummary"];
export type TraceList = Schemas["TraceList"];
export type TraceDetail = Schemas["TraceDetail"];
export type Span = Schemas["SpanOut"];
export type Finding = Schemas["FindingOut"];
export type Payload = Schemas["PayloadOut"];
export type IssueSummary = Schemas["IssueSummary"];
export type IssueList = Schemas["IssueList"];
export type IssueDetail = Schemas["IssueDetail"];
export type IssueEvent = Schemas["IssueEventOut"];
export type Occurrence = Schemas["OccurrenceOut"];
export type OccurrenceList = Schemas["OccurrenceList"];
export type StatusChange = Schemas["StatusChange"];
export type Investigation = Schemas["InvestigationDetail"];
export type InvestigationSummary = Schemas["InvestigationSummary"];
export type InvestigationList = Schemas["InvestigationList"];
export type Claim = Schemas["ClaimOut"];
export type CaseSummary = Schemas["CaseSummary"];
export type CaseDetail = Schemas["CaseDetail"];
export type CaseList = Schemas["CaseList"];
export type ReplayRun = Schemas["ReplayRunOut"];
export type ReplayRunList = Schemas["ReplayRunList"];

/** The regression case spec (see server/src/rootsignal_server/regression/spec.py). */
export interface CaseAssertion {
  id: string;
  kind: string;
  params: Record<string, unknown>;
  description: string;
  role: "reproduces" | "guard";
  requires: "recorded" | "live_tools" | "live_model" | "live";
  enabled: boolean;
}
export interface RecordedValue { captured: boolean; value?: unknown; truncated?: boolean; redacted?: boolean }
export interface CaseSpec {
  name: string;
  entrypoint: string | null;
  source: { trace_id: string; agent_name: string | null; agent_version: string | null; started_at: string };
  input: { kind: "kwargs" | "value" | "missing"; value?: unknown; supplied_by_user?: boolean; redacted?: boolean; truncated?: boolean };
  fixtures: {
    tools: { call_index: number; tool: string; side_effect: string; repeat_index: number | null;
      arguments: RecordedValue; outcome: "result" | "error"; result?: RecordedValue | null;
      error_type?: string | null; error_message?: string | null }[];
    llm: { call_index: number; model: string | null; output: RecordedValue; failed?: boolean }[];
    retrievals: unknown[];
  };
  assertions: CaseAssertion[];
  quality: { replayable_recorded: boolean; warnings: string[]; dropped_assertions: { description: string; reason: string }[] };
}

export interface CaseEdit {
  name?: string;
  entrypoint?: string | null;
  input?: unknown;
  disabled_assertions?: string[];
  note?: string;
}

/** An error response from the API (or the network). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(name, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * Authentication is an httpOnly session cookie the browser attaches by itself:
 * this code never sees or stores a credential. Every request carries the CSRF
 * header the API requires for cookie-authenticated changes.
 */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: { ...init?.headers, "X-Rootsignal-CSRF": "1" },
    });
  } catch {
    throw new ApiError(0, "network", "Can't reach the Rootsignal API. Is the server running?");
  }
  if (!response.ok) {
    let code = "http_error";
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { error?: { code?: string; message?: string } };
      code = body.error?.code ?? code;
      message = body.error?.message ?? message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, code, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export interface TraceQuery extends Query {
  status?: string;
  source?: string;
  agent_name?: string;
  agent_version?: string;
  since?: string;
  cursor?: string;
  limit?: number;
}

export interface IssueQuery extends Query {
  status?: string;
  severity?: string;
  detector?: string;
  regression?: boolean;
  sort?: "last_seen" | "occurrences" | "severity";
  cursor?: string;
  limit?: number;
}

export const api = {
  session: () => request<Context>("/v1/auth/session"),
  /** The key is sent once and immediately exchanged for a session cookie. */
  signInWithKey: (apiKey: string) => request<Context>("/v1/auth/session", json({ api_key: apiKey })),
  signInWithLink: (token: string) => request<Context>("/v1/auth/link", json({ token })),
  signOut: () => request<void>("/v1/auth/session", { method: "DELETE" }),

  traces: (q: TraceQuery) => request<TraceList>(withQuery("/v1/traces", q)),
  trace: (traceId: string) => request<TraceDetail>(`/v1/traces/${traceId}`),
  payload: (sha256: string) => request<Payload>(`/v1/payloads/${sha256}`),
  issues: (q: IssueQuery) => request<IssueList>(withQuery("/v1/issues", q)),
  issue: (id: string) => request<IssueDetail>(`/v1/issues/${id}`),
  occurrences: (id: string, cursor?: string) =>
    request<OccurrenceList>(withQuery(`/v1/issues/${id}/occurrences`, { cursor })),
  setStatus: (id: string, change: StatusChange) =>
    request<IssueSummary>(`/v1/issues/${id}/status`, json(change)),
  startInvestigation: (issueId: string, useLlm: boolean) =>
    request<Investigation>(`/v1/issues/${issueId}/investigations`, json({ use_llm: useLlm })),
  investigations: (issueId: string) =>
    request<InvestigationList>(`/v1/issues/${issueId}/investigations`),
  investigation: (id: string) => request<Investigation>(`/v1/investigations/${id}`),
  issueCases: (issueId: string) => request<CaseList>(`/v1/issues/${issueId}/regression-cases`),
  createCase: (issueId: string) => request<CaseDetail>(`/v1/issues/${issueId}/regression-cases`, json({})),
  regressionCase: (id: string, version?: number) =>
    request<CaseDetail>(withQuery(`/v1/regression-cases/${id}`, { version })),
  caseSpecDocument: (id: string, version: number) =>
    request<Record<string, unknown>>(withQuery(`/v1/regression-cases/${id}/spec`, { version })),
  caseReplays: (id: string) => request<ReplayRunList>(`/v1/regression-cases/${id}/replays`),
  editCase: (id: string, change: CaseEdit) =>
    request<CaseDetail>(`/v1/regression-cases/${id}`, { ...json(change), method: "PATCH" }),
};
