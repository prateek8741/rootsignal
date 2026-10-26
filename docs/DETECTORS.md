# Deterministic Detectors

Status: v1 detectors, implemented in increment 3. Code: `server/src/rootsignal_server/detection/`. Evaluation: `evaluation/run_detector_eval.py`.

## 1. How detection runs

```mermaid
sequenceDiagram
  participant API as ingest API
  participant DB as Postgres
  participant W as worker
  API->>DB: spans stored; root span seen → trace completed (root_span)
  API->>DB: enqueue trace_completed (deduplicated)
  W->>DB: maintenance: requeue stale jobs; complete idle traces (idle_timeout) + enqueue
  W->>DB: claim jobs (FOR UPDATE SKIP LOCKED)
  W->>W: load trace → run enabled detectors (pure functions)
  W->>DB: upsert findings, delete stale ones, mark job done (one transaction)
```

- **No LLM is involved.** Detectors are pure functions over the trace's spans; payload bodies aren't read.
- **When a trace counts as complete.** Its root span has arrived (`root_span`), or no span has arrived for `trace_idle_timeout` (default 60 s) and the worker's sweep completes it (`idle_timeout`, e.g. when the root span was lost). Late spans re-enqueue detection.
- **Idempotent.** Re-running detection upserts findings by `(trace, detector, finding_key)`, so finding IDs stay stable, and deletes findings that no longer apply.
- **Failures.** A failing job retries with exponential backoff (5 s, 10 s, … up to 10 min) and is marked `dead` after `max_attempts` (default 5). A job whose worker died is requeued after the visibility timeout (default 5 min). A worker that lost its lock rolls its writes back.
- **Findings state observations only:** counts, span IDs, thresholds. Explaining *why* is the investigation step (increment 6).

## 2. Detectors

Severity is a default triage hint, not a measured impact.

| Detector | Fires when | Grouped by (`finding_key`) | Severity |
|---|---|---|---|
| `unhandled_exception` | The agent run span (or, without agent spans, a true root span) ended with an error and an exception event. Reported once at the outermost failing agent; evidence includes the deepest span that recorded the same exception type (the origin) | outermost agent span | high |
| `tool_error` | A tool/MCP span failed for a reason other than a timeout, via an exception or `record_error` | tool + error type | medium |
| `timeout` | A non-agent span failed and its error type matches `time[d_ ]?out\|deadline`, or its status message matches `\btimed? ?out\b\|\bdeadline exceeded\b` | kind + target | medium |
| `repeated_tool_call` | The same tool ran ≥ `min_calls` (3) times with identical arguments (`args_hash`) in one trace | tool + args hash | high if the tool is `write`, else medium |
| `excessive_retries` | ≥ `min_retries` (2) back-to-back attempts after a failure, for identical tool calls, or for consecutive calls to the same model | `tool:<name>:<hash>` or `llm:<model>` | low if the last attempt succeeded, else medium |
| `step_limit_reached` | An agent made ≥ its `max_steps` model calls (only its own, not those of nested agents) | agent span | high |
| `token_budget_exceeded` | Trace total > `max_tokens_per_trace` (100,000), or a single call > `max_tokens_per_call` (32,000) | `trace` / `calls` | medium |
| `high_latency` | Trace > `max_trace_ms` (60 s); tool span > `max_tool_ms` (30 s); model span > `max_llm_ms` (60 s). Static thresholds | `trace` or kind + target | low |

### Configuration

Per project, in `projects.detector_config` (JSON):

```json
{"repeated_tool_call": {"min_calls": 5}, "high_latency": {"enabled": false}}
```

Unknown detectors or parameters, wrong types and non-positive numbers are rejected. If a project's stored config is invalid, the worker logs an error and uses the **defaults**, so a typo never switches detection off silently. There's no API to edit this yet; it arrives with project settings in Phase 2.

### Spans from other SDKs

