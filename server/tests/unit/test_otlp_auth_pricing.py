from __future__ import annotations

import gzip
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from otlp_factory import raw_request
from rootsignal_server.auth.keys import generate_key, hash_key, parse_bearer
from rootsignal_server.auth.rate_limit import RateLimiter
from rootsignal_server.ingest.otlp import (
    JSON,
    PROTOBUF,
    OtlpDecodeError,
    PayloadTooLarge,
    decode_request,
    decompress,
    encode_response,
    media_type,
)
from rootsignal_server.ingest.pricing import PriceTable


def otlp_json(trace_id: str = "0af7651916cd43dd8448eb211c80319c",
              span_id: str = "b7ad6b7169203331") -> bytes:
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{
        "traceId": trace_id, "spanId": span_id, "name": "x",
        "startTimeUnixNano": "1700000000000000000", "endTimeUnixNano": "1700000000100000000",
        "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}],
    }]}]}]}).encode()


def test_media_type_parsing() -> None:
    assert media_type("application/x-protobuf") == PROTOBUF
    assert media_type("application/json; charset=utf-8") == JSON
    assert media_type("text/plain") is None
    assert media_type(None) is None


def test_protobuf_round_trip() -> None:
    request = raw_request([{"span_id": b"\x01" * 8}])
    decoded = decode_request(request.SerializeToString(), PROTOBUF)
    assert decoded == request


def test_json_ids_are_hex_per_otlp_spec() -> None:
    decoded = decode_request(otlp_json(), JSON)
    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id.hex() == "0af7651916cd43dd8448eb211c80319c"
    assert span.span_id.hex() == "b7ad6b7169203331"
    assert span.start_time_unix_nano == 1_700_000_000_000_000_000


@pytest.mark.parametrize("body", [
    otlp_json(trace_id="not-hex"),
    otlp_json(span_id="abcd"),  # wrong length
    b"{not json",
    b'{"resourceSpans": "nope"}',
])
def test_invalid_json_is_rejected(body: bytes) -> None:
    with pytest.raises(OtlpDecodeError):
        decode_request(body, JSON)


def test_garbage_protobuf_is_rejected() -> None:
    with pytest.raises(OtlpDecodeError):
        decode_request(b"\xff\xff\xff\xff", PROTOBUF)


def test_gzip_and_bomb_protection() -> None:
    assert decompress(gzip.compress(b"hello"), "gzip", 100) == b"hello"
    assert decompress(b"plain", None, 100) == b"plain"
    bomb = gzip.compress(b"\x00" * 10_000_000)
    assert len(bomb) < 20_000
    with pytest.raises(PayloadTooLarge):
        decompress(bomb, "gzip", 1_000_000)
    with pytest.raises(OtlpDecodeError):
        decompress(b"not gzip", "gzip", 100)
    with pytest.raises(OtlpDecodeError):
        decompress(b"x", "br", 100)


def test_partial_success_response_encoding() -> None:
    proto = ExportTraceServiceResponse.FromString(encode_response(PROTOBUF, 2, "bad ids"))
    assert proto.partial_success.rejected_spans == 2
    as_json = json_format.Parse(encode_response(JSON, 1, "x"), ExportTraceServiceResponse())
    assert as_json.partial_success.error_message == "x"
    assert encode_response(PROTOBUF) == b""  # full success: empty message
    assert isinstance(ExportTraceServiceRequest(), ExportTraceServiceRequest)


def test_api_keys() -> None:
    key = generate_key()
    assert key.plaintext.startswith("rs_") and len(key.plaintext) == 46
    assert key.prefix == key.plaintext[:11]
    assert key.key_hash == hash_key(key.plaintext) and key.plaintext not in key.key_hash
    assert generate_key().plaintext != key.plaintext
    assert parse_bearer(f"Bearer {key.plaintext}") == key.plaintext
    assert parse_bearer(f"bearer {key.plaintext}") == key.plaintext
    for bad in (None, "", key.plaintext, f"Basic {key.plaintext}", "Bearer sk-123",
                "Bearer rs_" + "a" * 200):
        assert parse_bearer(bad) is None


def test_rate_limiter_refills_over_time() -> None:
    now = [0.0]
    limiter = RateLimiter(2.0, 2, clock=lambda: now[0])
    key = uuid.uuid4()
    assert limiter.try_acquire(key) == 0
    assert limiter.try_acquire(key) == 0
    wait = limiter.try_acquire(key)
    assert wait == pytest.approx(0.5)
    now[0] += 0.5
    assert limiter.try_acquire(key) == 0
    assert limiter.try_acquire(uuid.uuid4()) == 0  # independent buckets per key


def test_price_table_from_file(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": "test", "models": [
        {"provider": "Acme", "model": "m", "input_per_mtok": 1.5, "output_per_mtok": 3}],
        "zero_cost_providers": ["ollama", "llamacpp"]}))
    table = PriceTable.load(path)
    assert table.version == "test"
    assert table.cost("acme", "m", 2_000_000, 1_000_000) == Decimal("6.000000")
    assert table.cost("llamacpp", None, None, None) == Decimal(0)
    assert table.cost("acme", "m", None, 10) is None
    assert table.cost(None, "m", 1, 1) is None
    assert PriceTable().cost("openai", "gpt", 1, 1) is None  # no built-in vendor prices
