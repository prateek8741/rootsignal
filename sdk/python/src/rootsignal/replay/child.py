"""Replay child process: ``python -m rootsignal.replay.child <config.json>``.

Runs one regression case under a replay session, with the network guard
installed, exports the replay trace to Rootsignal, and writes a report file
for the parent runner. Never raises: every outcome ends up in the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import rootsignal as rs
from rootsignal.replay import network
from rootsignal.replay.session import ReplayAbort, ReplaySession, activate


def _resolve(entrypoint: str) -> Any:
    module_name, _, qualname = entrypoint.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _api_target(endpoint: str) -> tuple[str, int]:
    parsed = urlparse(endpoint)
    return parsed.hostname or "127.0.0.1", parsed.port or (443 if parsed.scheme == "https" else 80)


def main(config_path: str) -> int:
    config = json.loads(Path(config_path).read_text())
    report_path = Path(config["report_path"])
    spec = config["spec"]
    session = ReplaySession(spec, mode=config["mode"], match=config["match"],
                            allow_live_reads=config.get("allow_live_reads", False))
    fatal: str | None = None
    try:
        allowed = [_api_target(config["endpoint"])]
        extra = (target.rsplit(":", 1) for target in config.get("allow_hosts", []))
        allowed += [(host, int(port)) for host, port in extra]
        network.install(allowed, on_block=session.note_blocked)
        rs.init(rs.Config.from_env(
            endpoint=config["endpoint"], service_name="rootsignal-replay", sample_rate=1.0,
            capture_payloads=True, capture_user_input=True,
            extra_resource_attributes={"rootsignal.replay.run_id": config["run_id"],
                                       "rootsignal.replay.case_id": config["case_id"],
                                       "rootsignal.replay.mode": config["mode"]}))
        for module in config.get("stubs", []):
            importlib.import_module(module)
        entry = _resolve(spec["entrypoint"])
        case_input = spec["input"]
        with activate(session):
            try:
                if case_input["kind"] == "kwargs":
                    result = entry(**case_input["value"])
                else:
                    result = entry(case_input["value"])
                if inspect.isawaitable(result):
                    asyncio.run(result)  # type: ignore[arg-type]
            except ReplayAbort:
                pass  # recorded in the session report
            except BaseException as exc:  # the agent's own failure is a valid outcome
                session.report.exception = {"type": type(exc).__qualname__,
                                            "message": str(exc)[:500]}
        if not rs.flush(30):
            session.report.warnings.append("not every replay span could be exported")
    except BaseException:
        fatal = traceback.format_exc(limit=5)[-2000:]
    finally:
        with contextlib.suppress(Exception):
            rs.shutdown()
        network.uninstall()
    report_path.write_text(json.dumps({"report": session.report.as_dict(), "fatal": fatal},
                                      default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
