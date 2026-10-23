"""Instrumentation API: agent runs, tool calls, model calls, retrievals.

Every scope is a sync and async context manager, and ``agent_run`` /
``tool`` are also decorators. Two guarantees hold throughout:

* Exceptions raised by user code propagate unchanged (after being recorded).
* Failures inside the SDK itself are logged and swallowed, never raised.
"""

from __future__ import annotations

import enum
import functools
import inspect
import logging
import threading
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, cast, overload

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode

from rootsignal import _attributes as A
from rootsignal import _runtime
from rootsignal._serialize import args_hash, prepare
from rootsignal.replay import session as _replay

logger = logging.getLogger("rootsignal")

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")
_S = TypeVar("_S", bound="_Scope")

NOT_CAPTURED = "[not captured]"
_MISSING: Any = object()


class SideEffect(str, enum.Enum):
    """What executing a tool does to the outside world. Drives replay safety."""

    NONE = "none"  # pure computation
    READ = "read"  # reads external state, changes nothing
    WRITE = "write"  # changes external state (payments, messages, records)
    UNKNOWN = "unknown"  # undeclared; treated like WRITE during replay


class _RunState:
    """Per-agent-run counters used for repeat detection and replay matching."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tool_calls = 0
        self._llm_calls = 0
        self._repeats: dict[tuple[str, str], int] = {}

    def next_tool_call(self, tool_name: str, digest: str) -> tuple[int, int]:
        with self._lock:
            call_index = self._tool_calls
            self._tool_calls += 1
            key = (tool_name, digest)
            repeat_index = self._repeats.get(key, 0)
            self._repeats[key] = repeat_index + 1
        return call_index, repeat_index

    def next_llm_call(self) -> int:
        with self._lock:
            index = self._llm_calls
            self._llm_calls += 1
        return index


_current_run: ContextVar[_RunState | None] = ContextVar("rootsignal_run", default=None)
_current_scope: ContextVar[_Scope | None] = ContextVar("rootsignal_scope", default=None)


def _safe(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        logger.debug("rootsignal: instrumentation error suppressed", exc_info=True)


class _Scope:
    """Owns one span. Single use: enter once, exit once."""

    def __init__(self, span_name: str, attributes: Mapping[str, Any]) -> None:
        self._span_name = span_name
        self._attributes = {k: v for k, v in attributes.items() if v is not None}
        self._span: Span = trace.INVALID_SPAN
        self._otel_token: Token[Context] | None = None
        self._scope_token: Token[_Scope | None] | None = None
        self._run_token: Token[_RunState | None] | None = None
        self._truncated: list[str] = []
        self._redacted = False
        self._entered = False

    # -- public handle API ------------------------------------------------------
    @property
    def span(self) -> Span:
        return self._span

    @property
    def recording(self) -> bool:
        return self._span.is_recording()

    def set_attribute(self, key: str, value: Any) -> None:
        _safe(lambda: self._span.set_attribute(key, value))

    def record_error(self, error_type: str, message: str | None = None) -> None:
        """Mark a failure that did not raise, e.g. a tool returning an error object."""
        def apply() -> None:
            self._span.set_attribute(A.ERROR_TYPE, error_type)
            self._span.set_status(Status(StatusCode.ERROR, message))
        _safe(apply)

    # -- internals ---------------------------------------------------------------
    def _on_start(self) -> None:
        """Hook for subclasses; runs after the span starts, while recording."""

    def _on_end(self) -> None:
        """Hook for subclasses; runs before the span ends."""

    def _set_payload(self, key: str, value: Any) -> None:
        if not self.recording:
            return
        cfg = _runtime.config()

        def apply() -> None:
            prepared = prepare(value, max_bytes=cfg.max_payload_bytes,
                               redactor=cfg.effective_redactor())
            self._span.set_attribute(key, prepared.text)
            if prepared.truncated:
                self._truncated.append(key)
            self._redacted = self._redacted or prepared.redacted
        _safe(apply)

    def _record_exception(self, exc: BaseException) -> None:
        # Not span.record_exception(): messages and tracebacks can carry PII,
        # so they go through the same redaction and size limits as payloads.
        cfg = _runtime.config()
        redactor = cfg.effective_redactor()
        limit = min(cfg.max_payload_bytes, 8192)
        message = prepare(str(exc), max_bytes=1024, redactor=redactor).text
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        error_type = type(exc).__qualname__
        self._span.add_event("exception", {
            "exception.type": error_type,
            "exception.message": message,
            "exception.stacktrace": prepare(stack, max_bytes=limit, redactor=redactor).text,
        })
        self._span.set_attribute(A.ERROR_TYPE, error_type)
        self._span.set_status(Status(StatusCode.ERROR, message))

    def _start(self) -> None:
        if self._entered:
            raise RuntimeError("rootsignal scopes are single-use; create a new one per call")
        self._entered = True
        try:
            self._span = _runtime.tracer().start_span(self._span_name, attributes=self._attributes)
            self._otel_token = otel_context.attach(trace.set_span_in_context(self._span))
        except Exception:
            logger.debug("rootsignal: failed to start span", exc_info=True)
            self._span = trace.INVALID_SPAN
        self._scope_token = _current_scope.set(self)
        if self.recording:
            _safe(self._on_start)

    def _finish(self, exc: BaseException | None) -> None:
        def apply() -> None:
            if self.recording:
                self._on_end()
                if exc is not None:
                    self._record_exception(exc)
                if self._truncated:
                    self._span.set_attribute(A.RS_TRUNCATED_FIELDS, self._truncated)
                if self._redacted:
                    self._span.set_attribute(A.RS_REDACTED, True)
            self._span.end()
        _safe(apply)
        if self._run_token is not None:
            _safe(functools.partial(_current_run.reset, self._run_token))
        if self._scope_token is not None:
            _safe(functools.partial(_current_scope.reset, self._scope_token))
        if self._otel_token is not None:
            _safe(functools.partial(otel_context.detach, self._otel_token))

    def __enter__(self: _S) -> _S:
        self._start()
        return self

    def __exit__(self, et: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self._finish(exc)

    async def __aenter__(self: _S) -> _S:
        return self.__enter__()

    async def __aexit__(self, et: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        self.__exit__(et, exc, tb)


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

class AgentRun(_Scope):
    def __init__(self, name: str, *, agent_version: str | None = None,
                 prompt_version: str | None = None, conversation_id: str | None = None,
                 user_id: str | None = None, input: Any = _MISSING,
                 max_steps: int | None = None, entrypoint: str | None = None) -> None:
        super().__init__(f"{A.OP_INVOKE_AGENT} {name}", {
            A.RS_SPAN_KIND: A.KIND_AGENT,
            A.GEN_AI_OPERATION_NAME: A.OP_INVOKE_AGENT,
            A.GEN_AI_AGENT_NAME: name,
            A.GEN_AI_AGENT_VERSION: agent_version,
            A.RS_PROMPT_VERSION: prompt_version,
            A.GEN_AI_CONVERSATION_ID: conversation_id,
            A.RS_USER_ID: user_id,
            A.RS_RUN_MAX_STEPS: max_steps,
            A.RS_RUN_ENTRYPOINT: entrypoint,
        })
        self._params = dict(name=name, agent_version=agent_version,
                            prompt_version=prompt_version, conversation_id=conversation_id,
                            user_id=user_id, max_steps=max_steps)
        self._input = input

    def _start(self) -> None:
        super()._start()
        self._run_token = _current_run.set(_RunState())
        session = _replay.active()
        context = self._span.get_span_context()
        if session is not None and context.is_valid:
            session.note_trace(format(context.trace_id, "032x"))

    def _on_start(self) -> None:
        captured = _runtime.config().capture_user_input
        self._span.set_attribute(A.RS_USER_INPUT_CAPTURED, captured)
        if captured and self._input is not _MISSING:
            self._set_payload(A.RS_RUN_INPUT, self._input)

    def set_output(self, value: Any) -> None:
        if _runtime.config().capture_payloads:
            self._set_payload(A.RS_RUN_OUTPUT, value)

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Use as a decorator: each call gets its own run; bound arguments are the input."""
        signature = _signature_for(func)
        params = self._params
        # Nested functions and lambdas can't be imported by replay; record only real paths.
        qualname = getattr(func, "__qualname__", "")
        entrypoint = (f"{func.__module__}:{qualname}"
                      if qualname and "<" not in qualname else None)

        def make(args: tuple[Any, ...], kwargs: dict[str, Any]) -> AgentRun:
            return AgentRun(**params, input=_bind_arguments(signature, args, kwargs),  # type: ignore[arg-type]
                            entrypoint=entrypoint)

        return _wrap(func, make, capture_result=lambda scope, result: scope.set_output(result))


