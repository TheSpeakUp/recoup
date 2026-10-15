"""FastAPI app: chat with the agent, live audit trail, ledger, human approvals, Stripe webhooks.

Run:  uvicorn app.main:app --reload --port 8000   then open http://localhost:8000
"""
import logging
import os

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config
from .agent import Agent
from .audit import AuditLog
from .gateway import Gateway
from .ledger import Ledger
from .memory import build_memory
from .payments import build_payments
from .policy import PolicyEngine

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="AgentCore: policy-bound money agent")
STATE: dict = {}


def build(reset: bool = True):
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    scenario = yaml.safe_load((config.BASE_DIR / "scenarios" / f"{config.SCENARIO}.yaml").read_text())
    audit, ledger = AuditLog(config.DB_PATH), Ledger(config.DB_PATH)
    memory = build_memory()
    gateway = Gateway(payments=build_payments(), ledger=ledger, audit=audit,
                      policy=PolicyEngine(scenario.get("policy", {})), memory=memory, scenario=scenario)
    STATE.update(scenario=scenario, audit=audit, ledger=ledger, gateway=gateway, agent=Agent(gateway, memory, scenario))


build(reset=os.getenv("RESET_ON_START", "1") == "1")


class NewSession(BaseModel):
    account_ref: str


class Message(BaseModel):
    session_id: str
    text: str


class SimPayment(BaseModel):
    account_ref: str
    amount: int


@app.get("/")
def index():
    return FileResponse(config.BASE_DIR / "app" / "static" / "index.html")


@app.get("/api/state")
def state():
    g: Gateway = STATE["gateway"]
    return {
        "mode": config.mode_summary(),
        "title": STATE["scenario"].get("title"),
        "policy": g.policy.policy,
        "accounts": [g.account_view(r) for r in g.accounts],
        "approvals": list(g.approvals.values()),
        "audit": STATE["audit"].rows(limit=100),
        "chain": STATE["audit"].verify(),
        "ledger": STATE["ledger"].entries(limit=100),
        "balances": STATE["ledger"].balances(),
        "clauses": g.policy.clauses(),
        "findings": list(getattr(g, "findings", {}).values()),
        "recovery": bool(STATE["scenario"].get("recovery")),
    }


@app.post("/api/session")
async def new_session(body: NewSession):
    agent: Agent = STATE["agent"]
    if body.account_ref not in STATE["gateway"].accounts:
        raise HTTPException(404, "unknown account")
    s = agent.new_session(body.account_ref)
    opening = STATE["scenario"]["agent"].get(
        "opening", "[Open the conversation: the customer has just been contacted about their overdue invoice.]")
    out = await agent.handle(s.id, opening)
    return {"session_id": s.id, **out}


@app.post("/api/audit")
async def run_audit():
    return await STATE["agent"].run_audit()


@app.get("/api/explain/{finding_id}")
def explain(finding_id: str):
    g: Gateway = STATE["gateway"]
    return g.execute("explain_finding", {"finding_id": finding_id, "rationale": "Finance asked why."},
                     session="ui", actor="finance")


@app.post("/api/message")
async def message(body: Message):
    agent: Agent = STATE["agent"]
    if body.session_id not in agent.sessions:
        raise HTTPException(404, "unknown session")
    return await agent.handle(body.session_id, body.text)


@app.post("/api/approvals/{approval_id}/{action}")
def approval(approval_id: str, action: str):
    g: Gateway = STATE["gateway"]
    if action == "approve":
        return g.approve(approval_id)
    if action == "reject":
        return g.reject(approval_id)
    raise HTTPException(400, "action must be approve or reject")


@app.post("/api/simulate/payment")
def simulate_payment(body: SimPayment):
    import time
    return STATE["gateway"].record_inbound_payment(body.amount, ref=f"sim_{int(time.time() * 1000)}",
                                                   account_ref=body.account_ref, source="simulated_payment")


@app.post("/api/demo/tamper")
def tamper():
    """Demo only: silently edit the latest audit row to show the hash chain catching it."""
    conn = STATE["audit"].conn
    conn.execute("UPDATE audit SET amount = COALESCE(amount,0) + 100000 WHERE seq = (SELECT MAX(seq) FROM audit)")
    conn.commit()
    return STATE["audit"].verify()


@app.post("/api/reset")
def reset():
    build(reset=True)
    return {"ok": True}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    import stripe
    try:
        if config.STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET).to_dict()
        else:
            import json
            event = json.loads(payload)
    except Exception as e:
        raise HTTPException(400, f"bad webhook: {e}")
    obj = event.get("data", {}).get("object", {})
    g: Gateway = STATE["gateway"]
    etype = event.get("type")
    if etype == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        if not meta.get("account_ref"):
            return {"status": "ignored", "reason": "posted via payment_intent.succeeded"}
        return g.record_inbound_payment(obj.get("amount_total", 0), ref=obj.get("payment_intent") or obj["id"],
                                        account_ref=meta.get("account_ref"), source="payment_link")
    if etype == "payment_intent.succeeded" and (obj.get("metadata") or {}).get("account_ref"):
        return g.record_inbound_payment(obj.get("amount_received", 0), ref=obj["id"],
                                        account_ref=obj["metadata"]["account_ref"], source="payment_link")
    if etype == "invoice.paid":
        acc_ref = next((r for r, a in g.accounts.items() if a.get("customer_id") == obj.get("customer")), None)
        return g.record_inbound_payment(obj.get("amount_paid", 0), ref=obj["id"], account_ref=acc_ref, source="invoice")
    return {"status": "ignored", "type": etype}
