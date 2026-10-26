# Trace Schema

Status: v0.1. SDK and tables since increment 1. Ingest normalisation (OTLP → tables, §5) since increment 2, tested for the `rootsignal` and `gen_ai` dialects, and for `openinference` since increment 10 (on recorded LangGraph traces). API rules are in [API_SPECIFICATION.md](API_SPECIFICATION.md).

Sources of truth: `sdk/python/src/rootsignal/_attributes.py` (wire attributes) and `server/src/rootsignal_server/storage/models.py` (tables, migration `0001`).

## 1. Principles

1. **Standard first.** Spans are OpenTelemetry spans. Wherever the OTel GenAI semantic conventions define an attribute, we use it (keys as shipped in `opentelemetry-semantic-conventions` 0.66b1). `rootsignal.*` attributes carry only what the conventions don't define.
2. **Framework-neutral storage.** Tables hold canonical columns. Dialect-specific attributes stay in the bounded `spans.attributes` JSONB and are never relied on by detectors.
3. **Tenant-scoped identity.** Client-supplied IDs (trace ID, span ID, payload hash) are unique only *within a project*, and every key starts with `project_id`.
4. **Privacy by default.** The end user's request isn't captured unless `capture_user_input=True`. Payloads are redacted in the SDK, and size-bounded, before they leave the process.

## 2. Wire format

- **Transport:** OTLP/HTTP, `POST {endpoint}/v1/traces`. The SDK sends protobuf (`application/x-protobuf`). The server also accepts OTLP/JSON (hex IDs) and gzip.
- **Auth:** `Authorization: Bearer <api key>`. The key determines the org, project and environment. Resource attributes such as `deployment.environment.name` are informational only.
- **Batching and retry:** the OTel `BatchSpanProcessor` (default queue 2,048 spans, batch 256, 1 s schedule) and the OTLP exporter's built-in retry within `export_timeout_s` (default 5 s).
- **Sampling:** head-based, `ParentBased(TraceIdRatioBased(sample_rate))`. A trace is kept or dropped as a whole. Head sampling can drop failing traces; tail sampling is a later item.

## 3. Attributes emitted by the SDK

`P` = payload attribute: emitted only when `capture_payloads=True`, then redacted and truncated (§4).

### 3.1 Resource

| Key | Value |
|---|---|
| `service.name` | `Config.service_name` |
| `deployment.environment.name` | `Config.environment` (informational) |
| `rootsignal.sdk.version` | SDK version |
| `rootsignal.replay.run_id`, `rootsignal.replay.case_id`, `rootsignal.replay.mode` | Set only by the replay child process ([REPLAY.md](REPLAY.md)). The server stores a trace carrying a valid `run_id` as `source = replay` |
| `rootsignal.sdk.redaction` | `pattern` if the SDK redacted payloads, `off` otherwise. The server uses it to set `payloads.redaction_state` and to decide whether to redact again (§5.3) |

### 3.2 All spans

| Key | Notes |
|---|---|
| `rootsignal.span.kind` | `agent` · `llm` · `tool` · `retrieval` |
| `gen_ai.operation.name` | `invoke_agent` · `chat` (or caller-supplied) · `execute_tool` · `retrieval` |
| `error.type` | Exception class `__qualname__`, or the type passed to `record_error()` |
| `rootsignal.payload.truncated_fields` | List of payload keys cut to `max_payload_bytes` |
| `rootsignal.payload.redacted` | `true` if redaction changed any payload on the span |
| Event `exception` | `exception.type`, `exception.message` (≤1 KB), `exception.stacktrace` (≤8 KB), with **message and stack redacted**. The SDK deliberately doesn't use OTel's `record_exception`, which would export them unredacted. |

Span status is `ERROR` on an exception or `record_error()`, and left `UNSET` otherwise, as the OTel spec asks of instrumentation.

### 3.3 Agent run: `invoke_agent {name}`