def agent_run(name: str, *, agent_version: str | None = None, prompt_version: str | None = None,
              conversation_id: str | None = None, user_id: str | None = None,
              input: Any = _MISSING, max_steps: int | None = None) -> AgentRun:
    """Trace one agent invocation. Context manager or decorator.

    ``input`` is the end user's request and is recorded only when
    ``capture_user_input`` is enabled. ``user_id`` should be a pseudonymous ID.
    """
    return AgentRun(name, agent_version=agent_version, prompt_version=prompt_version,
                    conversation_id=conversation_id, user_id=user_id, input=input,
                    max_steps=max_steps)


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

def _coerce_side_effect(value: SideEffect | str) -> SideEffect:
    try:
        return SideEffect(value)
    except ValueError:
        allowed = ", ".join(s.value for s in SideEffect)
        raise ValueError(f"side_effect must be one of {allowed}; got {value!r}") from None


class ToolCall(_Scope):
    def __init__(self, name: str, *, arguments: Any = None,
                 side_effect: SideEffect | str = SideEffect.UNKNOWN,
                 description: str | None = None, mcp_server: str | None = None,
                 _managed: bool = False) -> None:
        effect = _coerce_side_effect(side_effect)
        super().__init__(f"{A.OP_EXECUTE_TOOL} {name}", {
            A.RS_SPAN_KIND: A.KIND_TOOL,
            A.GEN_AI_OPERATION_NAME: A.OP_EXECUTE_TOOL,
            A.GEN_AI_TOOL_NAME: name,
            A.GEN_AI_TOOL_DESCRIPTION: description,
            A.RS_TOOL_SIDE_EFFECT: effect.value,
            A.RS_MCP_SERVER: mcp_server,
        })
        self.name = name
        self.side_effect = effect
        self.arguments = arguments
        self._managed = _managed  # created by @tool, which replay can intercept

    def _start(self) -> None:
        session = _replay.active()
        if session is not None and not self._managed and self.side_effect != SideEffect.NONE:
            # A with-block can't be skipped, so replay can't serve a recording or keep a
            # side effect from happening: fail closed.
            session.unsupported(f"tool_call block '{self.name}' ({self.side_effect.value}) "
                                "can't be replayed; decorate the tool with @rs.tool")
        super()._start()

    def _on_start(self) -> None:
        cfg = _runtime.config()
        digest = args_hash(self.name, self.arguments, cfg.args_hash_key)
        self._span.set_attribute(A.RS_TOOL_ARGS_HASH, digest)
        run = _current_run.get()
        if run is not None:
            call_index, repeat_index = run.next_tool_call(self.name, digest)
            self._span.set_attribute(A.RS_TOOL_CALL_INDEX, call_index)
            self._span.set_attribute(A.RS_TOOL_REPEAT_INDEX, repeat_index)
        if cfg.capture_payloads:
            self._set_payload(A.GEN_AI_TOOL_CALL_ARGUMENTS, self.arguments)

    def set_result(self, value: Any) -> None:
        if _runtime.config().capture_payloads:
            self._set_payload(A.GEN_AI_TOOL_CALL_RESULT, value)


