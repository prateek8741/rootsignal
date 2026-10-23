"""Measure SDK hot-path overhead per instrumented tool call.

Exports go to a discarding exporter so the number isolates SDK work
(span creation, serialisation, redaction) on realistic JSON payloads; real OTLP export runs on a
background thread and is not on the measured path.

    uv run python scripts/bench_sdk_overhead.py [--calls 20000] [--payload-bytes 2000]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections.abc import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import rootsignal as rs


class Discard(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS


def realistic_payload(size: int) -> str:
    """JSON-ish tool output: ids, dates, amounts, versions and an occasional e-mail."""
    rng = random.Random(0)
    records: list[str] = []
    while sum(len(r) for r in records) < size:
        records.append(json.dumps({
            "order_id": f"ORD-{rng.randint(10_000, 99_999)}",
            "created_at": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T10:00:00Z",
            "amount": round(rng.uniform(1, 999), 2),
            "items": [rng.randint(1, 500) for _ in range(3)],
            "note": "contact ops@example.com" if rng.random() < 0.1 else "ok",
            "sdk": "v1.12.3",
        }))
    return ("[" + ", ".join(records) + "]")[:size]


def percentile(values: list[float], q: float) -> float:
    return statistics.quantiles(values, n=100)[int(q) - 1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=20_000)
    parser.add_argument("--payload-bytes", type=int, default=2_000)
    args = parser.parse_args()
    text = realistic_payload(args.payload_bytes)

    def plain(q: str) -> str:
        return q[:10]

    instrumented = rs.tool(side_effect="read")(plain)

    def measure(fn: object) -> list[float]:
        samples: list[float] = []
        with rs.agent_run("bench"):
            for _ in range(args.calls):
                start = time.perf_counter_ns()
                fn(text)  # type: ignore[operator]
                samples.append((time.perf_counter_ns() - start) / 1000)
        return samples

    rs.init(rs.Config(max_queue_size=8192, max_export_batch_size=512), exporter=Discard())
    measure(instrumented)  # warm-up
    base = measure(plain)
    redacted = measure(instrumented)
    rs.init(rs.Config(redact_pii=False, max_queue_size=8192, max_export_batch_size=512),
            exporter=Discard())
    unredacted = measure(instrumented)
    rs.shutdown()

    print(f"calls={args.calls} payload_bytes={args.payload_bytes}")
    for label, samples in (("uninstrumented", base), ("tool span, redaction on", redacted),
                           ("tool span, redaction off", unredacted)):
        print(f"{label:28s} p50={percentile(samples, 50):8.1f}µs "
              f"p95={percentile(samples, 95):8.1f}µs")


if __name__ == "__main__":
    main()
