"""``rootsignal replay``: run a regression case against local code and get a verdict.

Steps: register the run (scope ``replay``) → run the case in a child process
(network guard, recorded tools, write tools never executed) → submit the
child's report → wait for the server's verdict, computed with the same
detectors that found the bug.

Exit codes (for CI): 0 the expected outcome (``--purpose reproduce``: the
failure reproduced; ``verify``: the case passes and has reproduced before);
1 otherwise; 2 inconclusive, diverged, crashed or timed out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class RunnerError(Exception):
    pass


def _call(endpoint: str, api_key: str, method: str, path: str,
          body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        endpoint.rstrip("/") + path, data=data, method=method,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload: dict[str, Any] = json.loads(response.read() or b"{}")
            return payload
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read())["error"]["message"]
        except Exception:
            message = exc.reason
        raise RunnerError(f"{method} {path}: {exc.code} {message}") from exc
    except urllib.error.URLError as exc:
        raise RunnerError(f"can't reach {endpoint}: {exc.reason}") from exc


def run(case_id: str, *, purpose: str, mode: str = "recorded", match: str = "by_args",
        endpoint: str, api_key: str, allow_live_reads: bool = False,
        allow_hosts: list[str] | None = None, stubs: list[str] | None = None,
        timeout_s: float = 300, wait_s: float = 120, out: Any = sys.stdout) -> int:
    started = _call(endpoint, api_key, "POST", f"/v1/regression-cases/{case_id}/replays",
                    {"purpose": purpose, "mode": mode, "match": match})
    run_id = started["run_id"]
    print(f"replay {run_id}: case {case_id} v{started['case_version']} "
          f"({purpose}, {mode}, match={match})", file=out)
    with tempfile.TemporaryDirectory(prefix="rootsignal-replay-") as tmp:
        report_path = Path(tmp) / "report.json"
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps({
            "spec": started["spec"], "run_id": run_id, "case_id": case_id, "mode": mode,
            "match": match, "allow_live_reads": allow_live_reads,
            "allow_hosts": allow_hosts or [], "stubs": stubs or [], "endpoint": endpoint,
            "report_path": str(report_path)}))
        env = {**os.environ, "ROOTSIGNAL_API_KEY": api_key, "ROOTSIGNAL_ENDPOINT": endpoint}
        try:
            child = subprocess.run([sys.executable, "-m", "rootsignal.replay.child",
                                    str(config_path)], env=env, timeout=timeout_s,
                                   capture_output=True, text=True)
            crashed = None if report_path.exists() else (
                f"replay process exited with {child.returncode}: {child.stderr[-1500:]}")
        except subprocess.TimeoutExpired:
            crashed = f"replay timed out after {timeout_s:.0f} s"
        result = json.loads(report_path.read_text()) if report_path.exists() else {}
    report = result.get("report", {})
    crashed = crashed or result.get("fatal")
    _call(endpoint, api_key, "POST", f"/v1/replay-runs/{run_id}/report",
          {"trace_id": report.get("trace_id"), "report": report, "crashed": crashed})

    deadline = time.monotonic() + wait_s
    state = _call(endpoint, api_key, "GET", f"/v1/replay-runs/{run_id}")
    while state["status"] not in ("evaluated", "error") and time.monotonic() < deadline:
        time.sleep(0.5)
        state = _call(endpoint, api_key, "GET", f"/v1/replay-runs/{run_id}")
    if state["status"] not in ("evaluated", "error"):
        print(f"no verdict after {wait_s:.0f} s: is the Rootsignal worker running?", file=out)
        return 2
    case = _call(endpoint, api_key, "GET", f"/v1/regression-cases/{case_id}")
    _print_result(state, case, report, out)
    verdict = state["verdict"]
    if verdict in ("inconclusive", "diverged", "crashed"):
        return 2
    if purpose == "reproduce":
        return 0 if verdict == "failing" else 1
    return 0 if verdict == "passing" and case["status"] == "passing" else 1


def _print_result(state: dict[str, Any], case: dict[str, Any], report: dict[str, Any],
                  out: Any) -> None:
    print(f"verdict: {state['verdict']}  (case status: {case['status']})", file=out)
    print(f"  {state['summary']}", file=out)
    for row in state.get("results", []):
        if row.get("evaluated"):
            mark = "pass" if row["passed"] else "FAIL"
            print(f"  [{mark}] {row['id']} {row['description']} ({row['observed']})", file=out)
        else:
            print(f"  [skip] {row['id']} {row['description']} ({row['reason']})", file=out)
    served = report.get("served_tools", 0)
    stubs = len(report.get("stubs", []))
    print(f"  tools served from recording: {served}, write/unknown tools stubbed: {stubs}, "
          f"model outputs served: {report.get('served_model_calls', 0)}", file=out)
    if state.get("trace_id"):
        print(f"  replay trace: {state['trace_id']}", file=out)
