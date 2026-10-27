# Field Report: Dogfooding with a Real Agent

Status: increment 10, run on 2026-10-26. Every number below was measured on this machine (Apple M5, 24 GB, Ollama 0.35.0). Raw data:
- [`evaluation/datasets/dogfood_v0/`](../evaluation/datasets/dogfood_v0/): traces and labels;
- [`evaluation/reports/dogfood_v0.json`](../evaluation/reports/dogfood_v0.json): detector scores;
- [`evaluation/reports/dogfood_v0_run_v1.json`](../evaluation/reports/dogfood_v0_run_v1.json): answers, timings and automatic scores.

## 1. Setup

We had no production agent, so we built one: the **docs assistant** (`examples/src/rootsignal_examples/docs_assistant/`).

| | |
|---|---|
| Framework | LangGraph 1.2.14: an agent node calling the model, a `ToolNode`, and `tools_condition` routing between them |
| Model | `llama3.1:8b` on local Ollama, temperature 0, seed 7, at most 6 model calls per question |
| Corpus | This repo's `README.md` and `docs/*.md`, split at headings (161 sections), searched with BM25 |
| Tools | `retrieve(query)`, side effect `none`; `read_file(path)`, `read`, limited to corpus files; `save_note(title, body)`, `write` |
| Instrumentation | OpenInference's LangChain instrumentor, exporting through `rs.tracer_provider()`, plus `@rs.agent_run` and `@rs.tool` |
| Workload | 51 fixed questions: 36 answerable, 8 unanswerable, 4 "save a note", 2 "read this file", 1 path-traversal attempt |
| Dependencies | Exact pins, each release at least 2 weeks old when chosen. Licenses checked, including transitive ones: MIT, Apache-2.0 or BSD, except orjson, which includes MPL-2.0 code (fine for unmodified use) |

API cost: zero (local model). Time: 401.5 s for 51 questions (median 6.6 s, p90 11.3 s, max 27.8 s).

## 2. What the agent did

I reviewed every answer against the docs (method and caveats in the [dataset README](../evaluation/datasets/dogfood_v0/README.md)).

| Review | Traces | Examples |
|---|---|---|
| Correct (including 7 clean declines of unanswerable questions) | 31 | |
| Partly right or vague | 9 | Correct fact plus unrelated material; one decline that attributed a competitor's "custom price" plan to Rootsignal (u01) |
| Wrong | 3 | Session limits swapped (q07); the repeated-call rule inverted (q09); the straggler rule described as the regression rule (q26) |
| Missed: said "not in the docs" for something that is | 4 | Retrieval didn't surface the section (q11, q30, q35, q36) |
| False or skipped action | 3 | n01 invented verdicts, saved a note whose body was a fake tool call, and claimed a `read_file` call it never made. n02 never saved and didn't say so. n04 called `retrieve` with `save_note`'s arguments, then wrote "save_note called" as text without calling it |
| Crashed | 1 | a01: `read_file` correctly refused `../.env`, but LangGraph's default `ToolNode` re-raised the `FileNotFoundError` and the whole run failed |

**Behaviour pattern:** 50 of 51 runs made exactly 2 model calls and 1 tool call. The model never chained tools (for example `retrieve` then `read_file`), and never retried a search that came back weak.

The crash is a framework-default bug a real user would hit. In LangGraph 1.2, `ToolNode`'s default error handler only catches invalid tool arguments; any other tool exception escapes the graph. v1 keeps the default. **v2** passes `handle_tool_errors=True`, and on the same question it declines cleanly.

## 3. What broke in Rootsignal, and what we changed

### Instrumentation

