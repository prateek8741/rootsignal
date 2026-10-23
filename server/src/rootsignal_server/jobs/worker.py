"""Worker loop: claim jobs, run handlers, maintain the queue."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Connection, Engine

from rootsignal_server.jobs import queue
from rootsignal_server.jobs.queue import ClaimedJob

logger = logging.getLogger("rootsignal.worker")

Handler = Callable[[Connection, ClaimedJob], None]


@dataclass
class WorkerStats:
    done: int = 0
    retried: int = 0
    dead: int = 0
    lost_lock: int = 0


@dataclass
class Worker:
    engine: Engine
    handlers: Mapping[str, Handler]
    poll_interval_s: float = 1.0
    batch_size: int = 10
    visibility_timeout: timedelta = timedelta(minutes=5)
    trace_idle_timeout: timedelta = timedelta(seconds=60)
    maintenance_interval_s: float = 15.0
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")
    stats: WorkerStats = field(default_factory=WorkerStats)
    _last_maintenance: float = field(default=float("-inf"), repr=False)

    def maintain(self) -> None:
        with self.engine.begin() as conn:
            stale = queue.requeue_stale(conn, self.visibility_timeout)
            idle = queue.complete_idle_traces(conn, self.trace_idle_timeout)
        if stale or idle:
            logger.info("maintenance: requeued %d stale jobs, completed %d idle traces",
                        stale, idle)
        self._last_maintenance = time.monotonic()

    def run_once(self) -> int:
        """Claim and process one batch. Returns the number of jobs claimed."""
        if time.monotonic() - self._last_maintenance >= self.maintenance_interval_s:
            self.maintain()
        with self.engine.begin() as conn:
            jobs = queue.claim(conn, self.worker_id, limit=self.batch_size)
        for job in jobs:
            self._process(job)
        return len(jobs)

    def _process(self, job: ClaimedJob) -> None:
        handler = self.handlers.get(job.kind)
        try:
            with self.engine.begin() as conn:
                if handler is None:
                    raise LookupError(f"no handler for job kind {job.kind!r}")
                handler(conn, job)
                if not queue.mark_done(conn, job, self.worker_id):
                    # Lock expired and someone else may own it: discard our effects.
                    raise _LostLock
            self.stats.done += 1
        except _LostLock:
            self.stats.lost_lock += 1
            logger.warning("job %d: lock lost before completion; effects rolled back", job.id)
        except Exception as exc:
            error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            with self.engine.begin() as conn:
                status = queue.mark_failed(conn, job, self.worker_id, error)
            if status == "dead":
                self.stats.dead += 1
                logger.error("job %d (%s) dead after %d attempts: %s", job.id, job.kind,
                             job.attempts, error)
            else:
                self.stats.retried += 1
                logger.warning("job %d (%s) failed, will retry: %s", job.id, job.kind, error)

    def run_forever(self, stop: threading.Event) -> None:
        logger.info("worker %s started", self.worker_id)
        while not stop.is_set():
            try:
                claimed = self.run_once()
            except Exception:
                logger.exception("worker loop error; backing off")
                claimed = 0
                stop.wait(5)
            if claimed == 0:
                stop.wait(self.poll_interval_s)
        logger.info("worker %s stopped: %s", self.worker_id, self.stats)


class _LostLock(Exception):
    pass


def default_handlers() -> dict[str, Handler]:
    from rootsignal_server.detection.handler import handle_trace_completed
    from rootsignal_server.investigation.service import make_handlers

    return {"trace_completed": handle_trace_completed, **make_handlers()}