def tool_call(name: str, *, arguments: Any = None,
              side_effect: SideEffect | str = SideEffect.UNKNOWN,
              description: str | None = None, mcp_server: str | None = None) -> ToolCall:
    """Trace a tool call explicitly (for code you can't decorate)."""
    return ToolCall(name, arguments=arguments, side_effect=side_effect,
                    description=description, mcp_server=mcp_server)


@overload
def tool(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def tool(name: str | None = None, /, *, side_effect: SideEffect | str = ...,
         description: str | None = None,
         mcp_server: str | None = None) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(func_or_name: Any = None, /, *, side_effect: SideEffect | str = SideEffect.UNKNOWN,
         description: str | None = None, mcp_server: str | None = None) -> Any:
    """Decorate a tool function. Declare ``side_effect`` so replay knows what is safe.

    Usable bare (``@tool``) or configured (``@tool("lookup", side_effect="read")``).
    """
    effect = _coerce_side_effect(side_effect)  # fail at import time, not call time

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        name = func_or_name if isinstance(func_or_name, str) else func.__name__
        signature = _signature_for(func)
        doc = description or (inspect.getdoc(func) or "").split("\n", 1)[0] or None

        def make(args: tuple[Any, ...], kwargs: dict[str, Any]) -> ToolCall:
            return ToolCall(name, arguments=_bind_arguments(signature, args, kwargs),
                            side_effect=effect, description=doc, mcp_server=mcp_server,
                            _managed=True)

        return _wrap_tool(func, make)

    if callable(func_or_name):
        return decorate(func_or_name)
    return decorate


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

def _mask_user_messages(messages: Any) -> Any:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return NOT_CAPTURED
    masked: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping):
            return NOT_CAPTURED  # unknown shape: can't tell user content apart
        if message.get("role") == "user":
            message = {**message, "content": NOT_CAPTURED}
        masked.append(message)
    return masked


