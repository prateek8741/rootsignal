import type { IssueSummary, Span, TraceSummary } from "../api/client";

export function span(overrides: Partial<Span> & { span_id: string }): Span {
  return {
    parent_span_id: null, kind: "tool", name: "execute_tool lookup", status: "unset",
    status_message: null, error_type: null, started_at: "2026-10-23T10:00:00.000Z",
    ended_at: "2026-10-23T10:00:01.000Z", duration_ms: 1000, model: null, provider: null,
    input_tokens: null, output_tokens: null, cost_usd: null, tool_name: null, side_effect: null,
    args_hash: null, call_index: null, repeat_index: null, input_payload_hash: null,
    output_payload_hash: null, attributes: {}, events: [], dialect: "rootsignal",
    ...overrides,
  };
}

export function traceSummary(overrides: Partial<TraceSummary> = {}): TraceSummary {
  return {
    trace_id: "0af7651916cd43dd8448eb211c80319c", root_span_id: "a".repeat(16),
    service_name: "support", agent_name: "support-agent", agent_version: "v1",
    prompt_version: null, conversation_id: null, status: "unset", source: "production",
    started_at: "2026-10-23T10:00:00Z", ended_at: "2026-10-23T10:00:04Z", duration_ms: 4000,
    span_count: 6, error_count: 3, llm_call_count: 2, tool_call_count: 3, input_tokens: 900,
    output_tokens: 100, cost_usd: "0.000000", unpriced_llm_call_count: 0,
    completed_at: "2026-10-23T10:00:05Z", completion_reason: "root_span", finding_count: 2,
    ...overrides,
  };
}

export function issueSummary(overrides: Partial<IssueSummary> = {}): IssueSummary {
  return {
    id: "6b1f0c1e-0000-4000-8000-000000000001", fingerprint: "f".repeat(32),
    components: { detector: "tool_error" }, detector: "tool_error", agent_name: "support-agent",
    tool_name: "get_account", error_type: "UpstreamTimeout",
    title: "'get_account' fails with UpstreamTimeout in support-agent",
    description: "A tool call fails (other than by timing out).", severity: "medium",
    status: "open", is_regression: false, regression_count: 0, last_regressed_at: null,
    first_seen_at: "2026-10-23T09:00:00Z", last_seen_at: "2026-10-23T10:00:00Z",
    occurrence_count: 4, affected_versions: ["v1"], first_trace_id: null,
    latest_trace_id: null, worst_trace_id: null, resolved_at: null, fixed_in_version: null,
    created_at: "2026-10-23T09:00:00Z", updated_at: "2026-10-23T10:00:00Z",
    ...overrides,
  };
}