| Key | Notes |
|---|---|
| `gen_ai.agent.name`, `gen_ai.agent.version` | |
| `rootsignal.prompt.version` | |
| `gen_ai.conversation.id` | |
| `rootsignal.user.id` | Should be a pseudonymous ID; never an e-mail address |
| `rootsignal.run.max_steps` | Configured step limit, used by the step-limit detector |
| `rootsignal.run.entrypoint` | `module:qualname` of a module-level function decorated with `@rs.agent_run` (absent for nested functions and context-manager use); used by regression cases and replay |
| `rootsignal.capture.user_input` | Whether this run's input was allowed to be captured |
| `rootsignal.run.input` | `P`, **and** only if `capture_user_input=True`. With the decorator, the bound call arguments |
| `rootsignal.run.output` | `P`. With the decorator, the return value |

### 3.4 Model call: `{operation} {model}`

| Key | Notes |
|---|---|
| `gen_ai.request.model`, `gen_ai.response.model` | |
| `gen_ai.provider.name`, `gen_ai.system` | Same value; `gen_ai.system` is deprecated upstream but still read by many backends |
| `gen_ai.request.temperature`, `gen_ai.request.max_tokens` | |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | |
| `gen_ai.response.finish_reasons` | String array |
| `rootsignal.llm.call_index` | 0-based order of model calls within the agent run |
| `rootsignal.cost.usd` | Optional override; otherwise the server prices from tokens (increment 2) |
| `gen_ai.input.messages` | `P`. `{role, content}` mappings. With `capture_user_input=False`, user-role `content` becomes `"[not captured]"`, and an unrecognised message shape is dropped entirely |
| `gen_ai.output.messages` | `P` |

### 3.5 Tool call: `execute_tool {name}`

| Key | Notes |
|---|---|
| `gen_ai.tool.name`, `gen_ai.tool.description` | Description defaults to the docstring's first line |
| `rootsignal.tool.side_effect` | `none` · `read` · `write` · `unknown` (default). Drives replay safety: `unknown` is treated as `write` |
| `rootsignal.tool.args_hash` | 16 hex chars, always emitted (even with payloads off) so loops stay detectable. See §4.3 |
| `rootsignal.tool.call_index` | 0-based order of tool calls within the innermost agent run |
| `rootsignal.tool.repeat_index` | How many earlier calls in the run had the same name and `args_hash` |
| `rootsignal.mcp.server` | Set when the tool is served by an MCP server |
| `rootsignal.replay.served`, `rootsignal.replay.stubbed` | Replay only: the result came from the recording, or a write/unknown tool was stubbed instead of executed. Model-call spans served from the recording also carry `rootsignal.replay.served` |
| `gen_ai.tool.call.arguments` | `P`. Bound arguments as a JSON object (`self`/`cls` excluded) |
| `gen_ai.tool.call.result` | `P` |

### 3.6 Retrieval: `retrieval {data_source}`

| Key | Notes |
|---|---|
| `gen_ai.data_source.id` | |
| `rootsignal.retrieval.top_k`, `rootsignal.retrieval.result_count` | |
| `gen_ai.retrieval.query.text` | `P` |
| `gen_ai.retrieval.documents` | `P`. List; `{id, score, content}` mappings recommended |

### 3.7 Spans from OpenInference instrumentors

Third-party instrumentors can export through the SDK's provider: `LangChainInstrumentor().instrument(tracer_provider=rs.tracer_provider())`. The adapter was built from traces recorded with `openinference-instrumentation-langchain` 0.1.79 on LangGraph 1.2 (`server/tests/fixtures/openinference/`, `evaluation/datasets/dogfood_v0/`).

