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
};