| Finding | Effect | Status |
|---|---|---|
| **Every tool call produced two tool spans**: OpenInference's and the SDK's | Double tool counts. Fixture lists would have included a duplicate with side effect `unknown`, which also triggered a false "calls write/unknown tools" warning on the case | **Fixed.** A third-party tool span is *shadowed* when an SDK tool span with the same name starts inside it. Detection, regression-case fixtures and `tool_call_count` use the SDK span only. On this dataset: 51 of 52 OpenInference tool spans shadowed ([TRACE_SCHEMA §3.7](TRACE_SCHEMA.md#37-spans-from-openinference-instrumentors)) |
| OpenInference's LangChain tracer doesn't make its spans current | The SDK's tool span is a child of the run root, not of OpenInference's tool span, so parent links can't be used to match the two | Worked around: the shadowing rule uses time containment. The waterfall still shows the SDK tool spans beside the LangGraph chain, not inside it |
| **The server skipped redaction for OpenInference payloads.** The resource said `rootsignal.sdk.redaction = pattern`, and the server trusted that for every span on the resource, but the SDK only redacts its own spans | Prompts, retrieved text and tool output from third-party spans were stored unredacted | **Fixed**: the SDK's claim now applies only to `rootsignal`-dialect spans. Test: an e-mail address in an OpenInference `input.value` is redacted |
| The one tool failure the SDK never saw | In n04 the arguments failed validation *before* the decorated function ran, so only OpenInference recorded the failing call | Kept by design: an unshadowed third-party tool span still counts. This is why third-party spans are worth ingesting at all |
| OpenInference labels any LangGraph node *named* "agent" as `AGENT` (101 spans) | Mapped to our `agent` kind, every step would look like a separate agent run | **Fixed**: `AGENT` maps to `chain`, and runs come from the SDK |
| ChatOllama spans report `llm.system = "openai"` | Wrong provider | **Fixed**: `llm.provider` (`ollama`) takes precedence |
| Chain spans are 304 of 559 spans (54%) | Storage and noise: most spans are LangGraph's internal steps | Open. A per-project option to drop or sample `chain` spans is a candidate |
| OpenInference puts the full Python traceback, including local file paths, into a failing span's status message | A home-directory path (with the username) is stored in the trace; pattern redaction doesn't cover file paths | Open, noted in the threat model |
| Our own recorder first wrote protobuf's JSON mapping, with base64 IDs; OTLP/JSON requires hex | The server rejected the fixtures, correctly | Fixed in the recorder |

### Normalisation

There was no OpenInference adapter before this increment: every such span was stored as kind `other`, with no model, tokens or payloads. The adapter is now built from these recordings. It maps:
- kinds;
- `llm.model_name` and `llm.provider`;
- token counts;
- the flattened `llm.*_messages.*` attributes, into a JSON message list used as the payload;
- `input.value` and `output.value`, into payloads;
- retrieval documents.

After the change, the 51 traces contain 101 model calls, with 75,738 input and 4,193 output tokens. Tests: `server/tests/unit/test_openinference.py`, plus a database roll-up test on a real recorded trace.

### Detection

Measured with `evaluation/run_detector_eval.py --dataset dogfood_v0` on the recorded traces, using the final code:

| | |
|---|---|
| Findings | 3 on 2 traces: `tool_error` on a01 and n04, `unhandled_exception` on a01 |
| Against the labels | 3 true positives, 0 false positives, 0 false negatives. That is only 3 positives, so this says almost nothing about precision or recall |
| Traces with a real failure | 20 of 51 (§2) |
| …that a detector flagged | 2 (a01, n04). In n04 it flagged the malformed call, not the false "saved" claim |
| Failing traces no current detector can see | **18**: the 9 partial, 3 wrong, 4 missed, n01 and n02 (n04 was flagged) |

The deterministic detectors find execution failures (exceptions, tool errors, loops, limits). On this agent those were rare. What actually went wrong was mostly about **content**: wrong, uncited or invented answers, and claimed actions that never happened. That's the main lesson of the run.

Candidates, with the evidence for each:
- **Claimed action not taken (deterministic):** the final answer says a tool was called, or names one (`save_note`, `read_file`, "saved"), with no span of that tool in the trace. That's n01 and n04 here, 2 of 51. It's the same failure class as the scripted support agent's false "Done!". Precision risk: answers that mention a tool while explaining something.
- **Requested write not performed:** the user asked to save, and no `write` tool ran (n02). Recognising the request needs an intent signal, which fits better as an evaluated signal (Phase 2) than a rule.
- **Missing citation:** this is app-specific (here "cite `[docs/...]`"), so it would be a project-configured pattern signal, not a built-in detector. 12 of the 36 answers to answerable questions cite no file.

Smaller notes:
- `tool_error` titles carry fully qualified exception names (`pydantic_core._pydantic_core.ValidationError`). Grouping isn't affected, only readability.
- The trace detail waterfall shows shadowed spans with no marker. The dashboard could dim them.

### Replay (`live_model` mode)

Case: a regression case from the a01 crash issue. Entrypoint: `rootsignal_examples.docs_assistant.agent:answer`, with the captured input. Assertions:
- `a1`, reproduces: no exception escapes the run;
- `a2`, guard: the run completes.

| Run | Verdict | Exit | Why |
|---|---|---|---|
| Recorded mode, v1 | `diverged` | 2 | ChatOllama calls the model over HTTP, not through `llm_call.run`, so recorded mode can't serve it. The network guard blocked `127.0.0.1:11434` and the replay failed closed, as designed |
| `live_model` mode with `--allow-host 127.0.0.1:11434`, v1 | `failing`: the case became `reproduced` | 0 | The model asked for `read_file('../.env')` again. The SDK served the **recorded** `FileNotFoundError` (rebuilt from builtins) inside LangGraph's tool thread, and the run crashed as in production |
| `live_model`, v2 (`DOCS_ASSISTANT_VERSION=v2`) | `passing`: the case became `passing` | 0 | The error went back to the model, which declined |

Repeated 3 more times each: v1 reproduced 4 of 4 times and v2 passed 4 of 4. The fixed seed and temperature 0 made this case stable. Ollama doesn't promise determinism, and a case where the model's choice varies would need several runs per verdict, which isn't built.

The run also turned up an operational problem: one case was built by an API process started before the case-builder fix, and came out with duplicate fixtures. A server restart is needed after upgrading, which matters only until there is a deploy process.

## 4. Changes made because of this run

- OpenInference adapter in the normaliser, and the `AGENT` → `chain` mapping.
- Shadowing of duplicate third-party tool spans in detection, case fixtures and trace roll-ups.
- Server-side redaction for third-party spans that share the SDK's resource.
- The docs assistant itself: v1 with the framework default, v2 with the fix. It has offline tests with a scripted chat model (`examples/tests/`), so CI needs no Ollama.
- `run_detector_eval.py`: per-dataset disclaimers.

## 5. Open items, in rough priority

1. A "claimed action not taken" detector (evidence: 2 of 51 traces, plus the scripted support agent).
2. A per-project option to drop or sample `chain` spans (54% of spans here).
3. File paths and tracebacks in third-party status messages.
4. Evaluated signals (Phase 2) for content failures: wrong or uncited answers, requested writes not performed. These were 18 of the 20 failures here.
5. Repeated runs per replay verdict for nondeterministic `live_model` cases.
6. Better retrieval in the docs assistant itself. Four misses were retrieval, not reasoning. This belongs to the agent, not to Rootsignal.
