"""Load realistic demo traces into a running Rootsignal API for the dashboard.

Creates (or reuses) org ``demo`` / project ``agents`` / environment ``development``,
sends traces from four synthetic agents through the real SDK, runs the worker
until detection is done, and prints a one-time dashboard login link (read +
triage, single use, valid 5 minutes). The demo project gets a small monthly LLM
token budget so investigations can use a local model if the server has one
configured. No model is called while seeding: model spans carry plausible
token counts.

    uv run rootsignal-server serve            # in another terminal
    uv run python scripts/seed_demo.py [--endpoint http://127.0.0.1:8000] [--runs 40]
"""

from __future__ import annotations

import argparse
import contextlib
import random
import time
from datetime import timedelta

from sqlalchemy import text

import rootsignal as rs
from rootsignal_server import admin
from rootsignal_server.auth.sessions import create_login_link
from rootsignal_server.config import get_settings
from rootsignal_server.jobs.worker import Worker, default_handlers
from rootsignal_server.storage.db import make_engine

SYSTEM = "You are a helpful support agent for Acme Cloud. Use tools; never guess account data."

# Simulated latency (seconds), scaled by --time-scale, so the waterfall has shape.
TIME_SCALE = 0.3
_rng = random.Random(0)


def pause(low: float, high: float) -> None:
    time.sleep(_rng.uniform(low, high) * TIME_SCALE)


class UpstreamTimeout(Exception):
    pass


class InvoiceParseError(Exception):
    pass


def chat(rng: random.Random, user: str, reply: str, *, model: str = "llama3.1:8b",
         tokens: tuple[int, int] | None = None) -> None:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    with rs.llm_call(model, provider="ollama", input_messages=messages,
                     temperature=0.2) as call:
        pause(0.3, 1.2)
        inp, out = tokens or (rng.randint(250, 900), rng.randint(20, 160))
        call.set_response([{"role": "assistant", "content": reply}], input_tokens=inp,
                          output_tokens=out, finish_reasons=["stop"])


@rs.tool(side_effect="read")
def get_account(account_id: str) -> dict[str, object]:
    if account_id.startswith("ent-"):
        pause(0.8, 1.0)  # waits for the upstream timeout
        raise UpstreamTimeout(f"accounts-service timed out after 5000 ms ({account_id})")
    pause(0.05, 0.2)
    return {"id": account_id, "plan": "pro", "seats": 12, "status": "active"}


@rs.tool(side_effect="write")
def create_ticket(summary: str, priority: str) -> dict[str, str]:
    pause(0.1, 0.3)
    return {"ticket": "SUP-4821", "priority": priority}


@rs.tool(side_effect="none")
def search_docs(query: str) -> list[dict[str, object]]:
    pause(0.05, 0.25)
    if "E4" in query:
        return []
    return [{"id": "kb-112", "score": 0.83, "title": "Changing your plan",
             "content": "Plans can be changed from Settings > Billing at any time."}]


@rs.tool(side_effect="read")
def fetch_invoice(invoice_id: str) -> dict[str, object]:
    pause(0.1, 0.4)
    return {"id": invoice_id, "total": None, "currency": "EUR", "lines": 3}


def support_agent(rng: random.Random, version: str) -> None:
    account = rng.choice(["acc-1042", "acc-2290", "ent-0042", "ent-0077", "acc-3311"])
    with rs.agent_run("support-agent", agent_version=version, prompt_version="support-p7",
                      max_steps=4, conversation_id=f"conv-{rng.randint(1000, 9999)}") as run:
        if version == "v1":
            for step in range(4):
                chat(rng, "Can you upgrade my plan to 20 seats?",
                     f"Let me look up your account (attempt {step + 1}).")
                try:
                    account_data = get_account(account)
                    break
                except UpstreamTimeout:
                    continue
            else:
                run.set_output("Done! I've upgraded your plan to 20 seats.")
                return
        else:
            chat(rng, "Can you upgrade my plan to 20 seats?", "Let me look up your account.")
            try:
                account_data = get_account(account)
            except UpstreamTimeout:
                create_ticket(f"Account lookup failing for {account}", "high")
                chat(rng, "Can you upgrade my plan to 20 seats?",
                     "I couldn't reach the account service, so I've opened ticket SUP-4821.")
                run.set_output("I couldn't complete the upgrade; ticket SUP-4821 is open.")
                return
        chat(rng, "Can you upgrade my plan to 20 seats?",
             f"Your {account_data['plan']} plan now has 20 seats.")
        run.set_output("Your plan now has 20 seats.")


