"""``rootsignal-server`` command line: serve, tenants and API keys."""

from __future__ import annotations

import argparse
import getpass
import sys

from rootsignal_server import admin
from rootsignal_server.storage.db import make_engine


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rootsignal-server")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    tenant = commands.add_parser("create-tenant", help="get or create org/project/environment")
    key = commands.add_parser("create-key", help="create an API key (printed once)")
    for p in (tenant, key):
        p.add_argument("--org", required=True, help="organization slug")
        p.add_argument("--project", required=True, help="project slug")
        p.add_argument("--environment", default="development")
    key.add_argument("--name", required=True)
    key.add_argument("--scopes", default="ingest", help="comma-separated: ingest,read,admin")

    revoke = commands.add_parser("revoke-key", help="revoke an API key by its prefix")
    revoke.add_argument("prefix")

    worker = commands.add_parser("worker", help="run the background job worker")
    worker.add_argument("--once", action="store_true",
                        help="maintain + process one batch, then exit")
    worker.add_argument("--poll-interval", type=float, default=1.0)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from rootsignal_server.api.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    if args.command == "worker":
        return _run_worker(once=args.once, poll_interval=args.poll_interval)

    engine = make_engine()
    try:
        with engine.begin() as conn:
            if args.command == "create-tenant":
                ids = admin.ensure_tenant(conn, org_slug=args.org, project_slug=args.project,
                                          environment=args.environment, actor=_actor())
                print(f"org={ids.org_id} project={ids.project_id} "
                      f"environment={ids.environment_id}")
            elif args.command == "create-key":
                ids = admin.find_tenant(conn, args.org, args.project, args.environment)
                created = admin.create_api_key(conn, ids, name=args.name,
                                               scopes=args.scopes.split(","), actor=_actor())
                print(created.plaintext)
                print(f"(prefix {created.prefix}; store this key now, it is not shown again)",
                      file=sys.stderr)
            elif args.command == "revoke-key":
                admin.revoke_api_key(conn, args.prefix, actor=_actor())
                print(f"revoked {args.prefix}")
    except admin.AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


def _run_worker(*, once: bool, poll_interval: float) -> int:
    import logging
    import signal
    import threading

    from rootsignal_server.jobs.worker import Worker, default_handlers

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine()
    worker = Worker(engine, default_handlers(), poll_interval_s=poll_interval)
    try:
        if once:
            worker.maintain()
            worker.run_once()
            print(worker.stats)
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        worker.run_forever(stop)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
