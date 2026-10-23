"""Evaluate deterministic detectors on a labelled dataset.

Each case goes through the production path: OTLP/JSON decode → normalise →
detect. Scores are per (trace, detector): did the detector fire on the trace?

    uv run python evaluation/run_detector_eval.py [--dataset detectors_v0] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rootsignal_server.detection.detectors import DETECTOR_NAMES, run_detectors
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, normalize
from rootsignal_server.ingest.otlp import JSON, decode_request

ROOT = Path(__file__).resolve().parent
DISCLAIMER = (
    "Synthetic dataset written by the same people who wrote the detectors. These numbers "
    "check that each rule does what its label rubric says on constructed cases; they are "
    "NOT estimates of real-world precision or recall."
)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    def false_positive_rate(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None


@dataclass
class Report:
    dataset: str
    cases: int
    per_detector: dict[str, Counts]
    mismatches: list[dict[str, object]] = field(default_factory=list)


def fired_detectors(otlp_json: bytes) -> set[str]:
    batch = normalize(decode_request(otlp_json, JSON), IngestPolicy())
    by_trace: dict[str, list[SpanView]] = {}
    for span in batch.spans:
        by_trace.setdefault(span.trace_id, []).append(SpanView.from_normalized(span))
    fired: set[str] = set()
    for trace_id, spans in by_trace.items():
        fired |= {f.detector for f in run_detectors(TraceView.build(trace_id, spans))}
    return fired


def evaluate(dataset: str) -> Report:
    directory = ROOT / "datasets" / dataset
    labels = json.loads((directory / "labels.json").read_text())
    counts = {name: Counts() for name in DETECTOR_NAMES}
    report = Report(dataset, len(labels), counts)
    for case_id, label in sorted(labels.items()):
        expected = set(label["expected"])
        unknown = expected - set(DETECTOR_NAMES)
        if unknown:
            raise ValueError(f"{case_id}: unknown detectors in labels: {unknown}")
        fired = fired_detectors((directory / "cases" / f"{case_id}.json").read_bytes())
        for name in DETECTOR_NAMES:
            c = counts[name]
            if name in fired and name in expected:
                c.tp += 1
            elif name in fired:
                c.fp += 1
            elif name in expected:
                c.fn += 1
            else:
                c.tn += 1
        if fired != expected:
            report.mismatches.append({
                "case": case_id, "category": label["category"],
                "false_positives": sorted(fired - expected),
                "false_negatives": sorted(expected - fired), "note": label.get("note", "")})
    return report


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def print_report(report: Report) -> None:
    print(f"Dataset {report.dataset}: {report.cases} labelled traces\n")
    print(f"{'detector':24} {'TP':>3} {'FP':>3} {'FN':>3} {'TN':>3} "
          f"{'precision':>9} {'recall':>6} {'FPR':>5}")
    total = Counts()
    for name, c in report.per_detector.items():
        total.tp, total.fp, total.fn, total.tn = (total.tp + c.tp, total.fp + c.fp,
                                                  total.fn + c.fn, total.tn + c.tn)
        print(f"{name:24} {c.tp:3} {c.fp:3} {c.fn:3} {c.tn:3} {_fmt(c.precision()):>9} "
              f"{_fmt(c.recall()):>6} {_fmt(c.false_positive_rate()):>5}")
    print(f"{'(micro-average)':24} {total.tp:3} {total.fp:3} {total.fn:3} {total.tn:3} "
          f"{_fmt(total.precision()):>9} {_fmt(total.recall()):>6} "
          f"{_fmt(total.false_positive_rate()):>5}")
    if report.mismatches:
        print("\nMismatches:")
        for m in report.mismatches:
            print(f"  [{m['category']}] {m['case']}: FP={m['false_positives']} "
                  f"FN={m['false_negatives']} {m['note']}")
    print(f"\nNote: {DISCLAIMER}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="detectors_v0")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    args = parser.parse_args()
    report = evaluate(args.dataset)
    print_report(report)
    if args.json:
        payload = asdict(report)
        payload["disclaimer"] = DISCLAIMER
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
