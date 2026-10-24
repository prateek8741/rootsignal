"""The Phase 1 scenario end to end, from an empty database: observe → detect →
investigate → reproduce → fix → verify.

    ./scripts/demo.sh [--port 8000] [--keep]

1. Recreates the database ``rootsignal_demo`` (``ROOTSIGNAL_DEMO_DATABASE_URL``;
   the name must end in ``_demo``) and migrates it.
2. Starts the API server and the worker as subprocesses.
3. Runs the example support agent (v1, with a retry-loop bug) as "production".
4. Waits for the ``repeated_tool_call`` issue, runs a rules-only investigation,
   and creates a regression case from the worst trace.
5. ``rootsignal replay --purpose reproduce`` on v1 must fail; after the "fix"
   (``SUPPORT_AGENT_FIX=1``, v2) ``--purpose verify`` must pass. The write
   tool must never run during replay.
6. Checks the issue now carries a ``verified`` claim, resolves it as fixed in
   v2, sends v2 production traffic, and checks the issue stays resolved.

Exits 0 only if every check passes. No LLM, no network beyond localhost, no
Docker. With ``--keep`` the server and worker keep running and a dashboard
login link is printed (start the dashboard with ``make dashboard``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "postgresql+psycopg://localhost:5432/rootsignal_demo"
STUBS = "rootsignal_examples.support_agent.replay_stubs"


class DemoFailed(Exception):
    pass


def step(title: str) -> None:
    print(f"\n▸ {title}", flush=True)


def ok(message: str) -> None:
    print(f"  ✓ {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailed(message)
    ok(message)


def wait_until(probe: Callable[[], Any], what: str, timeout_s: float = 30) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise DemoFailed(f"timed out after {timeout_s:.0f} s waiting for {what}")


def recreate_database(url: str) -> None:
    from sqlalchemy import create_engine, text

    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if not name.endswith("_demo"):
        raise DemoFailed(f"refusing to recreate {name!r}: the demo database name must end in _demo")
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "server" / "alembic.ini"))
    config.attributes["url"] = url
    command.upgrade(config, "head")


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


class Api:
    def __init__(self, endpoint: str, key: str) -> None:
        import httpx

        self.client = httpx.Client(base_url=endpoint, timeout=30,
                                   headers={"Authorization": f"Bearer {key}"})

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        response = self.client.post(path, json=body)
        if response.status_code >= 400:
            raise DemoFailed(f"POST {path}: {response.status_code} {response.text}")
        return response.json()


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.endpoint = f"http://127.0.0.1:{args.port}"
        self.tmp = Path(tempfile.mkdtemp(prefix="rootsignal-demo-"))
        self.processes: list[subprocess.Popen[bytes]] = []
        self.env = {**os.environ, "ROOTSIGNAL_DATABASE_URL": args.database_url,
                    "ROOTSIGNAL_ENDPOINT": self.endpoint}
        for name in ("ROOTSIGNAL_ARGS_HASH_KEY", "SUPPORT_AGENT_FIX", "ROOTSIGNAL_SAMPLE_RATE",
                     "ROOTSIGNAL_ENABLED"):
            self.env.pop(name, None)

    # --- processes -------------------------------------------------------------------
    def start(self, name: str, *argv: str) -> None:
        log = (self.tmp / f"{name}.log").open("wb")
        self.processes.append(subprocess.Popen(argv, env=self.env, stdout=log,
                                               stderr=subprocess.STDOUT, cwd=ROOT))

    def stop(self) -> None:
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()

    def agent(self, account_id: str, *, fixed: bool) -> str:
        env = {**self.env, "ROOTSIGNAL_API_KEY": self.key, "ROOTSIGNAL_ENVIRONMENT": "production",
               "SUPPORT_AGENT_TICKETS": str(self.tmp / "production-tickets.jsonl")}
        if fixed:
            env["SUPPORT_AGENT_FIX"] = "1"
        result = subprocess.run([sys.executable, "-m", "rootsignal_examples.support_agent",
                                 account_id], env=env, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise DemoFailed(f"agent run failed: {result.stderr[-1000:]}")
        return result.stdout.strip()

    def replay(self, case_id: str, purpose: str, *, fixed: bool) -> tuple[int, str]:
        env = {**self.env, "ROOTSIGNAL_API_KEY": self.key,
               "SUPPORT_AGENT_TICKETS": str(self.canary)}
        if fixed:
            env["SUPPORT_AGENT_FIX"] = "1"
        result = subprocess.run([sys.executable, "-m", "rootsignal.cli", "replay", case_id,
                                 "--purpose", purpose, "--stubs", STUBS, "--wait", "60"],
                                env=env, capture_output=True, text=True, timeout=180)
        for line in result.stdout.splitlines():
            print(f"    │ {line}")
        if result.stderr.strip():
            print(f"    │ {result.stderr.strip()[-500:]}")
        return result.returncode, result.stdout

    def settled(self) -> bool:
        """Every exported trace has completed and no job is pending."""
        from sqlalchemy import text

        with self.engine.connect() as conn:
            open_traces: int = conn.execute(text(
                "SELECT count(*) FROM traces WHERE completed_at IS NULL")).scalar_one()
            pending: int = conn.execute(text(
                "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")).scalar_one()
        return open_traces == 0 and pending == 0

    # --- the scenario ----------------------------------------------------------------
    def run(self) -> None:
        from sqlalchemy import create_engine

        args = self.args
        step(f"Fresh database ({urlsplit(args.database_url).path.lstrip('/')})")
        if not port_free(args.port):
            raise DemoFailed(f"port {args.port} is in use (is `rootsignal-server serve` already "
                             "running?). Stop it or pass --port 8001 (then start the dashboard "
                             "with ROOTSIGNAL_API=http://127.0.0.1:8001 make dashboard)")
        recreate_database(args.database_url)
        migrate(args.database_url)
        ok("recreated and migrated")
        os.environ["ROOTSIGNAL_DATABASE_URL"] = args.database_url
        self.engine = create_engine(args.database_url)
        from rootsignal_server import admin

        with self.engine.begin() as conn:
            self.tenant = admin.ensure_tenant(
                conn, org_slug="demo", project_slug="support", environment="production",
                actor="demo", org_name="Demo Org", project_name="Support")
            self.key = admin.create_api_key(conn, self.tenant, name="demo",
                                            scopes=["ingest", "read", "triage", "replay"],
                                            actor="demo").plaintext
        ok("tenant demo/support/production and a key (ingest, read, triage, replay)")

        step("Start the API server and the worker")
        self.start("serve", sys.executable, "-m", "rootsignal_server.cli", "serve",
                   "--port", str(args.port))
        self.start("worker", sys.executable, "-m", "rootsignal_server.cli", "worker",
                   "--poll-interval", "0.2")
        api = Api(self.endpoint, self.key)
        self.api = api

        def ready() -> bool:
            try:
                status: int = api.client.get("/readyz").status_code
            except Exception:
                return False
            return status == 200

        wait_until(ready, "the API server")
        ok(f"API at {self.endpoint}; logs in {self.tmp}")

        step("Production traffic: support agent v1 (retries a timing-out lookup)")
        for account in ("acc-1001", "ent-42", "acc-1002", "ent-7", "ent-42"):
            print(f"    {account}: {self.agent(account, fixed=False)}")
        wait_until(self.settled, "detection on the production traces")
        issues = api.get("/v1/issues", limit=50)["items"]
        loop = next((i for i in issues if i["detector"] == "repeated_tool_call"), None)
        check(loop is not None, "detected: 'get_account' called repeatedly with identical "
                                "arguments")
        assert loop is not None
        print(f"    issue {loop['id']}: {loop['title']} ({loop['occurrence_count']} traces); "
              f"{len(issues)} open issues in total")

        step("Investigate (deterministic rules; no LLM)")
        started = api.post(f"/v1/issues/{loop['id']}/investigations", {"use_llm": False})
        investigation = wait_until(
            lambda: (lambda d: d if d["status"] == "completed" else None)(
                api.get(f"/v1/investigations/{started['id']}")), "the investigation")
        kinds = [c["kind"] for c in investigation["claims"]]
        check("observed" in kinds and "verified" not in kinds,
              f"{kinds.count('observed')} observed fact(s), {kinds.count('hypothesis')} "
              "hypothesis(es); nothing verified yet")

        step("Regression case from the worst trace")
        case = api.post(f"/v1/issues/{loop['id']}/regression-cases", {})
        check(case["replayable_recorded"], f"case {case['id']}: {case['assertion_count']} "
                                           "assertions, replayable with recorded data")

        self.canary = self.tmp / "replay-tickets.jsonl"
        step("Reproduce: replay the case against v1 (must fail)")
        code, out = self.replay(case["id"], "reproduce", fixed=False)
        check(code == 0 and "verdict: failing" in out, "the failure reproduces on v1")

        step("Fix (v2: open a ticket instead of retrying) and verify (must pass)")
        code, out = self.replay(case["id"], "verify", fixed=True)
        check(code == 0 and "verdict: passing" in out, "the failure is gone on v2")
        check(not self.canary.exists(), "create_ticket (a write tool) never ran during replay")

        step("Close the loop")
        claims = api.get(f"/v1/investigations/{started['id']}")["claims"]
        verified = [c for c in claims if c["kind"] == "verified"]
        check(len(verified) == 1 and verified[0]["source"] == "replay",
              "the investigation now has a verified claim, set by replay")
        print(f"    {verified[0]['statement']}")
        status = api.get(f"/v1/regression-cases/{case['id']}")["status"]
        check(status == "passing", "case status: passing")
        api.post(f"/v1/issues/{loop['id']}/status",
                 {"status": "resolved", "fixed_in_version": "v2",
                  "note": "Fix verified by replay (demo)"})
        ok("issue resolved as fixed in v2")

        step("Ship v2: production traffic on the fixed agent")
        before = api.get(f"/v1/issues/{loop['id']}")["issue"]
        for account in ("ent-42", "acc-1003"):
            print(f"    {account}: {self.agent(account, fixed=True)}")
        wait_until(self.settled, "detection on the v2 traces")
        after = api.get(f"/v1/issues/{loop['id']}")["issue"]
        check(after["status"] == "resolved" and
              after["occurrence_count"] == before["occurrence_count"],
              "no new repeated-call failures on v2: the issue stays resolved")

        print("\nDemo passed: observe → detect → investigate → reproduce → fix → verify.")
        if args.keep:
            self.keep_running(loop["id"])

    def keep_running(self, issue_id: str) -> None:
        from rootsignal_server.auth.sessions import create_login_link

        with self.engine.begin() as conn:
            token = create_login_link(conn, self.tenant, scopes=["read", "triage"],
                                      created_by="demo", ttl=timedelta(minutes=5))
        print(f"\nServer and worker keep running (logs in {self.tmp}). In another terminal:")
        dashboard_env = "" if self.args.port == 8000 else \
            f"ROOTSIGNAL_API=http://127.0.0.1:{self.args.port} "
        print(f"  {dashboard_env}make dashboard")
        link = f"{self.args.dashboard_url}/login/link#token={token}"
        print(f"then open once (valid 5 minutes): {link}")
        print(f"and go to {self.args.dashboard_url}/issues/{issue_id}. Ctrl-C stops everything.")
        try:
            while all(p.poll() is None for p in self.processes):
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--keep", action="store_true",
                        help="keep the server and worker running and print a dashboard link")
    parser.add_argument("--database-url",
                        default=os.environ.get("ROOTSIGNAL_DEMO_DATABASE_URL", DEFAULT_DB))
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:5173")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]  # progress in CI logs
    # SIGTERM (e.g. from CI) must still stop the server and worker: run the finally block.
    # Later signals are ignored so a repeated SIGTERM can't interrupt that cleanup.
    def on_term(*_: object) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)
    demo = Demo(args)
    try:
        demo.run()
    except DemoFailed as exc:
        print(f"\n✗ demo failed: {exc}\n  logs: {demo.tmp}", file=sys.stderr)
        return 1
    finally:
        demo.stop()
    shutil.rmtree(demo.tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
