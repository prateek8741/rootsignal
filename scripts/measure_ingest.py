"""Measure Postgres storage per span and single-process store throughput.

Uses the real SDK to produce spans and the real normalise + store path, on a
dedicated database (the name must end in ``_bench``; its schema is dropped).

    createdb rootsignal_bench
    uv run python scripts/measure_ingest.py --traces 2000 \
        --database-url postgresql+psycopg://localhost:5432/rootsignal_bench
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, make_url, text

import rootsignal as rs
from rootsignal_server import admin
from rootsignal_server.auth.keys import TenantContext
from rootsignal_server.ingest.normalize import IngestPolicy, normalize
from rootsignal_server.ingest.service import store_batch

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
TABLES = ("traces", "spans", "payloads")


class Timeout(Exception):
    pass


def make_agent(rng: random.Random):  # type: ignore[no-untyped-def]
    @rs.tool(side_effect="read")
    def get_orders(customer_id: str) -> list[dict[str, object]]:
        return [{"order_id": f"ORD-{rng.randint(10_000, 99_999)}", "total": rng.randint(5, 900),
                 "status": rng.choice(["shipped", "pending", "returned"])}
                for _ in range(rng.randint(3, 15))]

    @rs.tool(side_effect="read")
    def search_docs(query: str) -> list[dict[str, object]]:
        return [{"id": f"doc-{i}", "score": round(rng.random(), 3),
                 "content": "Refunds are processed within 5-7 business days. " * rng.randint(2, 8)}
                for i in range(4)]

    @rs.tool(side_effect="write")
    def create_ticket(summary: str) -> str:
        return f"T-{rng.randint(1000, 9999)}"

    def run() -> None:
        with rs.agent_run("support-agent", agent_version="v3", max_steps=6) as agent:
            for step in range(2):
                messages = [{"role": "system", "content": "You are a support agent. " * 20},
                            {"role": "user", "content": "Where is my refund?"}]
                with rs.llm_call("llama3.1:8b", provider="ollama",
                                 input_messages=messages) as call:
                    call.set_response([{"role": "assistant",
                                        "content": f"step {step}: checking orders " * 5}],
                                      input_tokens=rng.randint(300, 900),
                                      output_tokens=rng.randint(20, 120))
            get_orders(f"cus_{rng.randint(1, 500)}")
            search_docs("refund timeline")
            if rng.random() < 0.2:
                try:
                    with rs.tool_call("payments_api", arguments={"id": 1}, side_effect="read"):
                        raise Timeout("upstream timeout")
                except Timeout:
                    pass
            if rng.random() < 0.3:
                create_ticket("customer asked about refund")
            agent.set_output("Your refund is on its way.")

    return run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--traces", type=int, default=2000)
    parser.add_argument("--traces-per-batch", type=int, default=40)
    args = parser.parse_args()
    if not (make_url(args.database_url).database or "").endswith("_bench"):
        raise SystemExit("refusing: database name must end in _bench (its schema is dropped)")

    engine = create_engine(args.database_url, connect_args={"options": "-c timezone=UTC"})
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.attributes.update(url=args.database_url, configure_logger=False)
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        ids = admin.ensure_tenant(conn, org_slug="bench", project_slug="p",
                                  environment="bench", actor="bench")
    tenant = TenantContext(ids.org_id, ids.project_id, ids.environment_id, ids.org_id,
                           frozenset({"ingest"}))

    rng = random.Random(42)
    agent = make_agent(rng)
    exporter = InMemorySpanExporter()
    rs.init(rs.Config(max_queue_size=65_536, max_export_batch_size=4096), exporter=exporter)
    for _ in range(args.traces):
        agent()
    rs.flush(30)
    spans = exporter.get_finished_spans()
    rs.shutdown()

    by_trace: dict[int, list] = {}  # type: ignore[type-arg]
    for span in spans:
        by_trace.setdefault(span.context.trace_id, []).append(span)
    traces = list(by_trace.values())
    batches = [[span for trace in traces[i:i + args.traces_per_batch] for span in trace]
               for i in range(0, len(traces), args.traces_per_batch)]

    wire_bytes = 0
    normalize_s = store_s = 0.0
    policy = IngestPolicy()
    for batch_spans in batches:
        request = encode_spans(batch_spans)
        wire_bytes += len(request.SerializeToString())
        t0 = time.perf_counter()
        batch = normalize(request, policy)
        t1 = time.perf_counter()
        with engine.begin() as conn:
            store_batch(conn, tenant, batch)
        store_s += time.perf_counter() - t1
        normalize_s += t1 - t0

    with engine.begin() as conn:
        conn.execute(text("ANALYZE"))
        sizes = {t: conn.execute(text("SELECT pg_total_relation_size(:t)"), {"t": t}).scalar_one()
                 for t in TABLES}
        n_spans = conn.execute(text("SELECT count(*) FROM spans")).scalar_one()
        n_payloads = conn.execute(text("SELECT count(*) FROM payloads")).scalar_one()
        payload_body = conn.execute(text("SELECT sum(byte_size) FROM payloads")).scalar_one()

    total = sum(sizes.values())
    print(json.dumps({
        "traces": len(traces), "spans": n_spans, "payload_rows": n_payloads,
        "spans_per_trace": round(n_spans / len(traces), 2),
        "otlp_wire_bytes_per_span": round(wire_bytes / n_spans),
        "db_bytes_per_span_total": round(total / n_spans),
        "db_bytes_by_table_per_span": {t: round(b / n_spans) for t, b in sizes.items()},
        "payload_body_bytes_per_span": round(payload_body / n_spans),
        "normalize_spans_per_s": round(n_spans / normalize_s),
        "store_spans_per_s": round(n_spans / store_s),
        "batches": len(batches),
    }, indent=2))
    engine.dispose()


if __name__ == "__main__":
    main()
