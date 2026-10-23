from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import rootsignal as rs

Collect = Callable[[], Sequence[ReadableSpan]]


@pytest.fixture
def configure() -> Iterator[Callable[..., Collect]]:
    """Initialise the SDK with an in-memory exporter; returns a span collector."""

    def _configure(**overrides: Any) -> Collect:
        exporter = InMemorySpanExporter()
        rs.init(rs.Config(**overrides), exporter=exporter)

        def collect() -> Sequence[ReadableSpan]:
            assert rs.flush(5)
            return exporter.get_finished_spans()

        return collect

    yield _configure
    rs.shutdown()


@pytest.fixture
def collect(configure: Callable[..., Collect]) -> Collect:
    return configure()


def by_name(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def one(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    matches = by_name(spans, name)
    assert len(matches) == 1, f"expected one {name!r}, got {[s.name for s in spans]}"
    return matches[0]
