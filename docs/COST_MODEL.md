# Cost Model

Status: v0 (increment 2). Assumptions are labelled; measured numbers come from `scripts/measure_ingest.py` and `scripts/bench_sdk_overhead.py` on one machine (Apple M5, 24 GB, Postgres 16 Homebrew, Python 3.14), 2026-10-23. **No cloud prices are quoted here.** They change, and Phase 3 replaces every estimate below with billed numbers.

## 1. Cost drivers

| Driver | Grows with | Control in place |
|---|---|---|
| Postgres storage | spans × bytes/span × retention days | Per-project quotas, payload cap, attribute bounds, payload dedup; **retention deletion not implemented yet** |
| Postgres CPU / IO for ingest | spans/s | Quotas, per-key rate limit, request size limits |
| Platform LLM calls (triage, semantic signals) | investigations, signal evaluations | Default provider `none`; per-project token budget (increment 6) |
| SDK overhead in customer apps | spans × payload size | Sampling, `capture_payloads`, `redact_pii` |
| Egress / network | wire bytes | gzip supported; quota on wire bytes |

## 2. Measured

### 2.1 Storage and ingest

Synthetic support-agent workload: 2,000 traces, 10,984 spans (5.5 spans/trace), two model calls with a long system prompt, two tool calls, sometimes a failing payments call and a ticket, realistic JSON tool output.

| Metric | Value |
|---|---|
| OTLP protobuf on the wire | **~1.0 KB / span** |
| Postgres total incl. indexes | **~1.76 KB / span** (spans 1,084 B · payloads 491 B · traces 186 B per span) |
| Payload bodies | 313 B / span after dedup (5,066 payload rows for 10,984 spans; repeated system prompts dedupe within a project) |
| Normalisation | ~51,600 spans/s, one core |
| Store (one transaction per ~220-span batch) | **~2,060 spans/s**, one process |

Caveats: synthetic data dedupes better than real traffic with unique documents, and real payloads may be much larger (RAG contexts up to the 64 KiB cap). Measure our own agents in increment 10 before trusting the bytes-per-span figure.

### 2.2 SDK overhead (hot path, per tool span)

| Payload | Redaction on | Redaction off |
|---|---|---|
| 2 KB | 96 µs p50 | 25 µs p50 |
| 30 KB | 1,157 µs p50 | 141 µs p50 |

## 3. Derived capacity per project (assumption-driven)

Using the measured 1.76 KB/span:

| Scenario | Spans/day | Storage/day | Steady state at 14-day retention |
|---|---|---|---|
| Default quota (`max_spans_per_day` = 100,000) | 100,000 | ~176 MB | ~2.5 GB, **once retention deletion exists** |
| Small beta agent (assumption: 2,000 runs/day × 6 spans) | 12,000 | ~21 MB | ~0.3 GB |

At the measured store rate, one ingest process absorbs the default daily quota in under a minute of work. Ingest CPU is not the constraint at beta scale; storage growth is.

## 4. Quota defaults and why

| Setting | Default | Reason |
|---|---|---|
| `max_spans_per_day` | 100,000 | ~176 MB/day measured; keeps one runaway agent from filling a small database |
| `max_ingest_bytes_per_day` | 1 GiB (wire) | ~1M spans at 1 KB; a backstop for unusually large payloads |
| `max_payload_bytes` | 64 KiB per payload | Room for RAG contexts; truncation is flagged |
| `retention_days` | 14 | Debugging horizon vs. storage. **Not enforced until the Phase 2 retention job** |
| Rate limit | 50 req/s per key, burst 100 | SDK batches every 1 s; 50 req/s is far above one SDK's needs |
| Platform LLM provider | `none` | Zero recurring cost; detection must work without an LLM |

## 5. Monitored agents' own LLM cost

- Ingest prices model calls from `gen_ai.usage.*` tokens using an **operator-supplied** price file (`ROOTSIGNAL_PRICING_FILE`). No vendor prices ship with the code.
- `ollama` is priced at $0 (no API charge). Electricity and hardware aren't modelled.
- A model call that can't be priced stores `cost_usd = NULL` and increments `unpriced_llm_call_count`, so the dashboard can report totals as lower bounds instead of silently understating.

## 6. Development cost today

Recurring: **$0**. Native Postgres, local Python, local Ollama model; no paid APIs, no cloud resources.

## 7. Open items

1. Retention and deletion job (Phase 2). Without it, storage grows without bound.
2. Store throughput (~2k spans/s): profile `store_batch` (multi-row insert compilation vs. the roll-up query) before any scale claim.
3. Per-project LLM token budget enforcement (increment 6).
4. Phase 3: replace §3 with billed infrastructure cost per 100k spans for the chosen host.