| OpenInference | Canonical |
|---|---|
| `openinference.span.kind` `LLM`, `EMBEDDING` / `TOOL` / `RETRIEVER` / `CHAIN`, `AGENT` | `llm` / `tool` / `retrieval` / `chain`; anything else → `other`. `AGENT` maps to `chain` because the LangChain instrumentor gives that label to any graph node *named* "agent" (one span per step), so it doesn't mark an agent run. Agent runs come from the SDK's `invoke_agent` span |
| `llm.model_name`, `llm.provider` (falls back to `llm.system`) | `model`, `provider`. `llm.system` is the API family, and ChatOllama reports `openai` there |
| `llm.token_count.prompt` / `.completion` | `input_tokens` / `output_tokens` |
| `tool.name` | `tool_name`. `side_effect` is `unknown`: OpenInference doesn't know it |
| `llm.input_messages.<i>.message.*` / `llm.output_messages.<i>.*` | Input / output payloads, as a JSON list of messages (`role`, `content`, `tool_calls` with `id`, `function.name`, `function.arguments`). The flattened attributes are removed |
| `input.value` / `output.value` | Payloads for every other kind. For `llm` spans with messages, `input.value` repeats them in LangChain's serialised form and is dropped |
| `retrieval.documents.<i>.document.*` | Output payload of `retrieval` spans |

**Duplicate tool spans.** With OpenInference and `@rs.tool` on the same function, each call produces two tool spans. OpenInference's LangChain tracer doesn't make its span current, so the SDK span is *not* its child. A third-party tool span counts as **shadowed** when an SDK tool span with the same tool name starts within its interval.
- Detection, regression-case fixtures and the trace's `tool_call_count` use only the SDK span, which knows the side effect and hashes the raw arguments.
- The waterfall still shows both spans.
- Model calls aren't deduplicated. Wrapping a model call in `rs.llm_call` while OpenInference also traces it counts the call (and its tokens) twice.

**Redaction.** The SDK's `rootsignal.sdk.redaction = pattern` resource attribute applies only to the SDK's own spans. Payloads of other dialects on the same resource are redacted by the server under `redact_on_ingest = if_not_sdk`.

## 4. Capture policy, redaction and limits

### 4.1 What is captured

| Setting (default) | Effect |
|---|---|
| `capture_payloads` (`true`) | Model, tool and retrieval I/O, and run output |
| `capture_user_input` (`false`) | Run input and user-role model messages |
| `redact_pii` (`true`) | Pattern redaction of every payload and exception text |
| `max_payload_bytes` (32,768) | Per-attribute UTF-8 byte cap; never splits a character; appends `…[truncated]` |

Known limitation: `capture_user_input=False` doesn't stop user text from reaching tool arguments or retrieval queries that the agent derived from it. Those are governed by `capture_payloads` and redaction. A regression case can't be built from a run whose input wasn't captured (increment 7 will mark it `input_not_captured`).

### 4.2 Redaction

Order: secrets (`sk-…`, `AKIA…`, GitHub and Slack tokens, `Bearer …`) → e-mail → digit patterns (Luhn-validated card numbers, US SSN, phone numbers with separators, IPv4). Replacements are `[REDACTED:<NAME>]`. Redaction runs **before** truncation, so a secret straddling the cut is still removed.

Implementation: secret and e-mail patterns are skipped when a cheap substring prefilter (`"sk-"`, `"@"`, …) is absent. Digit patterns run only inside candidate runs of digits and separators, every pattern matches the unmodified run, and the **union** of all matched spans is redacted. A randomized test checks 20,000 cases: windowed redaction never leaves a sample in place that a full-text scan removes. It is still best-effort pattern matching; names, addresses and free-form identifiers aren't detected.

### 4.3 `args_hash`

`sha256(tool_name ∖0 canonical_json(args))[:16]`, computed over the **raw** arguments so that two calls differing only in redacted values aren't mistaken for repeats. Low-entropy arguments (e.g. an account number) could be recovered by brute force from a plain hash, so setting `args_hash_key` switches to HMAC-SHA256. The key must be stable across runs, or replay matching breaks.

### 4.4 Measured SDK overhead

