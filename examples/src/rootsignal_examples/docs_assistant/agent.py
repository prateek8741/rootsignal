"""Docs assistant: a LangGraph tool-calling agent on a local Ollama model.

It answers questions about this repository from its docs, citing files. It is
the dogfood agent of roadmap increment 10: unlike the scripted support agent,
its failures come from a real small model (malformed or repeated tool calls,
retrieval misses, uncited or invented answers).

Instrumentation is the combination we expect users to have:
- OpenInference's LangChain instrumentor for model, chain and tool spans,
  exporting through Rootsignal's tracer provider;
- the Rootsignal SDK for the run (``@rs.agent_run``) and tool side effects
  (``@rs.tool``).

    ROOTSIGNAL_API_KEY=... uv run python -m rootsignal_examples.docs_assistant \
        "How are API keys stored?"

Settings: ``OLLAMA_HOST`` (default http://127.0.0.1:11434), ``DOCS_ASSISTANT_MODEL``
(default llama3.1:8b), ``DOCS_ASSISTANT_VERSION`` (default v1),
``DOCS_ASSISTANT_NOTES`` (where ``save_note`` writes).

Versions: v1 uses LangGraph's default tool-error handling, where a tool exception
escapes the run; v2 sends the error back to the model as a tool message.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from opentelemetry.sdk.trace import TracerProvider

import rootsignal as rs
from rootsignal_examples.docs_assistant.corpus import corpus_files, corpus_root, index_for

VERSION = os.environ.get("DOCS_ASSISTANT_VERSION", "v1")
MODEL = os.environ.get("DOCS_ASSISTANT_MODEL", "llama3.1:8b")
MAX_STEPS = 6  # model calls per question
MAX_READ_CHARS = 6000

SYSTEM = """You answer questions about the Rootsignal project using only its documentation.
Use the `retrieve` tool to search the docs, and `read_file` to read a whole file when a
search result looks relevant but incomplete. Base every statement on what the tools
returned and cite the file path in square brackets, like [docs/REPLAY.md]. If the docs
don't contain the answer, say "I couldn't find this in the docs." and don't guess.
Only call `save_note` when the user explicitly asks you to save or note something."""


def _notes_file() -> Path:
    default = Path(tempfile.gettempdir()) / "rootsignal-docs-assistant-notes.md"
    return Path(os.environ.get("DOCS_ASSISTANT_NOTES", str(default)))


@tool
@rs.tool(side_effect="none")
def retrieve(query: str) -> str:
    """Search the Rootsignal docs. Returns the best-matching sections with their file paths."""
    hits = index_for(corpus_root()).search(query, k=4)
    return json.dumps([{"path": s.path, "section": s.heading, "score": round(score, 2),
                        "text": s.text[:900]} for score, s in hits], ensure_ascii=False)


@tool
@rs.tool(side_effect="read")
def read_file(path: str) -> str:
    """Read one documentation file, e.g. docs/REPLAY.md. Only files in the docs corpus."""
    root = corpus_root()
    allowed = {p.relative_to(root).as_posix(): p for p in corpus_files(root)}
    target = allowed.get(path.strip().lstrip("./"))
    if target is None:
        raise FileNotFoundError(f"{path!r} is not a documentation file; available: "
                                f"{', '.join(sorted(allowed))}")
    text = target.read_text(encoding="utf-8")
    return text[:MAX_READ_CHARS] + ("\n…[truncated]" if len(text) > MAX_READ_CHARS else "")


@tool
@rs.tool(side_effect="write")
def save_note(title: str, body: str) -> str:
    """Save a note for the user. Only when the user asks to save or note something."""
    with _notes_file().open("a", encoding="utf-8") as handle:
        handle.write(f"## {title}\n\n{body}\n\n")
    return f"Saved note '{title}'."


TOOLS = [retrieve, read_file, save_note]


def build_graph(model: Any | None = None) -> Any:
    llm = model or ChatOllama(model=MODEL, base_url=os.environ.get("OLLAMA_HOST",
                                                                  "http://127.0.0.1:11434"),
                              temperature=0, seed=7, num_predict=600)
    bound = llm.bind_tools(TOOLS)

    def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [bound.invoke([SystemMessage(SYSTEM), *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    # v1 keeps LangGraph's default handler, which only catches invalid tool arguments:
    # any other tool exception escapes the whole run (found by the dogfood run).
    # v2 returns every tool error to the model as a tool message.
    tool_node = ToolNode(TOOLS) if VERSION == "v1" else ToolNode(TOOLS, handle_tool_errors=True)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)  # → "tools" or END
    graph.add_edge("tools", "agent")
    return graph.compile()


_instrumented: TracerProvider | None = None
_lock = threading.Lock()


def instrument() -> None:
    """(Re)attach OpenInference to the current Rootsignal provider. Idempotent."""
    global _instrumented
    from openinference.instrumentation.langchain import LangChainInstrumentor

    provider = rs.tracer_provider()
    with _lock:
        if provider is None or provider is _instrumented:
            return
        instrumentor = LangChainInstrumentor()
        if _instrumented is not None:
            instrumentor.uninstrument()
        instrumentor.instrument(tracer_provider=provider)
        _instrumented = provider


_graph: Any | None = None


@rs.agent_run("docs-assistant", agent_version=VERSION, max_steps=MAX_STEPS)
def answer(question: str) -> str:
    """Answer one question about the docs."""
    global _graph
    instrument()
    if _graph is None:
        _graph = build_graph()
    result = _graph.invoke({"messages": [("user", question)]},
                           {"recursion_limit": 2 * MAX_STEPS})
    final = result["messages"][-1]
    return str(final.content) if isinstance(final, AIMessage) else str(final)
