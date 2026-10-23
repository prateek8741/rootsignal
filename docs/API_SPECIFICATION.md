# API Specification

Status: v0, implemented in increment 2. Base URL for local dev: `http://127.0.0.1:8000`. The machine-readable OpenAPI document is served at `/openapi.json` (interactive docs at `/docs`). This page adds the rules OpenAPI can't express: auth, tenancy, limits and OTLP semantics.

## 1. Authentication and tenancy

- Programs (SDK, scripts) send `Authorization: Bearer <key>`. Browsers use a **session cookie** instead (§1.1); a request with a Bearer header is always authenticated by the key, never by a cookie. `POST /v1/traces` accepts keys only (sessions never have `ingest`).
- Every other `/v1` endpoint requires one of the two. Keys look like `rs_` + 43 URL-safe characters. Only a SHA-256 of the key is stored; the plaintext is shown once, at creation.
- A key belongs to exactly one **org → project → environment** and has scopes from `ingest`, `read`, `triage` (change issue status) and `admin`. `admin` is reserved; no endpoint uses it yet.
- **A key sees only its own environment.** A trace or payload from another tenant, project or environment returns **404**, never 403, so IDs can't be probed.
- Keys are created and revoked with the CLI (no HTTP admin API until user accounts exist, Phase 2). Each action writes an `audit_events` row (as do login-link creation, session creation and session revocation):

```bash
uv run rootsignal-server create-tenant --org acme --project support --environment development
uv run rootsignal-server create-key --org acme --project support --name agent-prod --scopes ingest
uv run rootsignal-server revoke-key rs_AbCdEfGh      # by the 11-character prefix
```

### 1.1 Dashboard sessions

| Endpoint | Purpose |
|---|---|
| `POST /v1/auth/link {"token"}` | Redeem a one-time login link (from `rootsignal-server dashboard-link`) |
| `POST /v1/auth/session {"api_key"}` | Exchange a key with `read` once; the key is not kept |
| `GET /v1/auth/session` | Who is signed in (same body as `GET /v1/context`) |
| `DELETE /v1/auth/session` | Sign out; revokes the session server-side and clears the cookie |

