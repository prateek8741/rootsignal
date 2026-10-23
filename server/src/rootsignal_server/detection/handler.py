"""Job handler: run detectors on a completed trace."""

from __future__ import annotations

import logging

from sqlalchemy import Connection

from rootsignal_server.detection import store
from rootsignal_server.detection.detectors import run_detectors
from rootsignal_server.jobs.queue import ClaimedJob

logger = logging.getLogger("rootsignal.detection")


def handle_trace_completed(conn: Connection, job: ClaimedJob) -> None:
    if job.project_id is None:
        raise ValueError("trace_completed job without a project")
    trace_id = job.payload.get("trace_id")
    if not isinstance(trace_id, str):
        raise ValueError("trace_completed job without a trace_id")
    loaded = store.load_trace(conn, job.project_id, trace_id)
    if loaded is None:
        # Deleted (project removed, retention) between enqueue and now: nothing to do.
        logger.info("trace %s no longer exists; skipping detection", trace_id)
        return
    scope, trace = loaded
    findings = run_detectors(trace, store.load_config(conn, job.project_id))
    store.save_findings(conn, scope, trace, findings)
