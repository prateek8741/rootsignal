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

export async function request<T>(path: string, apiKey: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { ...init?.headers, Authorization: `Bearer ${apiKey}` },
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
  return (await response.json()) as T;
}

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
  context: (key: string) => request<Context>("/v1/context", key),
  traces: (key: string, q: TraceQuery) => request<TraceList>(withQuery("/v1/traces", q), key),
  trace: (key: string, traceId: string) => request<TraceDetail>(`/v1/traces/${traceId}`, key),
  payload: (key: string, sha256: string) => request<Payload>(`/v1/payloads/${sha256}`, key),
  issues: (key: string, q: IssueQuery) => request<IssueList>(withQuery("/v1/issues", q), key),
  issue: (key: string, id: string) => request<IssueDetail>(`/v1/issues/${id}`, key),
  occurrences: (key: string, id: string, cursor?: string) =>
    request<OccurrenceList>(withQuery(`/v1/issues/${id}/occurrences`, { cursor }), key),
  setStatus: (key: string, id: string, change: StatusChange) =>
    request<IssueSummary>(`/v1/issues/${id}/status`, key, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    }),
};