- **Successful sign-in** sets `rs_session=<random>; HttpOnly; SameSite=Strict; Path=/v1; Max-Age=…` (plus `Secure` when `ROOTSIGNAL_SESSION_COOKIE_SECURE=true`, required behind HTTPS). Only a SHA-256 of the token is stored.
- **Session scopes** are the key's or link's scopes ∩ {read, triage}.
- **Expiry:** 12 h absolute, 2 h idle. Revoking the issuing key also ends the session.
- **CSRF header:** cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` requests, and both sign-in calls, must send `X-Rootsignal-CSRF: 1` (otherwise 403 `csrf`).
- **Rate limit:** sign-in endpoints are limited per client IP (default 30/min).

`GET /v1/context` (and the session endpoint) return `{org_slug, org_name, project_slug, project_name, environment, scopes, key_prefix, principal, llm_available}`.

## 2. Errors

Non-OTLP error bodies are JSON: `{"error": {"code": "<code>", "message": "<text>"}}`.

| Status | `code` | When | Retry? |
|---|---|---|---|
| 400 | `invalid_request`, `invalid_cursor` | Undecodable OTLP body, bad hex ID, bad cursor | No |
| 401 | `unauthenticated` | Missing, malformed, unknown or revoked key (`WWW-Authenticate: Bearer`) | No |
| 403 | `forbidden` | Key lacks the endpoint's scope | No |
| 403 | `csrf` | Cookie-authenticated change (or sign-in) without `X-Rootsignal-CSRF: 1` | No |
| 401 | `invalid_link` | Login link unknown, expired or already used | No |
| 404 | `not_found` | Unknown **or other tenant's** resource | No |
| 409 | `invalid_transition` | Issue status change not allowed (already in that status; `fixed_in_version` without resolving) | No |
| 413 | `payload_too_large` | Body or decompressed body over the limit | No |
| 415 | `unsupported_media_type` | Not protobuf or JSON | No |
| 422 | (FastAPI validation) | Malformed path/query parameter, e.g. a non-hex trace ID | No |
| 429 | `rate_limited` | Per-key token bucket empty (`Retry-After`) | Yes |
| 429 | `quota_exceeded` | Project's daily span or byte quota reached (`Retry-After` = seconds to UTC midnight) | After reset |
| 503 | `unavailable` | Database unreachable (`Retry-After: 5`) | Yes |

Deviation from the OTLP spec: OTLP asks for a `google.rpc.Status` body on errors. We return the JSON body above for every content type. Standard exporters act on the status code alone (429/502/503/504 retryable).

## 3. `POST /v1/traces`: ingest (scope `ingest`)

OTLP/HTTP trace export, compatible with any OTLP exporter.

| Aspect | Behaviour |
|---|---|
| Content types | `application/x-protobuf`, `application/json` (OTLP/JSON: trace and span IDs **hex**, per spec) |
| Compression | `Content-Encoding: gzip` or none |
| Size limits | 5 MiB on the wire (`ROOTSIGNAL_MAX_REQUEST_BYTES`), 32 MiB decompressed (`ROOTSIGNAL_MAX_DECOMPRESSED_BYTES`), checked while streaming |
| Success | **200** with an `ExportTraceServiceResponse` in the request's content type. If some spans were invalid: `partial_success.rejected_spans` and an `error_message` listing reasons. The valid spans are stored |
| Span validation | Rejected: zero or wrong-length trace/span ID, wrong-length parent ID, missing start time |
| Idempotency | Spans are insert-only by `(project, trace_id, span_id)`; resending a batch stores nothing new. It is still counted in usage |
| Batches | A trace's spans may arrive in any order over several requests. Trace roll-ups are recomputed from stored spans on every batch |
| Completion | A trace is *complete* once its root span (no parent) is stored. Each batch that touches a complete trace enqueues one `trace_completed` job, deduplicated while queued or running |
| Payload handling | Model/tool/retrieval/run I/O moves from attributes into the `payloads` table, capped at the project's `max_payload_bytes` (default 64 KiB) with `…[truncated]` |
| Redaction | Per project `redact_on_ingest`: `if_not_sdk` (default: redact unless the Rootsignal SDK already did), `always`, `never`. Applies to payloads, string attributes, span events and status messages |
| Attribute bounds | String values ≤ 4,096 chars; ≤ 64 KiB of attributes per span (excess dropped, `rootsignal.server.attributes_truncated = true`) |
| Rate limit | 50 requests/s per key, burst 100, **per server process** |
| Quotas | Per project per UTC day: `max_spans_per_day` (default 100,000) and `max_ingest_bytes_per_day` (default 1 GiB, wire bytes). An over-quota request is rejected whole (nothing stored) |

```bash
# Any OTLP exporter works; with the OTel SDK env vars:
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:8000/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer rs_..."
```

## 4. Read API (scope `read`)

### `GET /v1/context` (any valid key)

`{org_slug, org_name, project_slug, project_name, environment, scopes, key_prefix}`: what the key belongs to and may do. Used by the dashboard's connect screen.

### `GET /v1/traces`

Newest first. Query parameters: `limit` (1–200, default 50), `cursor` (opaque, from the previous page), `status` (`unset|ok|error`), `agent_name`, `agent_version`, `since`, `until` (ISO-8601). Response: `{"items": [TraceSummary], "next_cursor": string|null}`. Keyset pagination on `(started_at, trace_id)` stays stable while new traces arrive.

`TraceSummary` fields: `trace_id`, `root_span_id`, `service_name`, `agent_name`, `agent_version`, `prompt_version`, `conversation_id`, `status`, `source`, `started_at`, `ended_at`, `duration_ms`, `span_count`, `error_count`, `llm_call_count`, `tool_call_count`, `input_tokens`, `output_tokens`, `cost_usd` (decimal string, null if no call was priced), `unpriced_llm_call_count` (if > 0, `cost_usd` is a lower bound), `completed_at`, `completion_reason` (`root_span` | `idle_timeout`), `finding_count`. All timestamps are UTC.

### `GET /v1/traces/{trace_id}`

`trace_id` = 32 lowercase hex. Response `{"trace": TraceSummary, "spans": [Span], "findings": [Finding]}`, spans ordered by start time, findings by severity. `Finding`: `id`, `detector`, `detector_version`, `finding_key`, `severity`, `title`, `summary`, `evidence_span_ids` (always IDs of spans in this trace), `facts`, `tool_name`, `error_type`, `created_at`, `updated_at`. See [DETECTORS.md](DETECTORS.md). Each `Span` has the canonical columns from [TRACE_SCHEMA.md §5.2](TRACE_SCHEMA.md), including `input_payload_hash` / `output_payload_hash` but not the bodies.

### `GET /v1/payloads/{sha256}`

Returns `{sha256, body, byte_size, truncated, content_type, redaction_state}`, but only if a span in the caller's environment references the payload; otherwise 404.

## 5. Issues

See [ISSUES.md](ISSUES.md) for grouping and lifecycle rules.

### `GET /v1/issues` (scope `read`)

Filters: `status` (`open|resolved|ignored`), `severity`, `detector`, `agent_name`, `regression` (bool). `sort`: `last_seen` (default), `occurrences` or `severity`, each with keyset pagination (`limit` ≤ 200, opaque `cursor`). Response `{"items": [IssueSummary], "next_cursor"}`.

`IssueSummary`: `id`, `fingerprint`, `components`, `detector`, `agent_name`, `tool_name`, `error_type`, `title`, `description`, `severity`, `status`, `is_regression`, `regression_count`, `last_regressed_at`, `first_seen_at`, `last_seen_at`, `occurrence_count` (distinct traces), `affected_versions`, `first_trace_id`, `latest_trace_id`, `worst_trace_id`, `resolved_at`, `fixed_in_version`, `created_at`, `updated_at`.

### `GET /v1/issues/{id}` (scope `read`)

`{"issue": IssueSummary, "versions": [{agent_version, occurrences, first_seen_at, last_seen_at}], "events": [{kind, actor, from_status, to_status, details, created_at}], "recent_occurrences": [Occurrence]}`. An `Occurrence` has `id`, `trace_id`, `finding_id`, `agent_version`, `severity`, `occurred_at`, `finding_title`, `finding_summary`; there are up to 20, newest first.

### `GET /v1/issues/{id}/occurrences` (scope `read`)

All occurrences, newest first, keyset-paginated.

### `POST /v1/issues/{id}/status` (scope `triage`)

Body `{"status": "open"|"resolved"|"ignored", "fixed_in_version"?: string, "note"?: string ≤ 2000}`. Returns the updated `IssueSummary` and records a `status_changed` event with actor `api_key:<prefix>`. Errors: 409 for a no-op or invalid combination, 404 for an unknown or other-tenant issue.

Findings in `GET /v1/traces/{id}` carry `issue_id`.

## 6. Investigations

See [INVESTIGATIONS.md](INVESTIGATIONS.md).

- `POST /v1/issues/{id}/investigations {"use_llm": bool}` (scope `triage`) → **202** `Investigation` (status `queued`); the worker runs it.
- `GET /v1/issues/{id}/investigations` (scope `read`) → `{"items": [InvestigationSummary]}` (latest 20).
- `GET /v1/investigations/{id}` (scope `read`) → `Investigation`.

**`Investigation` fields:**
- **Run:** `id`, `issue_id`, `status` (`queued|running|completed|failed`), `requested_by`, `summary`, `evidence_trace_ids`, `error`, `created_at`, `started_at`, `completed_at`.
- **AI step:** `use_llm`, `llm_status` (`not_requested|disabled|queued|budget_exhausted|failed|completed`), `llm_provider`, `llm_model`, `llm_input_tokens`, `llm_output_tokens`, `llm_error`.
- **Claims:** `rejected_claims` (`[{claim, reason}]`), and `claims` (`[{kind, source, statement, refs}]`, ordered observed → hypothesis → alternative → missing_evidence → next_step).

## 7. Operational endpoints (no auth)

`GET /healthz` → `{"status": "ok"}` (process up). `GET /readyz` → 200 if the database answers, else 503.

## 8. Not yet implemented

HTTP key management and user auth (Phase 2), investigation endpoints (increment 6), issue merge/split, editing `detector_config` over HTTP, `/v1/metrics` and `/v1/logs`, OTLP/gRPC, a `google.rpc.Status` error body, and retention enforcement (the `retention_days` setting exists but no deletion job runs yet).
