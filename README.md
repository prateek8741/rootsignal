# Rootsignal

An AI agent reliability platform: **observe → detect → investigate → reproduce → fix → verify**. Working name; not trademark-checked.

> **Status: Phase 1, increment 1 of 10.** The Python SDK and the database schema exist and are tested. There is **no ingest API yet** (increment 2), so the SDK has no Rootsignal server to send to. Until then, pass your own OpenTelemetry exporter to `rs.init(exporter=...)`. See [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md).

## Layout

```
sdk/python/      pip package `rootsignal`: OpenTelemetry-based tracing SDK
server/          pip package `rootsignal_server`: models, Alembic migrations (API/worker to come)
scripts/         dev setup (no containers), SDK overhead benchmark
docs/            competitive analysis, design, schema, roadmap
```

## Setup (macOS, no Docker)

Requirements: [uv](https://docs.astral.sh/uv/) and PostgreSQL 16 (`brew install postgresql@16 && brew services start postgresql@16`).

```bash
make setup     # creates rootsignal_dev + rootsignal_test, .env, venv; migrates dev DB
make check     # ruff + mypy (strict) + all tests
make bench     # SDK hot-path overhead
```

Server integration tests run against `ROOTSIGNAL_TEST_DATABASE_URL` and **drop its `public` schema**. They refuse to run unless the database name ends in `_test`.

## SDK in 30 seconds

```python
import rootsignal as rs

rs.init(api_key="...", service_name="support-agent")   # or ROOTSIGNAL_* env vars

@rs.tool(side_effect="read")          # none | read | write | unknown: drives replay safety
def get_account(account_id: str) -> dict: ...

@rs.agent_run("support-agent", agent_version="v1", max_steps=8)
def handle(request: str) -> str:
    messages = [{"role": "user", "content": request}]
    with rs.llm_call("llama3.1:8b", provider="ollama", input_messages=messages) as call:
        reply = ...  # your model call
        call.set_response([{"role": "assistant", "content": reply}],
                          input_tokens=120, output_tokens=40)
    get_account("acc-1")
    return reply
```

Every scope also works as a context manager (`with rs.agent_run(...)`, `rs.tool_call(...)`, `rs.retrieval(...)`), sync or async. Defaults that matter:
- The end user's request is **not** recorded unless `capture_user_input=True`.
- Payloads are pattern-redacted (best effort) and capped at 32 KB per attribute.
- Exporter failures, backend outages and slow backends never raise into your code or block it.

Details: [docs/TRACE_SCHEMA.md](docs/TRACE_SCHEMA.md).

## Documents

[Competitive analysis](docs/COMPETITIVE_ANALYSIS.md) · [High-level design](docs/HIGH_LEVEL_DESIGN.md) · [Trace schema](docs/TRACE_SCHEMA.md) · [Roadmap](docs/IMPLEMENTATION_ROADMAP.md)

License: not yet chosen.
