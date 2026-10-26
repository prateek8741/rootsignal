"""The docs assistant without Ollama: a scripted chat model drives the real
LangGraph graph, OpenInference and the Rootsignal SDK, and the spans go
through the server's normaliser and detectors."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import rootsignal as rs
from rootsignal_examples.docs_assistant import agent
from rootsignal_examples.docs_assistant.corpus import corpus_root, index_for
from rootsignal_server.detection.detectors import run_detectors
from rootsignal_server.detection.model import SpanView, TraceView
from rootsignal_server.ingest.normalize import IngestPolicy, normalize

QUESTIONS = Path(agent.__file__).with_name("questions.json")


class ScriptedModel(GenericFakeChatModel):
    """Replies with the scripted messages in order; tools are accepted and ignored."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedModel:  # type: ignore[override]
        return self


def call(tool: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": f"call-{tool}"}])


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[InMemorySpanExporter]:
    monkeypatch.setenv("DOCS_ASSISTANT_NOTES", str(tmp_path / "notes.md"))
    exporter = InMemorySpanExporter()
    rs.init(rs.Config(service_name="docs-assistant", capture_user_input=True), exporter=exporter)
    yield exporter
    rs.shutdown()
    from openinference.instrumentation.langchain import LangChainInstrumentor

    LangChainInstrumentor().uninstrument()
    agent._instrumented = None
    agent._graph = None


def run(script: list[AIMessage], question: str, exporter: InMemorySpanExporter,
        version: str = "v1") -> tuple[str | None, BaseException | None, TraceView]:
    agent._graph = None
    original = agent.VERSION
    agent.VERSION = version
    try:
        agent.instrument()
        agent._graph = agent.build_graph(ScriptedModel(messages=iter(script)))
        answer, error = None, None
        try:
            answer = agent.answer(question)
        except Exception as exc:
            error = exc
    finally:
        agent.VERSION = original
    assert rs.flush(5)
    batch = normalize(encode_spans(exporter.get_finished_spans()), IngestPolicy())
    view = TraceView.build(batch.spans[0].trace_id,
                           [SpanView.from_normalized(s) for s in batch.spans])
    return answer, error, view


def test_retrieval_answer_is_traced_once_per_tool_call(traced: InMemorySpanExporter) -> None:
    answer, error, view = run([call("retrieve", query="API key storage"),
                               AIMessage(content="Hashed [docs/API_SPECIFICATION.md]")],
                              "How are API keys stored?", traced)
    assert error is None and answer == "Hashed [docs/API_SPECIFICATION.md]"
    assert [s.kind for s in view.spans if s.kind in ("agent", "llm")] == ["agent", "llm", "llm"]
    tools = [s for s in view.spans if s.is_tool]
    assert [(t.dialect, t.tool_name, t.side_effect) for t in tools] == \
        [("rootsignal", "retrieve", "none")]
    assert run_detectors(view) == []


def test_v1_lets_a_tool_error_escape_and_v2_returns_it_to_the_model(
        traced: InMemorySpanExporter) -> None:
    script = [call("read_file", path="../.env"),
              AIMessage(content="I couldn't find this in the docs.")]
    _, error, view = run(script, "Read ../.env", traced, version="v1")
    assert isinstance(error, FileNotFoundError)
    assert {f.detector for f in run_detectors(view)} == {"tool_error", "unhandled_exception"}

    traced.clear()
    answer, error, view = run(script, "Read ../.env", traced, version="v2")
    assert error is None and answer == "I couldn't find this in the docs."
    assert {f.detector for f in run_detectors(view)} == {"tool_error"}


def test_read_file_only_reads_corpus_files() -> None:
    with pytest.raises(FileNotFoundError):
        agent.read_file.invoke({"path": "../.env"})
    with pytest.raises(FileNotFoundError):
        agent.read_file.invoke({"path": "/etc/passwd"})
    assert agent.read_file.invoke({"path": "docs/REPLAY.md"}).startswith("# Replay")


def test_retrieval_finds_the_section_that_answers() -> None:
    hits = index_for(corpus_root()).search("session cookie samesite flags", k=3)
    assert any(s.path in ("docs/DASHBOARD.md", "docs/API_SPECIFICATION.md") for _, s in hits)


def test_question_set_is_well_formed() -> None:
    questions = json.loads(QUESTIONS.read_text())["questions"]
    assert len({q["id"] for q in questions}) == len(questions) == 51
    paths = {p.relative_to(corpus_root()).as_posix() for p in
             [corpus_root() / "README.md", *(corpus_root() / "docs").glob("*.md")]}
    for q in questions:
        assert q["category"] in {"answerable", "unanswerable", "save_note", "read_file", "refuse"}
        assert set(q["expected_sources"]) <= paths, q["id"]
