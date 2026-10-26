"""Ask the docs assistant one question and export the trace.

    ROOTSIGNAL_API_KEY=... uv run python -m rootsignal_examples.docs_assistant \
        "How are API keys stored?"
"""

from __future__ import annotations

import sys

import rootsignal as rs
from rootsignal_examples.docs_assistant.agent import answer


def main() -> int:
    question = " ".join(sys.argv[1:]) or "How are API keys stored?"
    rs.init(rs.Config.from_env(service_name="docs-assistant", capture_user_input=True))
    try:
        print(answer(question))
    finally:
        rs.flush(10)
        rs.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