`repeated_tool_call` and `excessive_retries` need an argument fingerprint. Our SDK sends `rootsignal.tool.args_hash`. For other GenAI-convention SDKs, ingest derives one from the stored arguments payload (canonical JSON), marked `rootsignal.server.args_hash_derived`. It is stable within a trace, but if the arguments payload was redacted or truncated, distinct calls can collide.

## 3. Evaluation (dataset `detectors_v0`)

`make eval-detectors` decodes each labelled OTLP/JSON case, normalises it as production ingest does, and runs the detectors. Results on 2026-10-23 (35 traces, scored per trace × detector):

| Detector | TP | FP | FN | TN | Precision | Recall | FPR |
|---|---|---|---|---|---|---|---|
| unhandled_exception | 3 | 0 | 0 | 32 | 1.00 | 1.00 | 0.00 |
| tool_error | 8 | 0 | 0 | 27 | 1.00 | 1.00 | 0.00 |
| timeout | 5 | 0 | 0 | 30 | 1.00 | 1.00 | 0.00 |
| repeated_tool_call | 5 | **2** | 0 | 28 | 0.71 | 1.00 | 0.07 |
| excessive_retries | 3 | 0 | 0 | 32 | 1.00 | 1.00 | 0.00 |
| step_limit_reached | 2 | 0 | 0 | 33 | 1.00 | 1.00 | 0.00 |
| token_budget_exceeded | 2 | 0 | 0 | 33 | 1.00 | 1.00 | 0.00 |
| high_latency | 2 | 0 | 0 | 33 | 1.00 | 1.00 | 0.00 |

**What these numbers are not.** The dataset is synthetic and was written by the same people who wrote the detectors, so near-perfect scores are expected by construction. They show that each rule does what its rubric says, and that regressions get caught: `test_detector_eval_matches_known_results` pins the exact mismatches. **They are not estimates of real-world precision or recall.** That needs labelled traces from real agents: §3.1 is the first small set, and Phase 4 needs more.

### 3.1 Real traces (dataset `dogfood_v0`)

51 traces from the docs assistant (LangGraph + `llama3.1:8b`), recorded 2026-10-26: `uv run python evaluation/run_detector_eval.py --dataset dogfood_v0`.
- The detectors fired 3 times on 2 traces (`tool_error` ×2, `unhandled_exception` ×1).
- Against the labels: 3 true positives, 0 false positives, 0 false negatives. With only 3 positives that is no estimate of precision.
- The informative number: **18 of the 20 traces that failed did so in ways no detector covers** (wrong, uncited or invented answers; claimed actions that never happened).
- The labels were assigned by the assistant that wrote the detectors.

See [FIELD_REPORT_DOGFOOD.md](FIELD_REPORT_DOGFOOD.md).

What the evaluation caught during development:
1. `unhandled_exception` reported an orphaned tool span as the run when the root span was lost (fixed: only true roots stand in for the run).
2. Its evidence pointed at an intermediate agent instead of the span where the exception was raised (fixed: deepest origin).
3. An early `excessive_retries` counted every later call to a model after one failure as a retry (fixed: back-to-back attempts only).

## 4. Known limitations

- **Intentional repetition** (polling a job status, cheap cached reads) triggers `repeated_tool_call`. Both are in the dataset as expected false positives. Mitigations to evaluate: a per-tool allowlist, or ignoring calls whose results change.
- **Semantic failures are invisible here.** "Agent claims success after a failed refund" yields only `tool_error`; the false claim needs a semantic signal (Phase 2). On real traces this was the dominant failure (§3.1). One sub-case looks deterministic: an answer that claims a tool call when the trace has no such span (2 of 51 dogfood traces). It's a candidate detector.
- **Latency thresholds are static.** A baseline per agent and tool needs history (Phase 2).
- **Model-call retries are inferred** from consecutive calls to the same model. Without an error, a retry looks like a normal step.
- **Detector versions are all 1.** Bump a detector's version whenever its logic changes after release.
