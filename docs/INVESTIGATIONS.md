# Investigations

Status: v1, implemented in increment 6. Code: `server/src/rootsignal_server/investigation/`. Security analysis: [THREAT_MODEL.md §3.4](THREAT_MODEL.md).

An investigation answers "what do we know about this issue, and what should we check next?" in a form that can't pass off a guess as a fact.

## 1. Output: typed claims

| Kind | Meaning | Who may produce it | Must cite evidence |
|---|---|---|---|
| `observed` | A fact read off traces and statistics | Rules only | yes |
| `hypothesis` | A possible cause | Rules, LLM | yes |
| `alternative` | A competing explanation to rule out | Rules, LLM | yes |
| `missing_evidence` | What would tell hypotheses apart | Rules, LLM | no |
| `next_step` | What to do next | Rules, LLM | no |
| `verified` | A cause confirmed by a passing replay | Replay only (increment 8) | yes |

References point into the evidence bundle: `span` (trace + span ID), `trace`, `finding` (+ trace ID), or `stat` (a named statistic). A **validator** drops any claim whose references don't resolve, whose kind its source may not assert, or which duplicates another, and records each rejection with its reason. The same rules are also enforced by database CHECK constraints (`observed` ⇒ source `rules`; `verified` ⇒ source `replay`).

## 2. Pipeline

```mermaid
sequenceDiagram
  participant U as Dashboard / API (triage)
  participant DB as Postgres
  participant W as Worker
  participant M as Model (optional, local by default)
  U->>DB: POST /v1/issues/{id}/investigations → row (queued) + job
  W->>DB: investigate: collect evidence (tenant-scoped SQL)
  W->>W: rules → claims → validate
  W->>DB: claims + evidence snapshot; complete, or queue investigate_llm
  W->>DB: investigate_llm: read snapshot on a short separate connection; budget check
  W->>M: minimised evidence + existing claims (no transaction open)
  M-->>W: JSON claims (untrusted)
  W->>W: parse (strict schema) → validate
  W->>DB: accepted claims, rejections, token usage; complete + issue history event
```

### Evidence collected

Read-only and filtered by the investigation's project and environment:

- **Issue metadata.**
- **Representative traces:** the issue's first, latest and worst traces, with up to 60 spans each (kind, name, status, error type and a truncated message, duration, repeat index, side effect, whether the input was captured) and the findings that link them to the issue.
- **Window:** the issue's lifetime ± 1 h.
- **`version_breakdown`:** per agent version, how many traces in the window there were and how many hit the issue.
- **`tool_callers`:** for issues about a tool, how the same tool behaved for **every agent** calling it in the window (calls, errors, error types).

### Rules (deterministic, always run)

- **Observations:** occurrence count and time range; each finding in the representative traces; the distinct errors in the worst run; the version breakdown; per-agent tool outcomes.
- **Version contrast:** affected versions versus clean versions with ≥ 3 traces lead to "depends on the version", with "different traffic" as the alternative. If every version is affected, the cause is more likely outside the agent's recent changes.
- **Dependency comparison** (tool issues): if other agents' calls fail ≥ 50% of the time, that suggests an outage or degradation (alternative: shared bad input). If they fail ≤ 20%, the cause is likely specific to how this agent calls the tool (alternative: intermittent failure). With fewer than 3 calls from others, the rules report this as **missing evidence** rather than guessing.
- **Per detector:**
  - repeats and retries: the agent re-issues failed calls (or loops without progress; alternative: intentional polling);
  - step limit: no progress (alternative: the budget is too small);
  - unhandled exception: missing handling (alternative: inputs changed shape);
  - token budget: context growth (alternative: a few large inputs);
  - latency: the dominant span.
- **Missing evidence:** partial traces (root never arrived), uncaptured inputs of failing tool calls, truncated span lists, service-side logs for tool errors.
- **Next steps:** cap retries and add a failure path; check the dependency; turn the worst trace into a regression case.

### LLM step (optional)

- **Off by default.** It runs only if the request asks for it (`use_llm`), the server has a provider (`ROOTSIGNAL_LLM_PROVIDER=ollama|openai_compatible`), and the project has a monthly token budget (`projects.llm_monthly_token_budget`, default 0).
- **Local unless you opt in.** A non-loopback provider URL is refused unless `ROOTSIGNAL_LLM_ALLOW_EXTERNAL=true`.
- **What the model sees:** the issue, statistics, and span/finding metadata (already redacted by the ingest policy). **No payload bodies.** The evidence sits inside a per-request nonce-tagged block with every `<` escaped; the system prompt says the block is untrusted data.
- **What it may produce:** only `hypothesis`, `alternative`, `missing_evidence` and `next_step`, each validated like rule claims.
- **Failures don't lose work.** Model errors, invalid output, a disabled provider or an exhausted budget each set `llm_status` (`disabled`, `budget_exhausted`, `failed`); the deterministic result is kept, and the investigation still completes.
- **The model call happens with no database transaction open** (`test_llm_runs_with_no_transaction_open` checks `pg_stat_activity`).

## 3. API and UI

`POST /v1/issues/{id}/investigations {use_llm}` (scope `triage`, returns 202), `GET /v1/issues/{id}/investigations`, `GET /v1/investigations/{id}`. See [API_SPECIFICATION.md](API_SPECIFICATION.md). The issue page has an *Investigation* panel. It groups claims by kind, marks AI suggestions, links every span reference to the span in its trace (`/traces/{id}?span=…`), and lists discarded suggestions with reasons. Each completed investigation is recorded in the issue history.

## 4. Live check (2026-10-23, local `llama3.1:8b` via Ollama, demo data)

The "get_account times out in support-agent" issue (16 traces, v1 and v2) completed in **44 s**, using 4,464 input / 622 output tokens at $0. The rules produced 7 observations, 1 hypothesis ("every version affected → likely a dependency, data or configuration"), 2 missing-evidence items and 2 next steps. The model added 8 claims, all with valid references (0 rejected).

**Quality, honestly:**
- The model's additions were generic: three near-duplicate "the service is slow or down" hypotheses, plus sensible log and config checks. Useful as prompts for an engineer, not as insight.
- **Neither the rules nor the model found the actual cause** in the demo data: `get_account` fails only for `ent-` account IDs. Arguments aren't part of the evidence by design (privacy, minimisation), and only one error message happened to mention `ent-0042`.

**Next improvement:** a deterministic "failed vs succeeded calls" argument comparison over redacted argument payloads, with the same tenant scoping.

Investigation quality isn't measured yet. VALIDATION_PLAN.md (Phase 4) will rate correctness and evidence quality on labelled cases; until then, treat output as a structured starting point.

## 5. Limits

- One environment per investigation; no cross-environment comparison.
- Up to three representative traces are read; issues with diverse failure modes may need several investigations.
- No similarity search across issues, no repository/code access, no replay. Those come in increments 7–8 and Phase 2.
- The narrator isn't yet traced by our own SDK (increment 10b).
