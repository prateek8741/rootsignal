"""Replay session: serves recorded tool results and model outputs to agent code.

A session is active (via a context variable) while a regression case runs in
the replay child process. Decorated tools and ``llm_call.run`` ask it what to
do instead of touching the outside world:

* read/none tools with a recording get the recorded result, or the recorded
  error re-raised as the same exception class when it can be found;
* write/unknown tools are never executed: a recorded result is returned, or a
  stub (registered with :func:`stub`, else ``None``) and the intent is logged;
* a call the recording can't answer stops the run (:class:`ReplayDivergence`),
  unless live reads were explicitly allowed for side-effect-free tools.

Aborts derive from ``BaseException`` so agent code's ``except Exception``
can't swallow them; every abort is also recorded in the session report.
"""

from __future__ import annotations

import builtins
import contextvars
import sys
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

Mode = Literal["recorded", "live_model"]
Match = Literal["by_args", "strict_order"]


class ReplayAbort(BaseException):
    """Replay can't continue faithfully. Not an ``Exception`` on purpose."""


class ReplayDivergence(ReplayAbort):
    """The code made a call the recording can't answer."""


class ReplayUnsupported(ReplayAbort):
    """A call replay can't intercept (e.g. a ``tool_call`` block with side effects)."""


class ReplayNetworkBlocked(ReplayAbort):
    """Outbound network access outside the allowlist."""


class RecordedToolError(Exception):
    """A recorded tool error whose original exception class couldn't be found."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type


@dataclass
class Decision:
    action: Literal["serve", "raise", "live", "stub"]
    value: Any = None
    error: BaseException | None = None


@dataclass
class ReplayReport:
    trace_id: str | None = None
    served_tools: int = 0
    reused_tools: int = 0
    live_tools: int = 0
    served_model_calls: int = 0
    stubs: list[dict[str, Any]] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    exception: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


_STUBS: dict[str, Callable[..., Any]] = {}


def stub(tool: str, fn: Callable[..., Any]) -> None:
    """Register what a write/unknown tool returns during replay when nothing was recorded."""
    _STUBS[tool] = fn


class ReplaySession:
    def __init__(self, spec: dict[str, Any], *, mode: Mode = "recorded",
                 match: Match = "by_args", allow_live_reads: bool = False) -> None:
        self.mode = mode
        self.match = match
        self.allow_live_reads = allow_live_reads
        self.report = ReplayReport()
        self._lock = threading.Lock()
        tools: list[dict[str, Any]] = sorted(spec.get("fixtures", {}).get("tools", []),
                                             key=lambda t: t["call_index"])
        self._ordered = tools
        self._by_key: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
        for fixture in tools:
            self._by_key[(fixture["tool"], fixture.get("args_hash"))].append(fixture)
        self._key_cursor: dict[tuple[str, str | None], int] = defaultdict(int)
        self._order_cursor = 0
        self._llm = sorted(spec.get("fixtures", {}).get("llm", []), key=lambda f: f["call_index"])
        self._llm_cursor = 0

    # -- tools -----------------------------------------------------------------
    def _next_fixture(self, tool: str, digest: str | None) -> dict[str, Any] | None:
        if self.match == "strict_order":
            if self._order_cursor >= len(self._ordered):
                return None
            fixture = self._ordered[self._order_cursor]
            if fixture["tool"] != tool or fixture.get("args_hash") != digest:
                return None
            self._order_cursor += 1
            return fixture
        key = (tool, digest)
        candidates = self._by_key.get(key)
        if not candidates:
            return None
        index = self._key_cursor[key]
        if index >= len(candidates):
            # Identical call made more often than recorded: assume the same outcome.
            self.report.reused_tools += 1
            last: dict[str, Any] = candidates[-1]
            return last
        self._key_cursor[key] += 1
        chosen: dict[str, Any] = candidates[index]
        return chosen

    def resolve_tool(self, tool: str, arguments: Any, digest: str | None, side_effect: str,
                     func: Callable[..., Any] | None) -> Decision:
        with self._lock:
            fixture = self._next_fixture(tool, digest)
            if fixture is not None:
                if fixture["outcome"] == "error":
                    self.report.served_tools += 1
                    return Decision("raise", error=_rebuild_error(
                        func, fixture.get("error_type") or "Exception",
                        fixture.get("error_message") or ""))
                result = fixture.get("result") or {}
                if not result.get("captured"):
                    return self._diverge(f"recorded result of '{tool}' wasn't captured")
                if result.get("truncated"):
                    self.report.warnings.append(f"served a truncated recorded result of '{tool}'")
                self.report.served_tools += 1
                return Decision("serve", value=result.get("value"))
            if side_effect in ("write", "unknown"):
                self.report.stubs.append({"tool": tool, "arguments": arguments})
                handler = _STUBS.get(tool)
                return Decision("stub", value=handler(**arguments) if handler and isinstance(
                    arguments, dict) else None)
            if side_effect == "none" or (self.allow_live_reads and side_effect == "read"):
                self.report.live_tools += 1
                return Decision("live")
            return self._diverge(f"no recorded result for '{tool}' with these arguments "
                                 f"(match={self.match})")

    def _diverge(self, reason: str) -> Decision:
        self.report.divergences.append(reason)
        raise ReplayDivergence(reason)

    def unsupported(self, reason: str) -> None:
        with self._lock:
            self.report.unsupported.append(reason)
        raise ReplayUnsupported(reason)

    # -- model calls ------------------------------------------------------------
    def resolve_model(self, model: str) -> Decision:
        if self.mode == "live_model":
            return Decision("live")
        with self._lock:
            if self._llm_cursor >= len(self._llm):
                return self._diverge(f"more model calls than recorded ({len(self._llm)}); "
                                     "use --mode live_model to let the model answer")
            fixture = self._llm[self._llm_cursor]
            self._llm_cursor += 1
            output = fixture.get("output") or {}
            if not output.get("captured"):
                return self._diverge("recorded model output wasn't captured")
            self.report.served_model_calls += 1
            return Decision("serve", value=output.get("value"))

    def note_uncontrolled_model_call(self, model: str) -> None:
        with self._lock:
            if self.mode == "recorded":
                self.report.unsupported.append(
                    f"model '{model}' was called outside llm_call.run(); recorded replay "
                    "can't control it")

    def note_blocked(self, target: str) -> None:
        with self._lock:
            self.report.blocked.append(target)

    def note_trace(self, trace_id: str) -> None:
        with self._lock:
            if self.report.trace_id is None:
                self.report.trace_id = trace_id


def _rebuild_error(func: Callable[..., Any] | None, error_type: str,
                   message: str) -> BaseException:
    """Re-raise a recorded error as its original class so ``except`` clauses behave the same."""
    namespaces: list[Any] = []
    if func is not None and func.__module__ in sys.modules:
        namespaces.append(sys.modules[func.__module__])
    namespaces.append(builtins)
    for namespace in namespaces:
        obj: Any = namespace
        for part in error_type.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if isinstance(obj, type) and issubclass(obj, Exception):
            try:
                return obj(message)
            except Exception:  # constructor needs other arguments
                break
    return RecordedToolError(error_type, message)


_active: contextvars.ContextVar[ReplaySession | None] = contextvars.ContextVar(
    "rootsignal_replay", default=None)


def active() -> ReplaySession | None:
    return _active.get()


@contextmanager
def activate(session: ReplaySession) -> Iterator[ReplaySession]:
    token = _active.set(session)
    try:
        yield session
    finally:
        _active.reset(token)
