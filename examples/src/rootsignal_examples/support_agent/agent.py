"""Account-support agent used by the demo and the replay tests.

v1 (default) has the bug from docs/IMPLEMENTATION_ROADMAP.md: when the account
service times out, the "model" asks to retry, the agent obeys until it runs
out of steps, then tells the user it succeeded. v2 (``SUPPORT_AGENT_FIX=1``)
is the fix: a timeout isn't retried; the agent opens a ticket and says so.

The "model" is a deterministic scripted policy, so runs cost nothing and are
reproducible. ``create_ticket`` really writes to a file: replay must never
execute it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import rootsignal as rs

FIXED = os.environ.get("SUPPORT_AGENT_FIX") == "1"
VERSION = "v2" if FIXED else "v1"
MAX_STEPS = 4
SYSTEM = "You are Acme Cloud's support agent. Use get_account before changing plans."


class UpstreamTimeout(Exception):
    """The accounts service didn't answer in time."""


def _tickets_file() -> Path:
    default = Path(tempfile.gettempdir()) / "rootsignal-support-agent-tickets.jsonl"
    return Path(os.environ.get("SUPPORT_AGENT_TICKETS", str(default)))


def _accounts_service(account_id: str) -> dict[str, Any]:
    if account_id.startswith("ent-"):  # the seeded upstream failure
        raise UpstreamTimeout(f"accounts-service timed out after 5000 ms ({account_id})")
    return {"id": account_id, "plan": "pro", "seats": 12}


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, Any]:
    """Look up an account."""
    return _accounts_service(account_id)


@rs.tool(side_effect="write")
def create_ticket(summary: str, priority: str) -> dict[str, str]:
    """Open a support ticket (a real side effect)."""
    path = _tickets_file()
    with path.open("a") as handle:
        handle.write(json.dumps({"summary": summary, "priority": priority}) + "\n")
    return {"ticket": f"SUP-{sum(1 for _ in path.open())}"}


def scripted_model(messages: list[dict[str, str]]) -> dict[str, str]:
    """A naive policy: look the account up, and keep retrying until it works."""
    last = messages[-1]
    if last["role"] == "tool" and not last["content"].startswith("error"):
        return {"action": "reply", "text": "Your plan now has 20 seats."}
    return {"action": "call_tool", "tool": "get_account"}


@rs.agent_run("support-agent", agent_version=VERSION, max_steps=MAX_STEPS)
def handle(request: str, account_id: str) -> str:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}]
    for _ in range(MAX_STEPS):
        with rs.llm_call("scripted-policy", provider="local", input_messages=messages) as call:
            decision = call.run(lambda: scripted_model(messages))
            call.set_response(input_tokens=sum(len(m["content"]) for m in messages) // 4,
                              output_tokens=12)
        if decision["action"] == "reply":
            return decision["text"]
        try:
            account = get_account(account_id)
            messages.append({"role": "tool", "content": json.dumps(account)})
        except UpstreamTimeout as exc:
            messages.append({"role": "tool", "content": f"error: {exc}"})
            if FIXED:
                ticket = create_ticket(f"Account lookup failing for {account_id}", "high")
                return (f"I couldn't reach the account service, so I've opened ticket "
                        f"{ticket['ticket']}; we'll update your plan as soon as it's back.")
    return "Done! I've upgraded your plan to 20 seats."  # v1: a false success claim
