"""``rootsignal`` command line (SDK side)."""

from __future__ import annotations

import argparse
import os
import sys

from rootsignal.replay.runner import RunnerError, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rootsignal")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay", help="run a regression case against local code and get a verdict")
    replay.add_argument("case_id")
    replay.add_argument("--purpose", choices=["reproduce", "verify"], required=True,
                        help="reproduce: before a fix (expect the failure); verify: after it")
    replay.add_argument("--mode", choices=["recorded", "live_model"], default="recorded")
    replay.add_argument("--match", choices=["by_args", "strict_order"], default="by_args")
    replay.add_argument("--allow-live-reads", action="store_true",
                        help="run read tools for real when the recording has no answer")
    replay.add_argument("--allow-host", action="append", default=[], metavar="HOST:PORT",
                        help="also allow this network target (e.g. the model in live_model mode)")
    replay.add_argument("--stubs", action="append", default=[], metavar="MODULE",
                        help="module that registers write-tool stubs (rootsignal.replay.stub)")
    replay.add_argument("--endpoint", default=os.environ.get("ROOTSIGNAL_ENDPOINT",
                                                             "http://127.0.0.1:8000"))
    replay.add_argument("--timeout", type=float, default=300)
    replay.add_argument("--wait", type=float, default=120,
                        help="seconds to wait for the server's verdict")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ROOTSIGNAL_API_KEY")
    if not api_key:
        print("set ROOTSIGNAL_API_KEY (scopes: read, ingest, replay)", file=sys.stderr)
        return 2
    try:
        return run(args.case_id, purpose=args.purpose, mode=args.mode, match=args.match,
                   endpoint=args.endpoint, api_key=api_key,
                   allow_live_reads=args.allow_live_reads, allow_hosts=args.allow_host,
                   stubs=args.stubs, timeout_s=args.timeout, wait_s=args.wait)
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
