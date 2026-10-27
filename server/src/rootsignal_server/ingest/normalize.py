"""OTLP spans → canonical spans and payloads. Pure: no I/O, no database.

Dialects handled here:

* ``rootsignal`` (our SDK);
* ``gen_ai`` (spans that use only the OpenTelemetry GenAI conventions);
* ``openinference`` (Arize OpenInference instrumentors). The adapter was built
  from traces recorded with ``openinference-instrumentation-langchain`` 0.1.79
  on LangGraph 1.2 (``server/tests/fixtures/openinference/``). Flattened
  message attributes become the payload, ``input.value``/``output.value``
  become payloads, ``llm.provider`` wins over ``llm.system``.

OpenLLMetry spans are *detected* and stored with their attributes, but mapped
to a kind only via ``gen_ai.operation.name``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus

from rootsignal.redaction import Redactor
from rootsignal_server.ingest.pricing import PriceTable

RedactOnIngest = Literal["always", "if_not_sdk", "never"]

TRUNCATION_MARKER = "…[truncated]"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_NAME_CHARS = 512

# Payload attribute pairs (input, output) per canonical kind.
_PAYLOAD_KEYS: dict[str, tuple[str, str]] = {
    "agent": ("rootsignal.run.input", "rootsignal.run.output"),
    "llm": ("gen_ai.input.messages", "gen_ai.output.messages"),
    "tool": ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result"),
    "mcp": ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result"),
    "retrieval": ("gen_ai.retrieval.query.text", "gen_ai.retrieval.documents"),
}
_ROOTSIGNAL_KINDS = {"agent", "llm", "tool", "retrieval"}
_GEN_AI_OPERATION_KINDS = {
    "chat": "llm", "text_completion": "llm", "generate_content": "llm", "embeddings": "llm",
    "execute_tool": "tool", "invoke_agent": "agent", "create_agent": "agent",
    "retrieval": "retrieval", "invoke_workflow": "chain",
}
# AGENT → chain: the LangChain instrumentor labels any graph node *named* "agent" as
# AGENT (one per step), so it doesn't mark an agent run. Runs come from our SDK's
# invoke_agent span or GenAI ``invoke_agent``.
_OPENINFERENCE_KINDS = {"LLM": "llm", "EMBEDDING": "llm", "TOOL": "tool", "RETRIEVER": "retrieval",
                        "CHAIN": "chain", "AGENT": "chain"}
_STATUS = {OtlpStatus.STATUS_CODE_UNSET: "unset", OtlpStatus.STATUS_CODE_OK: "ok",
           OtlpStatus.STATUS_CODE_ERROR: "error"}
_SIDE_EFFECTS = {"none", "read", "write", "unknown"}


@dataclass(frozen=True)
class IngestPolicy:
    max_payload_bytes: int = 65_536
    redact_on_ingest: RedactOnIngest = "if_not_sdk"
    max_attribute_value_chars: int = 4096
    max_span_attributes_bytes: int = 65_536
    redactor: Redactor = field(default_factory=Redactor)
    prices: PriceTable = field(default_factory=PriceTable)


@dataclass(frozen=True)
class NormalizedPayload:
    sha256: str
    body: str
    byte_size: int
    truncated: bool
    content_type: Literal["text", "json"]
    redaction_state: Literal["none", "sdk", "server", "unknown"]


@dataclass
class NormalizedSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: str
    name: str
    status: str
    status_message: str | None
    error_type: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: float | None
    model: str | None
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    tool_name: str | None
    side_effect: str | None
    args_hash: str | None
    call_index: int | None
    repeat_index: int | None
    input_payload: NormalizedPayload | None
    output_payload: NormalizedPayload | None
    attributes: dict[str, Any]
    events: list[dict[str, Any]]
    dialect: str
    service_name: str | None
    replay_run_id: str | None = None  # from the replay child's resource attributes


@dataclass
class NormalizedBatch:
    spans: list[NormalizedSpan] = field(default_factory=list)
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    @property
    def received(self) -> int:
        return len(self.spans) + self.rejected

    def error_message(self) -> str:
        return "; ".join(f"{n} span(s): {r}" for r, n in sorted(self.rejection_reasons.items()))


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return attributes_to_dict(value.kvlist_value.values)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind)


def attributes_to_dict(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    return {kv.key: any_value(kv.value) for kv in attributes}


def _time(unix_nano: int) -> datetime | None:
    if unix_nano <= 0:
        return None
    return _EPOCH + timedelta(microseconds=unix_nano // 1000)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    budget = max(max_bytes - len(TRUNCATION_MARKER.encode("utf-8")), 0)
    return encoded[:budget].decode("utf-8", errors="ignore") + TRUNCATION_MARKER, True


def _redact_strings(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)[0]
    if isinstance(value, list):
        return [_redact_strings(v, redactor) for v in value]
    if isinstance(value, dict):
        return {k: _redact_strings(v, redactor) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Span-level normalisation
# ---------------------------------------------------------------------------

def _uuid_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def detect_dialect(attrs: Mapping[str, Any]) -> str:
    if "rootsignal.span.kind" in attrs:
        return "rootsignal"
    if "openinference.span.kind" in attrs:
        return "openinference"
    if "traceloop.span.kind" in attrs:
        return "openllmetry"
    if "gen_ai.operation.name" in attrs or "gen_ai.system" in attrs:
        return "gen_ai"
    return "unknown"


def canonical_kind(dialect: str, attrs: Mapping[str, Any]) -> str:
    if dialect == "rootsignal":
        kind = attrs.get("rootsignal.span.kind")
        if kind in _ROOTSIGNAL_KINDS:
            return "mcp" if kind == "tool" and attrs.get("rootsignal.mcp.server") else str(kind)
    if dialect == "openinference":
        return _OPENINFERENCE_KINDS.get(str(attrs.get("openinference.span.kind")).upper(), "other")
    operation = attrs.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation in _GEN_AI_OPERATION_KINDS:
        return _GEN_AI_OPERATION_KINDS[operation]
    return "other"


def _unflatten(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]] | None:
    """Pop OpenInference's flattened lists (``prefix.<i>.<path>``) into a list of dicts.

    ``llm.input_messages.0.message.tool_calls.1.tool_call.function.name`` becomes
    ``[{"tool_calls": [_, {"function.name": ...}]}]`` with the ``message.`` /
    ``tool_call.`` / ``document.`` wrappers dropped.
    """
    items: dict[int, dict[str, Any]] = {}
    for key in [k for k in attrs if k.startswith(prefix)]:
        index, _, path = key[len(prefix):].partition(".")
        if not index.isdigit() or not path:
            continue
        value = attrs.pop(key)
        path = path.removeprefix("message.").removeprefix("document.")
        item = items.setdefault(int(index), {})
        if path.startswith(("tool_calls.", "contents.")):
            field_name, _, rest = path.partition(".")
            sub_index, _, sub_path = rest.partition(".")
            if not sub_index.isdigit():
                continue
            sub_path = sub_path.removeprefix("tool_call.").removeprefix("message_content.")
            item.setdefault(field_name, {}).setdefault(int(sub_index), {})[sub_path] = value
        else:
            item[path] = value
    for item in items.values():
        for field_name in ("tool_calls", "contents"):
            if field_name in item:
                item[field_name] = [item[field_name][i] for i in sorted(item[field_name])]
    return [items[i] for i in sorted(items)] if items else None


def _openinference_values(kind: str, attrs: dict[str, Any]) -> tuple[Any, Any]:
    """(input, output) payload values for an OpenInference span; pops what it uses."""
    inputs = _unflatten(attrs, "llm.input_messages.")
    outputs = _unflatten(attrs, "llm.output_messages.")
    documents = _unflatten(attrs, "retrieval.documents.")
    raw_in, raw_out = attrs.pop("input.value", None), attrs.pop("output.value", None)
    attrs.pop("input.mime_type", None)
    attrs.pop("output.mime_type", None)
    if kind == "llm":
        # The messages carry the content; input.value repeats them in LangChain's
        # serialised form, so it is only kept when no messages were sent.
        return (inputs if inputs is not None else raw_in,
                outputs if outputs is not None else raw_out)
    if kind == "retrieval":
        return raw_in, documents if documents is not None else raw_out
    return raw_in, raw_out


def _payload(value: Any, *, truncated_by_sdk: bool, redaction_state: str, apply_server: bool,
             policy: IngestPolicy) -> NormalizedPayload:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    state = redaction_state
    if apply_server:
        text, _ = policy.redactor.redact(text)
        state = "server"
    text, truncated_here = _truncate(text, policy.max_payload_bytes)
    truncated = truncated_here or truncated_by_sdk
    content_type: Literal["text", "json"] = "text"
    if not truncated:
        try:
            json.loads(text)
            content_type = "json"
        except ValueError:
            pass
    encoded = text.encode("utf-8")
    return NormalizedPayload(hashlib.sha256(encoded).hexdigest(), text, len(encoded), truncated,
                             content_type, state)  # type: ignore[arg-type]


def derive_args_hash(tool_name: str, arguments: NormalizedPayload) -> str:
    """Fallback for SDKs that don't send ``rootsignal.tool.args_hash``.

    Hashes the stored (possibly redacted) argument text, canonicalised when it
    is JSON. It is stable within a trace, which is all the repeat and retry
    detectors need, but differs from the SDK's raw-argument hash.
    """
    text = arguments.body
    if arguments.content_type == "json":
        text = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    material = f"{tool_name}\x00{text}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def _bound_attributes(attrs: dict[str, Any], policy: IngestPolicy) -> dict[str, Any]:
    out: dict[str, Any] = {}
    used = 0
    for key in sorted(attrs):
        value = attrs[key]
        if isinstance(value, str) and len(value) > policy.max_attribute_value_chars:
            value = value[: policy.max_attribute_value_chars] + TRUNCATION_MARKER
        size = len(key) + len(json.dumps(value, ensure_ascii=False, default=str))
        if used + size > policy.max_span_attributes_bytes:
            out["rootsignal.server.attributes_truncated"] = True
            break
        out[key] = value
        used += size
    return out


def _error_type(attrs: Mapping[str, Any], events: list[dict[str, Any]]) -> str | None:
    explicit = _str(attrs.get("error.type"))
    if explicit:
        return explicit
    for event in events:
        if event["name"] == "exception":
            return _str(event["attributes"].get("exception.type"))
    return None


def _cost(attrs: Mapping[str, Any], provider: str | None, model: str | None,
          input_tokens: int | None, output_tokens: int | None,
          prices: PriceTable) -> Decimal | None:
    explicit = attrs.get("rootsignal.cost.usd")
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        try:
            return Decimal(str(explicit)).quantize(Decimal("0.000001"))
        except InvalidOperation:
            return None
    return prices.cost(provider, model, input_tokens, output_tokens)


def normalize_span(span: OtlpSpan, *, resource: Mapping[str, Any], policy: IngestPolicy,
                   batch: NormalizedBatch) -> NormalizedSpan | None:
    if len(span.trace_id) != 16 or not any(span.trace_id):
        batch.reject("invalid trace_id")
        return None
    if len(span.span_id) != 8 or not any(span.span_id):
        batch.reject("invalid span_id")
        return None
    if span.parent_span_id and len(span.parent_span_id) != 8:
        batch.reject("invalid parent_span_id")
        return None
    started_at = _time(span.start_time_unix_nano)
    if started_at is None:
        batch.reject("missing start time")
        return None
    ended_at = _time(span.end_time_unix_nano)

    attrs = attributes_to_dict(span.attributes)
    dialect = detect_dialect(attrs)
    kind = canonical_kind(dialect, attrs)

    sdk_redaction = resource.get("rootsignal.sdk.redaction")
    redaction_state = {"pattern": "sdk", "off": "none"}.get(str(sdk_redaction), "unknown")
    if dialect != "rootsignal" and redaction_state == "sdk":
        # Third-party instrumentors can share our SDK's provider (and resource), but
        # the SDK only redacts its own spans: these payloads were never redacted.
        redaction_state = "unknown"
    apply_server = policy.redact_on_ingest == "always" or (
        policy.redact_on_ingest == "if_not_sdk" and redaction_state != "sdk")

    sdk_truncated = set(attrs.get("rootsignal.payload.truncated_fields") or [])
    payloads: list[NormalizedPayload | None] = [None, None]
    if dialect == "openinference":
        for i, value in enumerate(_openinference_values(kind, attrs)):
            if value is not None:
                payloads[i] = _payload(value, truncated_by_sdk=False,
                                       redaction_state=redaction_state,
                                       apply_server=apply_server, policy=policy)
    else:
        for i, key in enumerate(_PAYLOAD_KEYS.get(kind, ())):
            if key in attrs:
                payloads[i] = _payload(attrs.pop(key), truncated_by_sdk=key in sdk_truncated,
                                       redaction_state=redaction_state,
                                       apply_server=apply_server, policy=policy)

    events = [{"name": e.name, "time": (_time(e.time_unix_nano) or started_at).isoformat(),
               "attributes": attributes_to_dict(e.attributes)} for e in span.events]
    status_message = span.status.message or None
    if apply_server:
        attrs = _redact_strings(attrs, policy.redactor)
        events = _redact_strings(events, policy.redactor)
        status_message = policy.redactor.redact(status_message)[0] if status_message else None

    model = (_str(attrs.get("gen_ai.response.model")) or _str(attrs.get("gen_ai.request.model"))
             or _str(attrs.get("llm.model_name")))
    # OpenInference: llm.provider is the hosting provider; llm.system is the API family
    # and is wrong for some integrations (ChatOllama reports "openai").
    provider = (_str(attrs.get("gen_ai.provider.name")) or _str(attrs.get("gen_ai.system"))
                or _str(attrs.get("llm.provider")) or _str(attrs.get("llm.system")))
    input_tokens = _int(attrs.get("gen_ai.usage.input_tokens",
                                  attrs.get("gen_ai.usage.prompt_tokens",
                                            attrs.get("llm.token_count.prompt"))))
    output_tokens = _int(attrs.get("gen_ai.usage.output_tokens",
                                   attrs.get("gen_ai.usage.completion_tokens",
                                             attrs.get("llm.token_count.completion"))))
    side_effect = attrs.get("rootsignal.tool.side_effect")
    is_tool = kind in ("tool", "mcp")
    tool_name = (_str(attrs.get("gen_ai.tool.name")) or _str(attrs.get("tool.name"))
                 if is_tool else None)
    args_hash = _str(attrs.get("rootsignal.tool.args_hash"))
    if is_tool and args_hash is None and payloads[0] is not None:
        args_hash = derive_args_hash(tool_name or span.name, payloads[0])
        attrs["rootsignal.server.args_hash_derived"] = True

    return NormalizedSpan(
        trace_id=span.trace_id.hex(),
        span_id=span.span_id.hex(),
        parent_span_id=span.parent_span_id.hex() if span.parent_span_id else None,
        kind=kind,
        name=(span.name or "unnamed")[:_MAX_NAME_CHARS],
        status=_STATUS.get(span.status.code, "unset"),
        status_message=status_message[:1024] if status_message else None,
        error_type=_error_type(attrs, events),
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=((span.end_time_unix_nano - span.start_time_unix_nano) / 1e6
                     if ended_at else None),
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=(_cost(attrs, provider, model, input_tokens, output_tokens, policy.prices)
                  if kind == "llm" else None),
        tool_name=tool_name,
        side_effect=(side_effect if side_effect in _SIDE_EFFECTS else "unknown") if is_tool
        else None,
        args_hash=args_hash,
        call_index=_int(attrs.get("rootsignal.tool.call_index")),
        repeat_index=_int(attrs.get("rootsignal.tool.repeat_index")),
        input_payload=payloads[0],
        output_payload=payloads[1],
        attributes=_bound_attributes(attrs, policy),
        events=events,
        dialect=dialect,
        service_name=_str(resource.get("service.name")),
        replay_run_id=_uuid_or_none(resource.get("rootsignal.replay.run_id")),
    )


def normalize(request: ExportTraceServiceRequest, policy: IngestPolicy) -> NormalizedBatch:
    batch = NormalizedBatch()
    for resource_spans in request.resource_spans:
        resource = attributes_to_dict(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                normalized = normalize_span(span, resource=resource, policy=policy, batch=batch)
                if normalized is not None:
                    batch.spans.append(normalized)
    return batch
