"""Rootsignal SDK: OpenTelemetry-based tracing for AI agents.

    import rootsignal as rs

    rs.init(api_key="...", service_name="support-agent")

    @rs.tool(side_effect="read")
    def get_account(account_id: str) -> dict: ...

    @rs.agent_run("support-agent", agent_version="v1")
    def handle(request: str) -> str:
        with rs.llm_call("llama3.1:8b", provider="ollama", input_messages=msgs) as call:
            ...
            call.set_response(reply, input_tokens=..., output_tokens=...)
"""

from rootsignal._runtime import ExportStats, flush, init, shutdown, stats, tracer_provider
from rootsignal._version import __version__
from rootsignal.config import Config, ConfigError
from rootsignal.redaction import Redactor
from rootsignal.tracing import (
    AgentRun,
    LLMCall,
    Retrieval,
    SideEffect,
    ToolCall,
    agent_run,
    current_scope,
    llm_call,
    retrieval,
    tool,
    tool_call,
)

__all__ = [
    "AgentRun",
    "Config",
    "ConfigError",
    "ExportStats",
    "LLMCall",
    "Redactor",
    "Retrieval",
    "SideEffect",
    "ToolCall",
    "__version__",
    "agent_run",
    "current_scope",
    "flush",
    "init",
    "llm_call",
    "retrieval",
    "shutdown",
    "stats",
    "tool",
    "tool_call",
    "tracer_provider",
]
