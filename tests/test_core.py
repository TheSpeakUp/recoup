import asyncio
import os

os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""

import yaml  # noqa: E402

from app import config  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.audit import AuditLog  # noqa: E402
from app.gateway import Gateway  # noqa: E402
from app.ledger import Ledger  # noqa: E402
from app.memory import LocalMemory  # noqa: E402
from app.payments import MockPayments  # noqa: E402
from app.policy import ALLOW, DENY, ESCALATE, PolicyEngine  # noqa: E402

SCEN = yaml.safe_load((config.BASE_DIR / "scenarios" / "collections.yaml").read_text())


def make(tmp_path):
    db = tmp_path / "t.db"
    audit, ledger = AuditLog(db), Ledger(db)
    mem = LocalMemory(tmp_path / "m.json")
    g = Gateway(payments=MockPayments(), ledger=ledger, audit=audit, policy=PolicyEngine(SCEN["policy"]),
                memory=mem, scenario=SCEN)
    return g, audit, ledger, mem


def test_policy_verdicts():
    p = PolicyEngine(SCEN["policy"])
    assert p.evaluate("offer_discount", {"percent": 5, "amount": 100}).verdict == ALLOW
    assert p.evaluate("offer_discount", {"percent": 10, "amount": 100}).verdict == DENY
    assert p.evaluate("create_instalment_plan", {"instalments": 6, "interval_days": 30}).verdict == DENY
    assert p.evaluate("charge_saved_card", {"amount": 300000}).verdict == ESCALATE
    assert p.evaluate("charge_saved_card", {"amount": 600000}).verdict == DENY
    d = p.evaluate("offer_discount", {"percent": 5, "amount": 40000}, outflow_today=20000, is_outflow=True)
    assert d.verdict == DENY and "Daily outflow" in d.reasons[0]


def test_audit_chain_detects_tamper(tmp_path):
    a = AuditLog(tmp_path / "a.db")
    for i in range(5):
        a.append(actor="agent", event="executed", tool="x", amount=i)
    assert a.verify()["ok"]
    a.conn.execute("UPDATE audit SET amount=999 WHERE seq=3")
    a.conn.commit()
    v = a.verify()
    assert not v["ok"] and v["broken_at_seq"] == 3


def test_ledger_balances(tmp_path):
    l = Ledger(tmp_path / "l.db")
    l.invoice_issued(1000, "r1")
    l.payment_received(400, "p1")
    b = l.balances()
    assert b["balanced"] and b["accounts"]["Accounts Receivable"] == 600


def test_gateway_denies_and_executes(tmp_path):
    g, audit, ledger, _ = make(tmp_path)
    out = g.execute("offer_discount", {"account_ref": "ACME", "invoice_number": "INV-1042", "percent": 10,
                                       "rationale": "test"}, session="s")
    assert out["status"] == "denied"
    out = g.execute("offer_discount", {"account_ref": "ACME", "invoice_number": "INV-1042", "percent": 5,
                                       "rationale": "test"}, session="s")
    assert out["status"] == "ok" and out["result"]["discount_amount"] == 24000
    assert ledger.balances()["balanced"]
    assert audit.verify()["ok"]


def test_escalation_and_human_approval(tmp_path):
    g, audit, ledger, _ = make(tmp_path)
    out = g.execute("charge_saved_card", {"account_ref": "ACME", "amount": 300000, "rationale": "t"}, session="s")
    assert out["status"] == "escalated_to_human"
    res = g.approve(out["approval_id"])
    assert res["status"] == "ok"
    assert ledger.balances()["accounts"]["Cash at Stripe"] == 300000
    assert audit.rows(limit=1)[0]["decision"] == "approved_by_human"


def test_mock_agent_negotiation(tmp_path):
    g, audit, ledger, mem = make(tmp_path)
    agent = Agent(g, mem, SCEN)

    async def run():
        s = agent.new_session("ACME")
        r1 = await agent.handle(s.id, "[open]")
        assert "INV-1042" in r1["reply"]
        r2 = await agent.handle(s.id, "Honestly cash is tight this month, can we split it?")
        assert "3 monthly payments" in r2["reply"]
        r3 = await agent.handle(s.id, "Yes, go ahead")
        assert "received" in r3["reply"]
        return s

    asyncio.run(run())
    events = [(r["tool"], r["event"]) for r in reversed(audit.rows())]
    assert ("create_instalment_plan", "deny") in events
    assert ("create_instalment_plan", "executed") in events
    assert ("charge_saved_card", "executed") in events
    assert ledger.balances()["balanced"]
    assert audit.verify()["ok"]


# ------------------------------------------------------------------ recovery scenario ----
REC = yaml.safe_load((config.BASE_DIR / "scenarios" / "recovery.yaml").read_text())


def make_rec(tmp_path):
    db = tmp_path / "r.db"
    audit, ledger = AuditLog(db), Ledger(db)
    mem = LocalMemory(tmp_path / "rm.json")
    g = Gateway(payments=MockPayments(), ledger=ledger, audit=audit, policy=PolicyEngine(REC["policy"]),
                memory=mem, scenario=REC)
    return g, audit, ledger, mem


def test_recovery_audit_and_financing(tmp_path):
    g, audit, ledger, mem = make_rec(tmp_path)
    agent = Agent(g, mem, REC)

    async def run():
        r = await agent.run_audit()
        assert "£1,832.00" in r["reply"]
        st = {f["id"]: f["status"] for f in g.findings.values()}
        assert st == {"F-ACME-1": "billed", "F-BLUEBIRD-1": "billed", "F-COBALT-1": "escalated",
                      "F-DELTA-1": "skipped", "F-ECHO-1": "blocked"}
        s = agent.new_session("BLUEBIRD")
        await agent.handle(s.id, "[open]")
        r = await agent.handle(s.id, "We can't pay now, cash is tight. Could we spread it over 12 months?")
        assert "6 months" in r["reply"]
        r = await agent.handle(s.id, "12 months through the partner then?")
        assert "isn't something I can approve" in r["reply"]
        r = await agent.handle(s.id, "OK, 6 months works")
        assert "has paid us today" in r["reply"]

    asyncio.run(run())
    events = [(r["tool"], r["event"]) for r in reversed(audit.rows())]
    assert ("create_instalment_plan", "deny") in events
    assert ("accept_financing", "deny") in events and ("accept_financing", "executed") in events
    b = ledger.balances()
    assert b["balanced"] and b["accounts"]["Cash at Stripe"] == 17460 and b["accounts"]["Financing Fees"] == 540
    assert audit.verify()["ok"]
    apr = next(iter(g.approvals))
    assert g.approve(apr)["status"] == "ok" and g.findings["F-COBALT-1"]["status"] == "billed"
