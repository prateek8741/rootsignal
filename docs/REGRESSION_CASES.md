# Regression Cases

Status: v1, implemented in increment 7. Code: `server/src/rootsignal_server/regression/`. Replay, which executes cases, is increment 8.

A **regression case** turns one failing production run into a repeatable test. It holds everything needed to run the agent again on the same input with the same tool results and model outputs, plus **assertions** that fail on the original run and should pass once the bug is fixed.

## 1. Creating a case

From an issue: `POST /v1/issues/{id}/regression-cases` (scope `triage`), or **Create from worst trace** on the issue page. By default it uses the issue's *worst* occurrence; pass `trace_id` to choose another occurrence of the same issue. Other traces are refused (422).

## 2. What a case contains (spec, `schema_version` 1)

| Part | Content |
|---|---|
| `source` | Trace ID, issue ID, agent name and version, when it ran, how the trace completed |
| `entrypoint` | `package.module:function` to call on replay. Recorded automatically by the SDK when the agent runs through a module-level `@rs.agent_run` function (`rootsignal.run.entrypoint`); otherwise set by hand |
| `input` | `kwargs` (decorated agents: the call's keyword arguments), `value`, or `missing`. Captured only if the agent ran with `capture_user_input=True`; otherwise supply it when creating the case, or edit it later |
| `fixtures.tools` | Each tool call in order: tool, declared side effect, args hash, repeat index, parsed arguments, and either the recorded result or the recorded error (type and message) |
| `fixtures.llm` | Each model call: model, recorded output messages, tokens, failure |
| `fixtures.retrievals` | Query and returned documents |
| `assertions` | See §3 |
| `quality` | `replayable_recorded`, `warnings`, and assertions that were considered but not used, with reasons |

Every recorded value says whether it was **captured**, **truncated** (payload cap) or **redacted** (contains `[REDACTED:…]` placeholders, so replay sees the placeholder, not the original).

## 3. Assertions

Assertions are generated from the findings that link the source trace to the issue, and evaluated with the same detector code and project configuration that produced the issue.

| Detector | Assertion (role `reproduces`) | Needs |
|---|---|---|
| `repeated_tool_call` | `max_identical_tool_calls`: at most *threshold − 1*; **1 for `write` tools** (a write must never repeat) | recorded |
| `excessive_retries` | `detector_absent(excessive_retries, tool)` | recorded (tools), live model (model retries) |
| `step_limit_reached` | `max_llm_calls ≤ max_steps − 1` | recorded |
| `unhandled_exception` | `no_unhandled_exception` | recorded |
| `tool_error`, `timeout` | `detector_absent(…, tool)` | **live tools**: replaying a recorded error can't make the tool succeed |
| `token_budget_exceeded` | `max_total_tokens` / `detector_absent` | live model |
| `high_latency` | `max_duration_ms` / `detector_absent` | live (real timing) |

Two **guards** are always considered: `run_completed` and `no_unhandled_exception`. They catch a "fix" that breaks something else.

**Selection rule:**
- A `reproduces` assertion is kept only if it **fails on the source run**; otherwise it wouldn't detect the failure.
- A guard is kept only if it **passes on the source run**.
- Duplicates are removed.
- Everything dropped is listed with its reason.

`test_every_detector_yields_a_reproducing_assertion_on_the_dataset` checks this across all positive cases in `detectors_v0`: every detector yields at least one reproducing assertion, and every guard holds on its source.

## 4. Versions

Cases are versioned and immutable. `PATCH /v1/regression-cases/{id}` (name, `entrypoint`, `input`, `disabled_assertions`, `note`) writes version *n + 1*, re-assessing warnings and replayability. Every version stays readable (`?version=n`), which keeps CI files reproducible. `GET /v1/regression-cases/{id}/spec` returns the spec as a standalone JSON document (also **Download JSON** in the dashboard) to check into a repository.

## 5. Status

Cases start as `draft`. Replay (increment 8) will set:
- `reproduced` / `not_reproduced`: does replay against the unfixed code fail like production did? A case that can't reproduce the failure is never reported as a passing test.
- `passing` / `failing`: after a fix.

## 6. Limits

- **Recorded replay** can verify fixes to agent logic, such as retry handling, loop limits and error handling. Fixes to tools, prompts or models need live tools or a live model; assertions say which.
- **Redaction and truncation** in the source data reduce fidelity, and the case says so. Capturing more data (`capture_user_input`, a higher `max_payload_bytes`) is the project's privacy decision.
- **Fixture matching** across reordered calls (after a fix changes the call sequence) is defined in increment 8.
- **Agents in standalone scripts** (`__main__`) or nested functions can't be imported by replay. Package your agent's entrypoint as an importable function.
