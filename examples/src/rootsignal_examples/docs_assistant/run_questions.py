"""Run the fixed question set through the docs assistant and score the answers.

    ROOTSIGNAL_API_KEY=... uv run python -m rootsignal_examples.docs_assistant.run_questions \\
        [--ids q01,u04] [--record evaluation/datasets/dogfood_v0/cases] [--out results.json]

Every run is exported to Rootsignal (traces, detection, issues). With ``--record``
each trace is also written as an OTLP/JSON request, so it can be replayed into
the normaliser and detectors offline.

Scores are crude, automatic and labelled as such: whether the answer cites an
expected file, contains one of the expected keywords, declines unanswerable
questions, and uses ``save_note`` only when asked. They show trends between
runs; they are not a judgment of answer quality.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToJson
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import rootsignal as rs
from rootsignal_examples.docs_assistant import agent

QUESTIONS = Path(__file__).with_name("questions.json")
DECLINE = re.compile(r"couldn.t find|could not find|not (?:in|covered by) the doc|"
                     r"doesn.t (?:say|mention|contain)|no information|don.t know", re.I)
CITATION = re.compile(r"\b((?:docs/)?[A-Z_]+\.md)\b")


_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def otlp_json(spans: Sequence[ReadableSpan]) -> str:
    """OTLP/JSON for ``spans``. Unlike protobuf's JSON mapping, OTLP/JSON wants hex IDs."""
    def hexify(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: base64.b64decode(v).hex() if k in _ID_FIELDS and isinstance(v, str)
                    else hexify(v) for k, v in node.items()}
        if isinstance(node, list):
            return [hexify(v) for v in node]
        return node

    return json.dumps(hexify(json.loads(MessageToJson(encode_spans(spans)))),
                      separators=(",", ":")) + "\n"


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


class Tee(SpanExporter):
    """Send spans to Rootsignal and keep a copy for scoring and recording."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return self.inner.export(spans)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)


def score(question: dict[str, Any], answer: str | None, error: str | None,
          tools: Counter[str]) -> dict[str, Any]:
    text = (answer or "").lower()
    cited = sorted({m if m.startswith("docs/") or m == "README.md" else f"docs/{m}"
                    for m in CITATION.findall(answer or "")})
    expected = question["expected_sources"]
    declined = bool(DECLINE.search(answer or ""))
    out: dict[str, Any] = {
        "error": error,
        "cited": cited,
        "cites_expected": bool(set(cited) & set(expected)) if expected else None,
        "keyword": any(k in text for k in question["mention_any"])
        if question["mention_any"] else None,
        "declined": declined,
        "saved_note": tools["save_note"] > 0,
    }
    category = question["category"]
    if category == "answerable" or category == "read_file":
        out["ok"] = error is None and not declined and bool(out["keyword"])
    elif category in ("unanswerable", "refuse"):
        out["ok"] = error is None and declined and not out["saved_note"]
    else:  # save_note
        out["ok"] = error is None and out["saved_note"]
    if category != "save_note" and out["saved_note"]:
        out["ok"] = False  # wrote without being asked
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", help="comma-separated question IDs (default: all)")
    parser.add_argument("--record", type=Path, help="write each trace as OTLP/JSON here")
    parser.add_argument("--out", type=Path, help="write per-question results (JSON) here")
    args = parser.parse_args()

    questions = json.loads(QUESTIONS.read_text())["questions"]
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    config = rs.Config.from_env(service_name="docs-assistant", capture_user_input=True,
                                max_queue_size=16_384)
    tee = Tee(OTLPSpanExporter(endpoint=config.traces_url, timeout=config.export_timeout_s,
                               headers={"Authorization": f"Bearer {config.api_key}"}))
    rs.init(config, exporter=tee)

    rows: list[dict[str, Any]] = []
    for question in questions:
        started = time.monotonic()
        answer, error = None, None
        try:
            answer = agent.answer(question["question"])
        except Exception as exc:  # the agent's failures are data here
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        rows.append({"id": question["id"], "category": question["category"],
                     "question": question["question"], "answer": answer, "error": error,
                     "seconds": round(time.monotonic() - started, 2)})
        print(f"{question['id']} {rows[-1]['seconds']:6.1f}s {'ERROR ' + error if error else ''}"
              f"{(answer or '')[:110]!r}", flush=True)
    rs.flush(30)

    # Match runs to traces: questions run one at a time, so root spans are in order.
    by_trace: dict[str, list[ReadableSpan]] = defaultdict(list)
    for span in tee.spans:
        by_trace[format(span.context.trace_id, "032x")].append(span)
    roots = sorted((s for s in tee.spans if s.parent is None and s.name.startswith("invoke_agent")),
                   key=lambda s: s.start_time or 0)
    if len(roots) != len(rows):
        print(f"warning: {len(roots)} root spans for {len(rows)} questions", file=sys.stderr)
    for row, root, question in zip(rows, roots, questions, strict=False):
        trace_id = format(root.context.trace_id, "032x")
        spans = by_trace[trace_id]
        tools = Counter(str(s.attributes.get("gen_ai.tool.name")) for s in spans
                        if s.attributes and s.attributes.get("rootsignal.span.kind") == "tool")
        llm = [s for s in spans if s.attributes
               and s.attributes.get("openinference.span.kind") == "LLM"]
        row.update(trace_id=trace_id, tool_calls=dict(tools), model_calls=len(llm),
                   tokens=sum(_int(s.attributes.get("llm.token_count.total"))
                              for s in llm if s.attributes),
                   **score(question, row["answer"], row["error"], tools))
        if args.record:
            args.record.mkdir(parents=True, exist_ok=True)
            (args.record / f"{row['id']}.json").write_text(otlp_json(spans))
    rs.shutdown()

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
    print("\ncategory      n  ok  errors  cites_expected  saved_note")
    for category, items in sorted(by_category.items()):
        cites = [r["cites_expected"] for r in items if r.get("cites_expected") is not None]
        ok = sum(bool(r.get("ok")) for r in items)
        errors = sum(bool(r["error"]) for r in items)
        saved = sum(bool(r.get("saved_note")) for r in items)
        print(f"{category:12} {len(items):2} {ok:3} {errors:6}  "
              f"{sum(cites):3}/{len(cites):<3}         {saved}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"model": agent.MODEL, "agent_version": agent.VERSION,
                                        "results": rows}, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
