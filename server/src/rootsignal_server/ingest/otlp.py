"""Decode OTLP/HTTP trace export requests (protobuf or JSON, optionally gzip)."""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

PROTOBUF = "application/x-protobuf"
JSON = "application/json"


class OtlpDecodeError(ValueError):
    """The request body is not a valid OTLP trace export (HTTP 400, not retryable)."""


class PayloadTooLarge(ValueError):
    """The body exceeds a size limit (HTTP 413)."""


def media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    return base if base in (PROTOBUF, JSON) else None


def decompress(body: bytes, encoding: str | None, max_bytes: int) -> bytes:
    """Undo ``Content-Encoding``. Stops early on decompression bombs."""
    if not encoding or encoding.strip().lower() == "identity":
        return body
    if encoding.strip().lower() != "gzip":
        raise OtlpDecodeError(f"unsupported content-encoding {encoding!r}")
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(body, max_bytes + 1)
    except zlib.error as exc:
        raise OtlpDecodeError(f"invalid gzip body: {exc}") from exc
    if len(out) > max_bytes or decompressor.unconsumed_tail:
        raise PayloadTooLarge(f"decompressed body exceeds {max_bytes} bytes")
    return out


_ID_FIELDS = {"traceId": 16, "spanId": 8, "parentSpanId": 8}


def _hex_ids_to_base64(node: Any) -> None:
    """OTLP/JSON encodes trace and span IDs as hex; protobuf's JSON mapping wants base64."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _ID_FIELDS and isinstance(value, str):
                if value == "":
                    continue
                try:
                    raw = bytes.fromhex(value)
                except ValueError as exc:
                    raise OtlpDecodeError(f"{key} must be hex, got {value[:40]!r}") from exc
                if len(raw) != _ID_FIELDS[key]:
                    raise OtlpDecodeError(f"{key} must be {_ID_FIELDS[key]} bytes")
                node[key] = base64.b64encode(raw).decode("ascii")
            else:
                _hex_ids_to_base64(value)
    elif isinstance(node, list):
        for item in node:
            _hex_ids_to_base64(item)


def decode_request(body: bytes, media: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    if media == PROTOBUF:
        try:
            request.ParseFromString(body)
        except DecodeError as exc:
            raise OtlpDecodeError(f"invalid protobuf: {exc}") from exc
        return request
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OtlpDecodeError(f"invalid JSON: {exc}") from exc
    _hex_ids_to_base64(document)
    try:
        json_format.ParseDict(document, request, ignore_unknown_fields=True)
    except json_format.ParseError as exc:
        raise OtlpDecodeError(f"invalid OTLP JSON: {exc}") from exc
    return request


def encode_response(media: str, rejected_spans: int = 0, error_message: str = "") -> bytes:
    response = ExportTraceServiceResponse()
    if rejected_spans:
        response.partial_success.rejected_spans = rejected_spans
        response.partial_success.error_message = error_message
    if media == PROTOBUF:
        return response.SerializeToString()
    return json_format.MessageToJson(response, indent=None).encode("utf-8")