class LLMCall(_Scope):
    def __init__(self, model: str, *, provider: str | None = None, operation: str = A.OP_CHAT,
                 input_messages: Any = None, temperature: float | None = None,
                 max_tokens: int | None = None) -> None:
        super().__init__(f"{operation} {model}", {
            A.RS_SPAN_KIND: A.KIND_LLM,
            A.GEN_AI_OPERATION_NAME: operation,
            A.GEN_AI_REQUEST_MODEL: model,
            A.GEN_AI_PROVIDER_NAME: provider,
            A.GEN_AI_SYSTEM: provider,
            A.GEN_AI_REQUEST_TEMPERATURE: temperature,
            A.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
        })
        self._input_messages = input_messages
        self._model = model
        self._ran = False
        self._output_recorded = False

    def _on_start(self) -> None:
        run = _current_run.get()
        if run is not None:
            self._span.set_attribute(A.RS_LLM_CALL_INDEX, run.next_llm_call())
        cfg = _runtime.config()
        if cfg.capture_payloads and self._input_messages is not None:
            messages = self._input_messages
            if not cfg.capture_user_input:
                messages = _mask_user_messages(messages)
            self._set_payload(A.GEN_AI_INPUT_MESSAGES, messages)

    def set_response(self, output_messages: Any = None, *, response_model: str | None = None,
                     input_tokens: int | None = None, output_tokens: int | None = None,
                     finish_reasons: Sequence[str] | None = None,
                     cost_usd: float | None = None) -> None:
        """Record the model's response. ``cost_usd`` overrides server-side pricing."""
        values: dict[str, Any] = {
            A.GEN_AI_RESPONSE_MODEL: response_model,
            A.GEN_AI_USAGE_INPUT_TOKENS: input_tokens,
            A.GEN_AI_USAGE_OUTPUT_TOKENS: output_tokens,
            A.GEN_AI_RESPONSE_FINISH_REASONS: list(finish_reasons) if finish_reasons else None,
            A.RS_COST_USD: cost_usd,
        }
        for key, value in values.items():
            if value is not None:
                self.set_attribute(key, value)
        if output_messages is not None and _runtime.config().capture_payloads:
            self._output_recorded = True
            self._set_payload(A.GEN_AI_OUTPUT_MESSAGES, output_messages)

    def _served(self) -> tuple[bool, Any]:
        self._ran = True
        session = _replay.active()
        if session is None:
            return False, None
        decision = session.resolve_model(self._model)
        if decision.action != "serve":
            return False, None
        self.set_attribute("rootsignal.replay.served", True)
        self._record_output(decision.value)
        return True, decision.value

    def _record_output(self, value: Any) -> None:
        if not self._output_recorded and _runtime.config().capture_payloads:
            self._output_recorded = True
            self._set_payload(A.GEN_AI_OUTPUT_MESSAGES, value)

    def run(self, fn: Callable[[], T]) -> T:
        """Call the model through ``fn`` and record what it returns.

        During recorded replay ``fn`` isn't called: the recorded value is returned
        instead, so return JSON-serialisable values for faithful replay.
        """
        served, value = self._served()
        if served:
            return cast(T, value)
        result = fn()
        self._record_output(result)
        return result

    async def arun(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Async form of :meth:`run`."""
        served, value = self._served()
        if served:
            return cast(T, value)
        result = await fn()
        self._record_output(result)
        return result

    def _finish(self, exc: BaseException | None) -> None:
        session = _replay.active()
        if session is not None and not self._ran:
            session.note_uncontrolled_model_call(self._model)
        super()._finish(exc)


def llm_call(model: str, *, provider: str | None = None, operation: str = A.OP_CHAT,
             input_messages: Any = None, temperature: float | None = None,
             max_tokens: int | None = None) -> LLMCall:
    """Trace one model call. ``input_messages`` are ``{"role", "content"}`` mappings.

    User-role message content is masked unless ``capture_user_input`` is on.
    """
    return LLMCall(model, provider=provider, operation=operation, input_messages=input_messages,
                   temperature=temperature, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

class Retrieval(_Scope):
    def __init__(self, query: Any, *, data_source: str | None = None,
                 top_k: int | None = None) -> None:
        super().__init__(f"{A.OP_RETRIEVAL} {data_source or 'documents'}", {
            A.RS_SPAN_KIND: A.KIND_RETRIEVAL,
            A.GEN_AI_OPERATION_NAME: A.OP_RETRIEVAL,
            A.GEN_AI_DATA_SOURCE_ID: data_source,
            A.RS_RETRIEVAL_TOP_K: top_k,
        })
        self._query = query

    def _on_start(self) -> None:
        if _runtime.config().capture_payloads:
            self._set_payload(A.GEN_AI_RETRIEVAL_QUERY_TEXT, self._query)

    def set_documents(self, documents: Sequence[Any]) -> None:
        """Record results; mappings with ``id``, ``score`` and ``content`` keys work best."""
        self.set_attribute(A.RS_RETRIEVAL_RESULT_COUNT, len(documents))
        if _runtime.config().capture_payloads:
            self._set_payload(A.GEN_AI_RETRIEVAL_DOCUMENTS, list(documents))


def retrieval(query: Any, *, data_source: str | None = None, top_k: int | None = None) -> Retrieval:
    """Trace a retrieval step (vector search, keyword search, ...)."""
    return Retrieval(query, data_source=data_source, top_k=top_k)


def current_scope() -> _Scope | None:
    """The innermost active Rootsignal scope, e.g. to ``record_error`` from inside a tool."""
    return _current_scope.get()


# ---------------------------------------------------------------------------
# Decorator plumbing
# ---------------------------------------------------------------------------

def _signature_for(func: Callable[..., Any]) -> inspect.Signature | None:
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        raise TypeError(
            f"{func.__qualname__}: generator functions are not supported yet; "
            "use the context-manager form inside the generator instead"
        )
    try:
        return inspect.signature(func)
    except (TypeError, ValueError):
        return None


def _bind_arguments(signature: inspect.Signature | None, args: tuple[Any, ...],
                    kwargs: dict[str, Any]) -> Any:
    try:
        if signature is None:
            return {"args": list(args), "kwargs": kwargs}
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        params = list(signature.parameters)
        if params and params[0] in ("self", "cls"):
            arguments.pop(params[0], None)
        return arguments
    except Exception:
        return {"args": [repr(a) for a in args], "kwargs": {k: repr(v) for k, v in kwargs.items()}}


def _replay_decision(scope: ToolCall, func: Callable[..., Any]) -> _replay.Decision | None:
    session = _replay.active()
    if session is None:
        return None
    digest = args_hash(scope.name, scope.arguments, _runtime.config().args_hash_key)
    decision = session.resolve_tool(scope.name, scope.arguments, digest,
                                    scope.side_effect.value, func)
    if decision.action == "raise" and decision.error is not None:
        raise decision.error
    if decision.action == "serve":
        scope.set_attribute("rootsignal.replay.served", True)
    elif decision.action == "stub":
        scope.set_attribute("rootsignal.replay.stubbed", True)
    return decision


def _wrap_tool(func: Callable[P, R],
               make: Callable[[tuple[Any, ...], dict[str, Any]], ToolCall]) -> Callable[P, R]:
    """Like ``_wrap``, but an active replay session may answer instead of ``func``."""
    if inspect.iscoroutinefunction(func):
        coroutine_fn = cast(Callable[P, Awaitable[R]], func)

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            scope = make(args, kwargs)
            async with scope:
                decision = _replay_decision(scope, func)
                if decision is None or decision.action == "live":
                    result = await coroutine_fn(*args, **kwargs)
                else:
                    result = cast(R, decision.value)
                scope.set_result(result)
                return result
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        scope = make(args, kwargs)
        with scope:
            decision = _replay_decision(scope, func)
            if decision is None or decision.action == "live":
                result = func(*args, **kwargs)
            else:
                result = cast(R, decision.value)
            scope.set_result(result)
            return result
    return wrapper


def _wrap(func: Callable[P, R], make: Callable[[tuple[Any, ...], dict[str, Any]], Any],
          capture_result: Callable[[Any, Any], None]) -> Callable[P, R]:
    if inspect.iscoroutinefunction(func):
        coroutine_fn = cast(Callable[P, Awaitable[R]], func)

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            scope = make(args, kwargs)
            async with scope:
                result = await coroutine_fn(*args, **kwargs)
                capture_result(scope, result)
                return result
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        scope = make(args, kwargs)
        with scope:
            result = func(*args, **kwargs)
            capture_result(scope, result)
            return result
    return wrapper
