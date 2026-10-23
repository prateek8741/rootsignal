"""The SDK must never break or noticeably slow the host app when export fails."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import rootsignal as rs


@rs.tool(side_effect="none")
def work(x: int) -> int:
    return x + 1


def run_agent() -> int:
    with rs.agent_run("agent"):
        return work(1)


class _Handler(BaseHTTPRequestHandler):
    behaviour = "ok"  # overwritten per server
    delay_s = 0.0
    received: list[tuple[dict[str, str], bytes]]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
        time.sleep(self.delay_s)
        status = 200 if self.behaviour == "ok" else 500
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def backend() -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def start(behaviour: str = "ok", delay_s: float = 0.0) -> tuple[str, list[Any]]:
        received: list[tuple[dict[str, str], bytes]] = []
        handler = type("H", (_Handler,), {"behaviour": behaviour, "delay_s": delay_s,
                                          "received": received})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", received

    yield start
    rs.shutdown()
    for s in servers:
        s.shutdown()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_otlp_export_sends_protobuf_with_bearer_key(backend: Any) -> None:
    url, received = backend("ok")
    rs.init(rs.Config(api_key="rs_test_key", endpoint=url, service_name="svc"))
    assert run_agent() == 2
    assert rs.flush(5)
    assert len(received) >= 1
    headers, body = received[0]
    assert headers["authorization"] == "Bearer rs_test_key"
    assert headers["content-type"] == "application/x-protobuf"
    request = ExportTraceServiceRequest.FromString(body)
    names = {s.name for rs_ in request.resource_spans for ss in rs_.scope_spans for s in ss.spans}
    assert names == {"invoke_agent agent", "execute_tool work"}
    resource = {a.key: a.value.string_value for a in request.resource_spans[0].resource.attributes}
    assert resource["service.name"] == "svc"
    assert rs.stats().exported_spans == 2


def test_backend_down_does_not_raise(caplog: Any) -> None:
    rs.init(rs.Config(api_key="k", endpoint=f"http://127.0.0.1:{_closed_port()}",
                      export_timeout_s=0.5))
    try:
        assert run_agent() == 2
        rs.flush(3)  # may report failure; must not raise
        assert rs.stats().exported_spans == 0
    finally:
        rs.shutdown()


def test_backend_500_counts_failures_without_raising(backend: Any) -> None:
    url, received = backend("error")
    rs.init(rs.Config(api_key="k", endpoint=url, export_timeout_s=1.0))
    assert run_agent() == 2
    rs.flush(5)
    assert received, "exporter should have attempted delivery"
    assert rs.stats().exported_spans == 0


def test_slow_backend_does_not_block_the_hot_path(backend: Any) -> None:
    url, _ = backend("ok", delay_s=2.0)
    rs.init(rs.Config(api_key="k", endpoint=url, export_timeout_s=0.5, schedule_delay_ms=10))
    start = time.perf_counter()
    for _ in range(50):
        run_agent()
    elapsed = time.perf_counter() - start
    # 50 runs x 2 spans with export in a background thread; generous bound for CI noise.
    assert elapsed < 0.5, f"instrumented calls took {elapsed:.3f}s"


def test_exporter_exceptions_are_contained() -> None:
    class Exploding(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            raise RuntimeError("boom")

        def shutdown(self) -> None:
            raise RuntimeError("boom on shutdown")

    rs.init(rs.Config(), exporter=Exploding())
    assert run_agent() == 2
    rs.flush(5)
    assert rs.stats().failed_spans == 2
    rs.shutdown()  # must not raise


def test_missing_api_key_disables_tracing_with_a_warning(caplog: Any) -> None:
    rs.init(rs.Config(api_key=None))
    assert run_agent() == 2
    assert "no API key" in caplog.text
    rs.shutdown()


def test_invalid_config_raises_config_error() -> None:
    with pytest.raises(rs.ConfigError):
        rs.Config(sample_rate=1.5)
    with pytest.raises(rs.ConfigError):
        rs.Config(endpoint="ftp://x")