From `make bench` on an Apple M5, Python 3.14, realistic JSON tool arguments, discarding exporter (export runs on a background thread and isn't measured). These are numbers from one machine, not a benchmark claim.

| Payload | Redaction on (p50 / p95) | Redaction off (p50 / p95) |
|---|---|---|
| 2 KB | 96 / 105 µs | 25 / 31 µs |
| 30 KB | 1,157 / 1,191 µs | 141 / 150 µs |

The ADR-003 target (<1 ms p50 per span) holds at typical payload sizes and is missed slightly at the 30 KB cap. Follow-up: move redaction onto the export thread, which needs care because truncation would then come before redaction.

## 5. Canonical tables

Defined in migration `0001`. Common rules:
- Tenant columns `org_id`, `project_id` and, where relevant, `environment_id` are on every row. **Composite foreign keys** `(org_id, project_id) → projects` and `(project_id, environment_id) → environments` make the database itself reject a row whose org, project and environment don't belong together.
- Deleting a project cascades to all its environments, keys, traces, spans, payloads and jobs.

### 5.1 `traces`: PK `(project_id, trace_id)`

`trace_id` (32 hex), `root_span_id`, `service_name`, `agent_name`, `agent_version`, `prompt_version`, `conversation_id`, `status ∈ {unset, ok, error}`, `source ∈ {production, replay}`, `replay_run_id` (from the resource attribute `rootsignal.replay.run_id`), `replay_case_id` (reserved, not populated), `started_at`, `ended_at`, `duration_ms`, roll-ups (`span_count`, `error_count`, `llm_call_count`, `tool_call_count`, `input_tokens`, `output_tokens`, `cost_usd`), `completed_at` (set when the trace is judged complete; detection runs then).

### 5.2 `spans`: PK `(project_id, trace_id, span_id)`, FK → `traces`

| Column | Source (rootsignal dialect) |
|---|---|
| `span_id` (16 hex), `parent_span_id` | OTLP span |
| `kind ∈ {agent, llm, tool, retrieval, mcp, chain, other}` | `rootsignal.span.kind`; `tool` + `rootsignal.mcp.server` → `mcp` |
| `name`, `started_at`, `ended_at`, `duration_ms` | OTLP span |
| `status ∈ {unset, ok, error}`, `status_message`, `error_type` | OTLP status, `error.type` |
| `model`, `provider` | `gen_ai.response.model` ?? `gen_ai.request.model`; `gen_ai.provider.name` ?? `gen_ai.system` |
| `input_tokens`, `output_tokens`, `cost_usd` | `gen_ai.usage.*`, `rootsignal.cost.usd` or priced server-side |
| `tool_name`, `side_effect`, `args_hash`, `call_index`, `repeat_index` | §3.5 |
| `input_payload_hash`, `output_payload_hash` | SHA-256 of the payload body (§5.3). Input = run input / input messages / tool arguments / retrieval query; output = run output / output messages / tool result / documents |
| `attributes` (JSONB) | Remaining attributes, size-bounded |
| `events` (JSONB) | Span events (exceptions) |
| `dialect ∈ {rootsignal, gen_ai, openinference, openllmetry, unknown}` | Which adapter normalised the span |

Kind mapping for other dialects: spans that have only GenAI conventions map by `gen_ai.operation.name` (`chat`/`text_completion`/`generate_content` → `llm`, `execute_tool` → `tool`, `invoke_agent` → `agent`, `retrieval` → `retrieval`). OpenInference is in §3.7. OpenLLMetry spans are detected but have no adapter yet. Anything unrecognised → `other`, keeping its raw attributes.

### 5.3 `payloads`: PK `(project_id, sha256)`

`body` (text, capped server-side at the project's `max_payload_bytes`, default 64 KiB), `byte_size`, `truncated` (set by the server cap or by the SDK's `truncated_fields`), `redaction_state ∈ {none, sdk, server, unknown}` (`sdk` from the resource attribute; `server` when the project's `redact_on_ingest` policy redacted it), `content_type ∈ {text, json}`, `last_referenced_at` (for retention). Content-addressed and deduplicated **within a project only**, so no cross-tenant deduplication can leak whether another tenant stored the same content. Spans reference payloads without a foreign key, so retention can delete bodies independently; readers treat a missing body as expired.

### 5.4 Tenancy and access tables

`organizations`, `projects` (`retention_days` default 14, not yet enforced; quotas `max_spans_per_day`, `max_ingest_bytes_per_day`, `max_payload_bytes`; `redact_on_ingest ∈ {always, if_not_sdk, never}`), `environments`, `api_keys` (`prefix` shown to users, `key_hash` = SHA-256 of the full key, which is never stored; `scopes ⊆ {ingest, read, admin}`, non-empty; `revoked_at`).

### 5.5 `jobs`

Durable queue: `kind`, `payload` (JSONB), `status ∈ {queued, running, done, failed, dead}`, `priority`, `attempts`/`max_attempts`, `run_after`, `locked_at`/`locked_by`, `dedupe_key` (unique among `queued`/`running` jobs only). `org_id`/`project_id` are both set or both NULL (system jobs).

### 5.7 `findings` (migration `0003`)

One row per detector hit on a trace, unique on `(project_id, trace_id, detector, finding_key)`: `severity`, `title`, `summary`, `evidence_span_ids`, `facts` (JSONB), `detector_version`, plus `agent_name`, `agent_version`, `tool_name` and `error_type` for issue fingerprints (increment 4). It cascades with its trace. `traces.completion_reason ∈ {root_span, idle_timeout}`, and `projects.detector_config` holds per-project detector overrides. See [DETECTORS.md](DETECTORS.md).

Tool spans without `rootsignal.tool.args_hash` (non-Rootsignal SDKs) get one derived at ingest from the arguments payload, flagged by `rootsignal.server.args_hash_derived = true`.

### 5.8 Issues (migration `0004`)

`issues`, unique on `(project_id, environment_id, fingerprint)`: components, title, description, severity, status (`open|resolved|ignored`), regression fields, aggregates, representative trace IDs, resolution fields. `issue_occurrences` has one row per finding (unique `finding_id`, cascades with the finding): `issue_id`, `trace_id`, `agent_version`, `severity`, `weight`, `occurred_at`. `issue_events` holds the history. `projects.issue_grouping` holds per-detector fingerprint overrides. API keys gain the `triage` scope. Composite FKs keep occurrences, findings and issues in one tenant. See [ISSUES.md](ISSUES.md).

### 5.10 Regression cases (migration `0006`)

`regression_cases` holds identity and status: issue, source trace (no FK; a case outlives its trace), name, status (`draft`, `reproduced`, `not_reproduced`, `passing`, `failing`), current version. `regression_case_versions` holds immutable spec snapshots (`spec` JSONB, change note, author) keyed by `(case_id, version)`. See [REGRESSION_CASES.md](REGRESSION_CASES.md).

### 5.11 Replay runs (migration `0007`)

`replay_runs`: tenant columns, case and case version (FK `(project_id, case_id)` → `regression_cases`), `purpose`, `mode`, `matching`, `status`, `verdict`, `summary`, `requested_by`, `trace_id`, the child's `report` and the per-assertion `results` (JSONB), plus timestamps. Migration `0007` also adds `traces.replay_run_id`, the `replay` API-key scope and the `replayed` issue event. See [REPLAY.md](REPLAY.md).

### 5.9 Sessions and investigations (migration `0005`)

`dashboard_sessions`, `login_links` (token hashes only), `investigations`, `investigation_claims` (CHECKs: observed ⇒ rules, verified ⇒ replay), `projects.llm_monthly_token_budget`, `usage_daily.llm_*_tokens`. See [INVESTIGATIONS.md](INVESTIGATIONS.md).

### 5.6 `usage_daily` and `audit_events` (migration `0002`)

`usage_daily`, keyed by `(project_id, day)` in UTC: `requests`, `spans_received`, `spans_accepted`, `bytes_received` (wire bytes) and `rejected_requests`. Quota checks lock and update this row inside the ingest transaction. `audit_events` is append-only: `actor`, `action` (`tenant.ensure`, `api_key.create`, `api_key.revoke`), `target`, `details`.

`traces.unpriced_llm_call_count`: model calls with no price, so a `cost_usd` with this > 0 is a lower bound.

## 6. Versioning

Additive attribute changes don't bump anything. Renaming or removing a `rootsignal.*` key, or changing a column's meaning, requires a migration and a compatibility note here; ingest keeps reading the old key for at least one minor SDK version.
