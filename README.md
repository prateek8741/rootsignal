# Rootsignal

An AI agent reliability platform: **observe → detect → investigate → reproduce → fix → verify**. Working name; not trademark-checked.

> **Status: Phase 1, increment 7 of 10.** SDK → authenticated OTLP ingest → worker → 8 deterministic detectors → issues with regression detection → evidence-backed investigations → regression cases built from failing runs → React dashboard with cookie-session sign-in. Replay (running a case against your code) is next. See [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md).

## Layout

```
sdk/python/      pip package `rootsignal`: OpenTelemetry-based tracing SDK
server/          pip package `rootsignal_server`: ingest + read API, worker, detectors, admin CLI, migrations
dashboard/       React + TypeScript dashboard (Vite)
evaluation/      labelled datasets and the detector evaluation script
scripts/         dev setup (no containers), SDK overhead benchmark, storage/throughput measurement
docs/            competitive analysis, design, schema, roadmap
```

## Setup (macOS, no Docker)

Requirements: [uv](https://docs.astral.sh/uv/), PostgreSQL 16 (`brew install postgresql@16 && brew services start postgresql@16`) and Node 24 (dashboard).

```bash
make setup     # creates rootsignal_dev + rootsignal_test, .env, venv; migrates dev DB
make check     # ruff + mypy (strict) + Python tests + dashboard typecheck/tests
make bench     # SDK hot-path overhead
```

Server integration tests run against `ROOTSIGNAL_TEST_DATABASE_URL` and **drop its `public` schema**. They refuse to run unless the database name ends in `_test`.

## Run it

```bash
uv run rootsignal-server serve                     # http://127.0.0.1:8000, docs at /docs
uv run rootsignal-server worker                    # in another terminal: runs detectors
make seed-demo                                     # optional demo data; prints a one-time login link
uv run rootsignal-server dashboard-link --org <org> --project <project>   # sign-in link for the dashboard
make dashboard                                     # http://127.0.0.1:5173
uv run rootsignal-server create-tenant --org acme --project support
uv run rootsignal-server create-key --org acme --project support --name dev --scopes ingest,read
export ROOTSIGNAL_API_KEY=rs_...                    # printed once by create-key
# instrument and run your agent (below), then:
curl -H "Authorization: Bearer $ROOTSIGNAL_API_KEY" http://127.0.0.1:8000/v1/traces
```

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

[Competitive analysis](docs/COMPETITIVE_ANALYSIS.md) · [High-level design](docs/HIGH_LEVEL_DESIGN.md) · [Trace schema](docs/TRACE_SCHEMA.md) · [API](docs/API_SPECIFICATION.md) · [Detectors](docs/DETECTORS.md) · [Issues](docs/ISSUES.md) · [Dashboard](docs/DASHBOARD.md) · [Investigations](docs/INVESTIGATIONS.md) · [Regression cases](docs/REGRESSION_CASES.md) · [Threat model](docs/THREAT_MODEL.md) · [Cost model](docs/COST_MODEL.md) · [Roadmap](docs/IMPLEMENTATION_ROADMAP.md)

License: not yet chosen.
