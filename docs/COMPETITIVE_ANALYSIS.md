# Competitive Analysis

Status: Phase 0 draft · Research date: 2026-10-23 · Working product name: **Rootsignal** (not yet checked for trademark conflicts)

## 1. Method and evidence levels

Every claim below is tagged with how it was established. Pricing and features in this market change monthly; re-verify before quoting externally.

| Tag | Meaning |
|---|---|
| **[V-doc]** | Verified in the vendor's official documentation or pricing page on the research date |
| **[V-code]** | Verified by reading the vendor's public source code (repo + commit noted) |
| **[A]** | Publicly announced / marketing page / early access; behaviour not independently verified |
| **[3P]** | Only third-party sources (reviews, aggregators); treat as unreliable |
| **[U]** | Our assumption or inference; needs validation |

Primary sources:
- Raindrop docs index `https://raindrop.ai/docs/llms.txt` and 22 pages fetched as Markdown (signals, issues, triage-agent, experiments, self-healing-loop cookbook, workshop, MCP overview, PII redaction, data security, plans, change reports, self diagnostics, Python SDK, OpenTelemetry, …).
- Raindrop Workshop source: `github.com/raindrop-ai/workshop` at commit `a6b82d7` (2026-08-22 commit date; repo pushed 2026-10-22).
- Langfuse `langfuse.com/pricing.md`, LangSmith `langchain.com/pricing`, Braintrust `braintrust.dev/pricing`, Braintrust CI docs, Phoenix license page, GitHub license metadata for all repos named here.

We read no proprietary code and copied no UI, branding or marketing copy.

## 2. Raindrop.ai

Positioning **[V-doc]**: "the monitoring platform for AI agents… Similar to how Sentry tracks errors in your web apps, Raindrop discovers silent agent failures in production."

### 2.1 Capability inventory

| Area | What exists | Evidence |
|---|---|---|
| **Ingestion** | SDKs for TS, Python, Go, plus Rust/Swift/Java (beta), browser and HTTP API. OTLP/HTTP (JSON + protobuf, no gRPC; traces only, no metrics/logs) at `/v1/traces`, mapping OTel GenAI semconv (`gen_ai.system`, `gen_ai.usage.*`, `gen_ai.input.messages`…). ~30 framework integrations incl. LangGraph/LangChain, Google ADK, OpenAI Agents SDK, Claude Agent SDK, CrewAI, Pydantic AI, DSPy, MCP servers. Data model is **event**-centric (an "interaction" with input/output, user, convo) with traces attached. | [V-doc] |
| **Signals** | Named behaviours labelled on every event. Sources: presets (User Frustration, Task Failure, Forgetting, Refusals, Jailbreaking, NSFW, User Praise), LLM-drafted classifiers from a natural-language description with a human labelling loop, regex, SDK-instrumented (thumbs up/down), auto-derived tool errors, agent self-reports. New signals backfill 90 days (sampled). Triage can generate a "code signal" (sandboxed function). | [V-doc] |
| **Self Diagnostics** | SDK gives the agent a reporting tool; agent self-reports `missing_context`, `repeatedly_broken_tool`, `capability_gap`, `complete_task_failure`. TS-documented. | [V-doc] |
| **Issues** | "Issue Detection" = broader *distribution changes* (severity, status, trend, event/user counts, root-cause hypothesis). Separate **Stumbles** catalogue = one-off flagged conversations with tags (e.g. "Hallucination") and a Similar panel. Docs state neither catalogue alone proves nothing is wrong, and an issue can group several causes. Pro only. | [V-doc] |
| **Triage Agent** | Investigation agent in Slack, web and MCP; answers "grounded in real conversations"; scheduled Briefs, Daily Digest, signal alerts, creates signals from chat. Docs carry a disclaimer that AI output may be inaccurate. **No published description of how hypotheses are verified or how evidence is structured.** | [V-doc] (mechanism undocumented) |
| **Change Reports** | GitHub-connected brief: reads the agent harness (prompts/models/tools) in your repo, detects which agents a merged PR changed, and reports post-merge production behaviour (tool usage, negative signals, stumbles). Uses `commit_sha` on events. | [V-doc] |
| **Experiments** | Compare two cohorts of *already logged* traffic (date range, model, flag, property) on signal rates with 95% CI / p-value; median cost per event. Docs: "Experiments read existing traffic; they don't run or replay your agent." | [V-doc] |
| **MCP server** | Hosted at `mcp.raindrop.ai`, OAuth 2.1 or API key; ~60 tools incl. `get_trace`, `get_issue`, `get_issue_events`, `search_events`, `run_rql` (read-only query language), `query_cost`, `create_signal`, `create_experiment`, `create_dataset`, `ask_agent_question`. Write tools gated by scopes (`write:signals`, `write:issues`, …). | [V-doc] |
| **Self-healing loop** | Documented cookbook: alert → MCP evidence → import trace into Workshop → **your coding agent writes a test in your repo** that fails before / passes after → normal PR → verify with an Experiment on post-deploy traffic. The test lives in the user's repo; Raindrop doesn't store or run it. | [V-doc] |
| **Workshop** | Local OSS debugger with replay (see §3). | [V-doc][V-code] |
| **Simulations** | "Early access": CI-native agent simulation, runs scenarios against a PR and comments regressions + cost deltas; "your real code runs unchanged against stateful service copies"; "bring your own traces" incl. from Langfuse/Helicone. | **[A]** |
| **Datasets / Dashboards / RQL / Query API** | Shared datasets of events/traces; RQL dashboards; TS/Python Query SDK. | [V-doc] |
| **Cost tracking** | LLM spend by model/provider; unpriced calls reported as coverage gaps instead of being under-counted. | [V-doc] |
| **Privacy** | Default: stores all data sent. **PII Guard** server-side redaction at ingestion (pattern + AI), only redacted text stored, ≤128 KB per value, priced from $80/mo plus per-event tiers. SDK-side regex redaction **TypeScript only** today; does not cover `properties`. Edge PII redaction is Enterprise. | [V-doc] |
| **Security / compliance** | SOC 2 Type II; AWS encryption at rest; TLS; no training on customer data except customer-specific custom models. Subprocessors incl. OpenAI, Anthropic, Google, Supabase, Tinybird, ClickHouse, Modal, Vercel, AWS, Azure. Enterprise: SSO/SAML, audit logs, warehouse export. Roles + project isolation. No self-hosted offering found. | [V-doc] |

