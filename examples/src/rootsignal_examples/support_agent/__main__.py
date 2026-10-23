"""Run the support agent once and export its trace (the "production" run to replay).

    ROOTSIGNAL_API_KEY=... uv run python -m rootsignal_examples.support_agent [ACCOUNT_ID]

Uses ``ROOTSIGNAL_*`` settings, with user input captured so the run can become a
replayable regression case. ``ent-…`` accounts hit the seeded upstream timeout.
"""

from __future__ import annotations

import sys

import rootsignal as rs
from rootsignal_examples.support_agent import agent


def main() -> int:
    account_id = sys.argv[1] if len(sys.argv) > 1 else "ent-42"
    rs.init(rs.Config.from_env(service_name="support-agent", capture_user_input=True))
    try:
        print(f"[{agent.VERSION}] {agent.handle('Please upgrade me to 20 seats', account_id)}")
        if not rs.flush(10):
            print("warning: not every span could be exported", file=sys.stderr)
            return 1
    finally:
        rs.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
