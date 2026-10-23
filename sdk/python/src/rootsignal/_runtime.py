"""Process-wide SDK state: configuration, tracer provider, exporter."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from rootsignal import _attributes as A
from rootsignal._version import __version__
from rootsignal.config import Config

logger = logging.getLogger("rootsignal")


@dataclass
class ExportStats:
    exported_spans: int = 0
    failed_spans: int = 0


class SafeExporter(SpanExporter):
    """Wraps an exporter so that no exporter error escapes into the host app."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.stats = ExportStats()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception:
            logger.warning("rootsignal: span export raised; %d spans dropped", len(spans),
                           exc_info=logger.isEnabledFor(logging.DEBUG))
            result = SpanExportResult.FAILURE
        with self._lock:
            if result is SpanExportResult.SUCCESS:
                self.stats.exported_spans += len(spans)
            else:
                self.stats.failed_spans += len(spans)
        return result

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:
            logger.debug("rootsignal: exporter shutdown raised", exc_info=True)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception:
            return False


@dataclass
class _State:
    config: Config
    provider: TracerProvider | None
    exporter: SafeExporter | None
    tracer: trace.Tracer


_lock = threading.Lock()
_state: _State | None = None
_NOOP_TRACER = trace.NoOpTracer()
_DEFAULT_CONFIG = Config()


def _build_exporter(config: Config) -> SpanExporter:
    # Imported lazily so tests and exporter-injected setups don't pay for it.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=config.traces_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=config.export_timeout_s,
    )


def init(config: Config | None = None, *, exporter: SpanExporter | None = None,
         **overrides: Any) -> None:
    """Configure the SDK. Safe to call again; the previous provider is shut down.

    Spans go to a private ``TracerProvider``; the global OpenTelemetry provider
    is left untouched so the SDK never hijacks an application's own tracing.
    Pass ``exporter`` to send spans somewhere other than the Rootsignal API.
    """
    global _state
    cfg = Config.from_env(**overrides) if config is None else config
    with _lock:
        previous = _state
        _state = None
    if previous is not None and previous.provider is not None:
        previous.provider.shutdown()

    if not cfg.enabled:
        new_state = _State(cfg, None, None, _NOOP_TRACER)
    elif exporter is None and not cfg.api_key:
        logger.warning("rootsignal: no API key configured; tracing is disabled")
        new_state = _State(cfg, None, None, _NOOP_TRACER)
    else:
        resource = Resource.create({
            A.SERVICE_NAME: cfg.service_name,
            A.DEPLOYMENT_ENVIRONMENT: cfg.environment,
            A.RS_SDK_VERSION: __version__,
            **cfg.extra_resource_attributes,
        })
        provider = TracerProvider(
            resource=resource, sampler=ParentBased(TraceIdRatioBased(cfg.sample_rate))
        )
        safe = SafeExporter(exporter if exporter is not None else _build_exporter(cfg))
        provider.add_span_processor(BatchSpanProcessor(
            safe,
            max_queue_size=cfg.max_queue_size,
            max_export_batch_size=cfg.max_export_batch_size,
            schedule_delay_millis=cfg.schedule_delay_ms,
            export_timeout_millis=int(cfg.export_timeout_s * 1000),
        ))
        new_state = _State(cfg, provider, safe, provider.get_tracer("rootsignal", __version__))
    with _lock:
        _state = new_state


def config() -> Config:
    state = _state
    return state.config if state is not None else _DEFAULT_CONFIG


def tracer() -> trace.Tracer:
    state = _state
    return state.tracer if state is not None else _NOOP_TRACER


def tracer_provider() -> TracerProvider | None:
    """The SDK's provider, for passing to third-party instrumentors."""
    state = _state
    return state.provider if state is not None else None


def stats() -> ExportStats:
    state = _state
    if state is None or state.exporter is None:
        return ExportStats()
    return ExportStats(**vars(state.exporter.stats))


def flush(timeout_s: float = 5.0) -> bool:
    state = _state
    if state is None or state.provider is None:
        return True
    try:
        return bool(state.provider.force_flush(int(timeout_s * 1000)))
    except Exception:
        logger.debug("rootsignal: flush raised", exc_info=True)
        return False


def shutdown() -> None:
    global _state
    with _lock:
        state, _state = _state, None
    if state is not None and state.provider is not None:
        try:
            state.provider.shutdown()
        except Exception:
            logger.debug("rootsignal: shutdown raised", exc_info=True)