### 2.2 Pricing [V-doc, docs "Plans and limits"]

| Plan | Price | Includes |
|---|---|---|
| Hobby | $0 | 1,000 events/mo (hard stop), 1 custom signal, 15 Triage messages/mo; tracing, search, presets, MCP |
| Pro | $299/mo | metered events $0.003 each to 1M, then $0.002; 10 custom signals; 500 Triage messages; Issues, Stumbles, Experiments |
| Enterprise | custom | SSO/SAML, edge PII, audit logs, warehouse export, SLAs |

Third-party listings still show older Starter $65 / Pro $350 tiers **[3P]**; the docs say pre-launch customers keep legacy plans. Implied cost at 100k events/mo on Pro: roughly $299 + 100k × $0.003 = **~$599/mo**, if the platform fee and metering are additive (**[U]**: the docs don't say whether the fee includes any events).

### 2.3 Our reading of Raindrop's design

- **Strength:** it is product-behaviour-first (users, conversations, semantic signals, distribution shifts), with a very broad integration surface and a mature human-in-the-loop classifier workflow. It is ahead of us on breadth.
- **Verification today happens outside Raindrop:** in the cookbook the regression check lives in the customer's repo, written by a coding agent, and production verification is a statistical cohort comparison. Simulations (early access) suggests they are closing this gap.
- **Replay is local-only** (Workshop) and depends on a replay server per project (§3).
- **Unknowns [U]:** Triage's internal tool set, how it separates evidence from hypothesis, detector precision/recall, and Simulations' fidelity, pricing and availability.

## 3. Raindrop Workshop: evaluation as a foundation

| Item | Finding |
|---|---|
| License | **MIT**, © 2026 Invisible Tools, Inc. (dba Raindrop) [V-code]. Reuse is legally fine with notice preserved. |
| Stack | TypeScript on **Bun ≥1.3**, Express, ws, Drizzle ORM on **SQLite**, React 19 + Vite UI, `@modelcontextprotocol/sdk`, Vercel `ai` SDK, Raindrop's own SDK packages. ~1.37 MB of TypeScript. Distributed as the `raindrop` CLI binary, which also onboards to Raindrop Cloud. |
| Data model | `runs`, `spans` (payloads inline as text, model/provider/tokens, attributes JSON), `live_events`, `annotations`, `saved_events`, chat `sessions/messages`. Single-user; **no tenant, auth or retention model**. |
| Normalisation | `src/spans/normalize.ts` + adapters for Vercel AI SDK, Claude Agent SDK, Traceloop/OpenLLMetry, LiveKit → small typed `NormalizedSpan` (`llm` / `tool` / `other`). A good, deliberately minimal pattern. |
| MCP tools | `get_current_run`, `get_run_outline`, `query_traces`, `get_span_payload`, `get_span_context`, `search_run`, `annotate`, `replay_run`, `import_cloud_trace`, `show_in_ui`, `ask_agent`. Payload slicing (`payload-slice.ts`) keeps responses within the agent's context budget. |
| Replay | Picks the LLM span with the longest input, extracts messages/system prompt/model, and re-invokes a **user-scaffolded local HTTP replay server** registered in `.raindrop/agents.yaml`. Model and tool calls run live. The `setup-agent-replay` skill tells the coding agent to *prefer* dependency injection, test doubles or dry-run flags to avoid side effects, and explicitly says: "Do not introduce readonly mode, mocked tools, or mutating-tool classifications." So side-effect safety depends on how the coding agent wires the replay server, not on the platform. [V-code] |
| Local security | Loopback bind + Origin/Referer allowlist for mutating endpoints (`local-origin-guard.ts`). |

**Verdict: don't build on Workshop. Use it as a reference and stay interoperable with it.**
1. Stack mismatch: we standardise on Python/FastAPI/Postgres; adopting Bun/TS/SQLite would split the codebase.
2. It is a single-user local tool coupled to the Raindrop CLI and cloud onboarding. Multi-tenancy, auth and retention would be a rewrite anyway.
3. Its replay deliberately avoids the deterministic, fixture-based, side-effect-classified replay that our thesis B depends on.
4. What we take as *ideas* (no code copied): the small normalised span union, adapter-per-SDK dispatch, the run-outline → payload-slice MCP pattern for context economy, and the loopback origin guard.
5. Interop opportunity [U]: Workshop accepts OTLP JSON at `/v1/traces`. If our SDK emits standard OTLP + GenAI semconv, developers could mirror traces to Workshop and to us at once.

## 4. Open-source and commercial alternatives

### 4.1 Langfuse
- **License [V-code]:** MIT except `ee/`, `web/src/ee/`, `worker/src/ee/` (commercial). Copyright now ClickHouse, Inc. (acquired Jan 2026 **[3P]**). Enterprise self-host features (e.g. project RBAC, retention policies, audit logs) need a key **[V-doc via search summary]**.
- **Architecture:** TypeScript web + worker, Postgres + **ClickHouse** + Redis/Valkey + S3-compatible storage. Production guidance is Kubernetes/Helm; Compose is for low scale **[3P]**.
- **Features:** tracing (OTel-native), prompt management, datasets, experiments (with or without datasets), LLM-as-judge, human annotation, scores, CI via GitHub Action **[3P/changelog]**.
- **Cloud pricing [V-doc]:** Hobby free 50k units, 30-day access; Core $29 (100k units, 90 days); Pro $199 (3 years, retention management); Enterprise $2,499; overage $8/100k units.
- **Fit for us:** it would be an excellent tracing backend, but running ClickHouse + Redis + S3 natively without Docker is heavy for a laptop, the most valuable governance features are in `ee/`, and it has no failure → issue → replay workflow. **Decision: reference implementation + import/compat target (accept its OTel output), not a dependency.**

### 4.2 Arize Phoenix
- **License [V-code]: Elastic License 2.0.** Free to self-host with no feature gates, but ELv2 **forbids offering it as a hosted/managed service**. That rules it out as the core of a SaaS.
- **OpenInference [V-code]: Apache-2.0.** Python/JS auto-instrumentors for LangChain/LangGraph, OpenAI, Anthropic, LlamaIndex, DSPy, Google ADK and more. **Decision: reuse OpenInference instrumentors as an optional ingestion source**; our ingest normalises their attributes.

### 4.3 LangSmith (LangChain)
- **License:** platform proprietary; SDK MIT [V-code]. Self-hosting is Enterprise-only, price not public.
- **Pricing [V-doc]:** Developer $0/seat (5k base traces/mo, 1 seat); Plus $39/seat (10k base traces), then pay-as-you-go metered in LangChain Standard Units ($1/LSU). Per-trace overage rates in third-party sources conflict **[3P]**.
- **Features:** tracing, datasets, evaluators, annotation queues, online evals, "Insights" clustering **[3P]**. Strongest with LangChain/LangGraph.
- **Fit:** a competitor for LangGraph users, not a component.

### 4.4 Braintrust
- **License:** platform proprietary; SDKs Apache-2.0; `autoevals` MIT [V-code].
- **Pricing [V-doc]:** Starter $0 (1 GB processed data, 10k scores, 14-day retention); Pro $249/mo (5 GB, 50k scores, 30-day retention); overage $3–4/GB and $1.50–2.50 per 1k scores.
- **Features:** eval-first: experiments, datasets, scorers, playgrounds, Loop (AI agent that optimises prompts/scorers/datasets), Brainstore trace DB, **GitHub Action `braintrustdata/eval-action` that posts eval results on PRs** [V-doc CI page].
- **Fit:** the strongest incumbent for "evals in CI". `autoevals` (MIT) is a candidate evaluator library.

### 4.5 Reusable components (licenses verified on GitHub)

| Component | License | Planned use |
|---|---|---|
| OpenTelemetry Python API/SDK, OTLP proto, semantic-conventions | Apache-2.0 | SDK foundation, wire format, GenAI attribute names |
| OpenInference instrumentors | Apache-2.0 | Optional auto-instrumentation for LangGraph, ADK, OpenAI… |
| OpenLLMetry (Traceloop) | Apache-2.0 | Second accepted attribute dialect |
| MCP Python SDK | MIT | Our read-only investigation MCP server |
| autoevals | MIT | Candidate evaluator implementations (Phase 2) |
| Langfuse core | MIT (non-`ee`) | Reference only; no code copied |
| Workshop | MIT | Reference only; no code copied |
| Phoenix | ELv2 | **Do not incorporate** (SaaS restriction) |

## 5. Competitive matrix

✅ = verified, 🟡 = partial / announced / early access, ❌ = not found, ❓ = unknown. "Rootsignal (planned)" is a target, not a capability.

| Capability | Raindrop | Workshop | Langfuse | Phoenix | LangSmith | Braintrust | Rootsignal (planned) |
|---|---|---|---|---|---|---|---|
| OTLP / GenAI semconv ingest | ✅ | ✅ (JSON) | ✅ | ✅ | ✅ | 🟡 | Phase 1 |
| Self-host / local | ❌ | ✅ local | ✅ (heavy) | ✅ | Enterprise | Enterprise ❓ | Phase 1 (native Postgres) |
| Multi-tenant SaaS | ✅ | ❌ | ✅ | ❌ (license) | ✅ | ✅ | Phase 2–3 |
| Deterministic detectors (tool error, loops, retries, timeouts) | 🟡 tool errors as signals; loops via custom signals | ❌ | ❌ (DIY via evals) | ❌ | 🟡 | ❌ | **Phase 1** |
| Semantic signals / classifiers | ✅ mature | ❌ | 🟡 LLM-judge | 🟡 evals | 🟡 online evals | 🟡 online scorers | Phase 2 |
| Issue grouping with lifecycle | ✅ | ❌ | ❌ | ❌ | 🟡 Insights | 🟡 Topics ❓ | Phase 1 (fingerprint) |
| AI triage / investigation agent | ✅ | 🟡 via coding agent | ❌ | 🟡 assistant ❓ | 🟡 | 🟡 Loop (optimiser) | Phase 1 (structured evidence) |
| Explicit fact / hypothesis / verified separation | ❓ undocumented | ❌ | ❌ | ❌ | ❓ | ❓ | **Thesis A** |
| Investigation MCP server | ✅ | ✅ local | ✅ | ✅ | ✅ | ✅ ❓ | Phase 2 |
| Trace → stored regression case | 🟡 datasets; test in user repo | 🟡 coding agent writes eval | 🟡 trace → dataset item | 🟡 | 🟡 trace → dataset | 🟡 trace → dataset | **Thesis B** |
| Deterministic replay with recorded tool fixtures | ❌ | ❌ (live replay by design) | ❌ | ❌ | ❌ ❓ | ❌ ❓ | **Thesis B, Phase 1** |
| Side-effect classification / blocking in replay | 🟡 Simulations "service copies" [A] | ❌ | ❌ | ❌ | ❌ | ❌ | **Phase 1** |
| "Did the original failure stop?" verdict | 🟡 cohort experiment on prod traffic | ❌ | ❌ | ❌ | ❌ | 🟡 eval diff | **Thesis B** |
| Evals in CI on PRs | 🟡 Simulations [A] | ❌ | ✅ GH Action | 🟡 | ✅ | ✅ GH Action | Phase 2 |
| Pluggable / local LLM for platform AI | ❌ (fixed subprocessors) | 🟡 | 🟡 judge model configurable | ✅ | 🟡 | 🟡 BYO keys | **Thesis C** |
| SDK-side PII redaction (Python) | ❌ (TS only) | ❌ | 🟡 masking fn | ❓ | 🟡 | ❓ | Phase 1 (basic) |

## 6. Hypothesised gaps and how we test them

None of these are claimed advantages. Each needs a measurement before it appears in any external material.

| # | Hypothesis | Why it might hold | Why it might not | Validation experiment |
|---|---|---|---|---|
| H1 | Deterministic detectors catch a meaningful share of real agent failures cheaply and with fewer false positives than LLM classifiers | Loops, tool errors and retries are structural and visible in spans | Many costly failures are semantic (wrong answer, not an error); incumbents' tool-error signals already cover the easy cases | Labelled set of ≥200 traces (our agents + synthetic) → precision/recall per detector; compare against an LLM classifier on the same set; report cost per 1k traces |
| H2 (A) | Investigations that separate facts / hypotheses / missing evidence, each fact citing a span, are trusted more and wrong less often | LLM triage tends to sound confident | Users may not care about structure if the answer is right | Blind rating of N investigations (ours vs. free-form LLM summary) for correctness, citation validity (automated check that cited spans exist and contain the claim) and time-to-understanding |
| H3 (B) | Fixture-based replay reproduces a recorded failure deterministically more often than live replay | Recorded tool outputs remove environmental variance | Failures caused by model nondeterminism won't reproduce with recorded model outputs, and with live models they reproduce only probabilistically | Regression reproduction rate across the failure catalogue: fixtures-only vs. live-model + fixture tools vs. fully live, k=5 repeats each |
| H4 (B) | Default-deny side-effect policy makes replay safe enough to automate | Explicit `side_effect=` declarations + block on unknown | Developers won't annotate tools; inference may be wrong | % of tools annotated in our own agents; count of blocked vs. leaked side effects in a sandbox harness with canary tools |
| H5 (C) | Teams value self-hosting + local/BYO models for the platform's own AI | Data-residency and cost sensitivity | Raindrop/Braintrust convenience wins; the market may not pay for this | Discovery interviews (≥10 teams) + Phase 3 beta usage of local-model mode; measure investigation quality loss with a local model vs. a frontier model |
| H6 | Our total cost per 100k events is meaningfully below incumbents at small scale | Postgres-only, rules-first, LLM on demand | Ops cost and engineering time dominate at small scale | COST_MODEL measured from the Phase 3 beta, not projected |

## 7. Risks and honest takeaways

1. **Raindrop is moving toward our thesis B** (Simulations, Change Reports, self-healing cookbook). Our window is "open, self-hostable, deterministic, failure-specific". If that isn't measurably better, we should not pretend it is.
2. **Braintrust and Langfuse already own "datasets + evals in CI".** We should integrate with that (export regression cases as datasets) rather than compete head-on early.
3. **Breadth gap is large:** Raindrop lists ~30 integrations. We start with OTLP + OpenInference/OpenLLMetry dialects + our own SDK, and LangGraph/ADK via OpenInference.
4. **Deterministic replay has a hard limit:** it verifies plumbing and tool-handling fixes, not prompt/model fixes, unless it runs the live model. The roadmap must keep both modes and report which mode verified a fix.
5. Third-party pricing data is unreliable. Only the [V-doc] figures above should be reused.
