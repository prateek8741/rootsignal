"""Build OTLP export requests for tests, preferably from real SDK output."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import rootsignal as rs

TRACE_ID = bytes.fromhex("0af7651916cd43dd8448eb211c80319c")


def record_sdk_spans(build: Callable[[], Any], **config: Any) -> Sequence[ReadableSpan]:
    """Run ``build`` under the real SDK and return the finished spans."""
    exporter = InMemorySpanExporter()
    rs.init(rs.Config(**config), exporter=exporter)
    try:
        build()
        assert rs.flush(5)
        return exporter.get_finished_spans()
    finally:
        rs.shutdown()


def sdk_request(build: Callable[[], Any], **config: Any) -> ExportTraceServiceRequest:
    return encode_spans(record_sdk_spans(build, **config))


def request_from(spans: Sequence[ReadableSpan]) -> ExportTraceServiceRequest:
    return encode_spans(spans)


def kv(key: str, value: Any) -> KeyValue:
    any_value = AnyValue()
    if isinstance(value, bool):
        any_value.bool_value = value
    elif isinstance(value, int):
        any_value.int_value = value
    elif isinstance(value, float):
        any_value.double_value = value
    else:
        any_value.string_value = str(value)
    return KeyValue(key=key, value=any_value)


def raw_request(spans: list[dict[str, Any]],
                resource: dict[str, Any] | None = None) -> ExportTraceServiceRequest:
    """Hand-built request for shapes the SDK never produces (other dialects, bad IDs)."""
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for key, value in (resource or {"service.name": "raw"}).items():
        resource_spans.resource.attributes.append(kv(key, value))
    scope = resource_spans.scope_spans.add()
    for spec in spans:
        span = scope.spans.add()
        span.trace_id = spec.get("trace_id", TRACE_ID)
        span.span_id = spec["span_id"]
        if spec.get("parent_span_id"):
            span.parent_span_id = spec["parent_span_id"]
        span.name = spec.get("name", "span")
        span.start_time_unix_nano = spec.get("start", 1_700_000_000_000_000_000)
        span.end_time_unix_nano = spec.get("end", 1_700_000_000_500_000_000)
        for key, value in spec.get("attributes", {}).items():
            span.attributes.append(kv(key, value))
        if "status" in spec:
            span.status.code = spec["status"]
    return request