def docs_agent(rng: random.Random) -> None:
    question = rng.choice(["How do I change my plan?", "What does error E42 mean?",
                           "How do I rotate API keys?"])
    with rs.agent_run("docs-agent", agent_version="v3", max_steps=6):
        with rs.retrieval(question, data_source="kb-index", top_k=4) as r:
            pause(0.05, 0.15)
            r.set_documents([{"id": "kb-112", "score": round(rng.uniform(0.4, 0.9), 2),
                              "content": "Plans can be changed from Settings > Billing."}])
        attempts = 4 if "E42" in question else 1
        for _ in range(attempts):
            search_docs("E42 error code" if "E42" in question else question)
            chat(rng, question, "Searching the documentation.")
        chat(rng, question, "Here is what I found.")


def billing_agent(rng: random.Random) -> None:
    with contextlib.suppress(InvoiceParseError), rs.agent_run("billing-agent",
                                                              agent_version="v1"):
        chat(rng, "Why is my invoice higher this month?", "Let me fetch the invoice.")
        invoice = fetch_invoice(f"inv-{rng.randint(100, 999)}")
        if invoice["total"] is None and rng.random() < 0.5:
            raise InvoiceParseError("invoice total missing; cannot compute difference")
        chat(rng, "Why is my invoice higher this month?", "You added 3 seats mid-cycle.")


def research_agent(rng: random.Random) -> None:
    with rs.agent_run("research-agent", agent_version="v2"):
        heavy = rng.random() < 0.4
        for _ in range(3):
            chat(rng, "Summarise our Q3 churn interviews.", "Reading interview notes.",
                 tokens=(rng.randint(30_000, 45_000), 2_000) if heavy else None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:5173")
    parser.add_argument("--time-scale", type=float, default=0.3,
                        help="multiplier for simulated latencies (0 = instant)")
    args = parser.parse_args()
    global TIME_SCALE
    TIME_SCALE = args.time_scale

    engine = make_engine()
    with engine.begin() as conn:
        tenant = admin.ensure_tenant(conn, org_slug="demo", project_slug="agents",
                                     environment="development", actor="seed_demo",
                                     org_name="Demo Org", project_name="Agents")
        ingest = admin.create_api_key(conn, tenant, name="seed-ingest", scopes=["ingest"],
                                      actor="seed_demo")
        conn.execute(text("UPDATE projects SET llm_monthly_token_budget = 200000 "
                          "WHERE id = :p AND llm_monthly_token_budget = 0"),
                     {"p": tenant.project_id})

    rng = random.Random(args.seed)
    rs.init(rs.Config(api_key=ingest.plaintext, endpoint=args.endpoint,
                      service_name="demo-agents", max_queue_size=8192))
    agents = [lambda: support_agent(rng, "v1"), lambda: support_agent(rng, "v2"),
              lambda: docs_agent(rng), lambda: billing_agent(rng),
              lambda: research_agent(rng)]
    for _ in range(args.runs):
        rng.choice(agents)()
    rs.flush(30)
    stats = rs.stats()
    rs.shutdown()
    print(f"exported {stats.exported_spans} spans ({stats.failed_spans} failed)")

    worker = Worker(engine, default_handlers(), trace_idle_timeout=timedelta(seconds=60))
    while worker.run_once():
        pass
    print(f"worker: {worker.stats}")
    with engine.begin() as conn:
        token = create_login_link(conn, tenant, scopes=["read", "triage"], created_by="seed_demo",
                                  ttl=timedelta(minutes=get_settings().login_link_ttl_minutes))
    print("\nOpen this once to sign in to the dashboard (expires in a few minutes; get a new "
          "one with `rootsignal-server dashboard-link --org demo --project agents`):")
    print(f"{args.dashboard_url.rstrip('/')}/login/link#token={token}")
    engine.dispose()


if __name__ == "__main__":
    main()
