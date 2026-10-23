"""Load traces for detection and persist findings. All queries are project-scoped."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, text

from rootsignal_server.detection.config import DetectorConfig
from rootsignal_server.detection.detectors import DETECTORS
from rootsignal_server.detection.model import Finding, SpanView, TraceView

logger = logging.getLogger("rootsignal.detection")

_SPAN_SELECT = (
    "SELECT span_id, parent_span_id, kind, name, status, status_message, error_type, "
    "started_at, ended_at, duration_ms, model, input_tokens, output_tokens, tool_name, "
    "side_effect, args_hash, call_index, attributes, events "
    "FROM spans WHERE project_id = :p AND trace_id = :t"
)


@dataclass(frozen=True)
class TraceScope:
    org_id: uuid.UUID
    project_id: uuid.UUID
    environment_id: uuid.UUID
    trace_id: str


def load_trace(conn: Connection, project_id: uuid.UUID,
               trace_id: str) -> tuple[TraceScope, TraceView] | None:
    head = conn.execute(text(
        "SELECT org_id, environment_id FROM traces WHERE project_id = :p AND trace_id = :t"),
        {"p": project_id, "t": trace_id}).one_or_none()
    if head is None:
        return None
    rows = conn.execute(text(_SPAN_SELECT), {"p": project_id, "t": trace_id}).mappings().all()
    view = TraceView.build(trace_id, (SpanView.from_mapping(dict(r)) for r in rows))
    return TraceScope(head.org_id, project_id, head.environment_id, trace_id), view


def load_config(conn: Connection, project_id: uuid.UUID) -> DetectorConfig:
    overrides = conn.execute(text("SELECT detector_config FROM projects WHERE id = :p"),
                             {"p": project_id}).scalar_one()
    try:
        return DetectorConfig.from_overrides(overrides)
    except ValueError as exc:
        # Never let a bad override silently switch detection off: use defaults.
        logger.error("invalid detector_config for project %s (%s); using defaults",
                     project_id, exc)
        return DetectorConfig()


def save_findings(conn: Connection, scope: TraceScope, trace: TraceView,
                  findings: Sequence[Finding]) -> int:
    """Replace this trace's findings with ``findings`` (upsert + delete stale).

    Upserting keeps finding IDs stable across re-detection, which issue
    occurrences (increment 4) will reference.
    """
    keep_detectors: list[str] = []
    keep_keys: list[str] = []
    for f in findings:
        conn.execute(text(
            "INSERT INTO findings (org_id, project_id, environment_id, trace_id, detector, "
            "detector_version, finding_key, severity, title, summary, evidence_span_ids, facts, "
            "agent_name, agent_version, tool_name, error_type) VALUES (:o, :p, :e, :t, :d, :v, "
            ":k, :sev, :title, :summary, :evidence, CAST(:facts AS jsonb), :agent, :agent_v, "
            ":tool, :err) ON CONFLICT (project_id, trace_id, detector, finding_key) DO UPDATE SET "
            "detector_version = EXCLUDED.detector_version, severity = EXCLUDED.severity, "
            "title = EXCLUDED.title, summary = EXCLUDED.summary, "
            "evidence_span_ids = EXCLUDED.evidence_span_ids, facts = EXCLUDED.facts, "
            "agent_name = EXCLUDED.agent_name, agent_version = EXCLUDED.agent_version, "
            "tool_name = EXCLUDED.tool_name, error_type = EXCLUDED.error_type, updated_at = now()"),
            {"o": scope.org_id, "p": scope.project_id, "e": scope.environment_id,
             "t": scope.trace_id, "d": f.detector, "v": f.detector_version, "k": f.key,
             "sev": f.severity, "title": f.title, "summary": f.summary,
             "evidence": list(f.evidence_span_ids), "facts": json.dumps(dict(f.facts)),
             "agent": trace.agent_name, "agent_v": trace.agent_version, "tool": f.tool_name,
             "err": f.error_type})
        keep_detectors.append(f.detector)
        keep_keys.append(f.key)
    conn.execute(text(
        "DELETE FROM findings WHERE project_id = :p AND trace_id = :t "
        "AND (detector, finding_key) NOT IN ("
        "  SELECT * FROM unnest(CAST(:dets AS text[]), CAST(:keys AS text[])))"),
        {"p": scope.project_id, "t": scope.trace_id, "dets": keep_detectors,
         "keys": keep_keys})
    return len(findings)


def detector_versions() -> dict[str, int]:
    return {d.name: d.version for d in DETECTORS}
