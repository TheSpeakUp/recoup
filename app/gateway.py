"""Action gateway: the ONLY path from the agent to money.

  LLM proposes tool call -> policy verdict -> (execute | deny | escalate) -> audit row -> ledger posting

Tools are registered with a JSON schema (shown to the LLM), a handler, and flags.
To adapt to a new track, add or edit tools here and limits in the scenario YAML.
"""
import itertools
import time
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .ledger import Ledger
from .policy import ALLOW, DENY, ESCALATE, PolicyEngine

RATIONALE = {"type": "string", "description": "One-sentence justification for the audit trail."}


@dataclass
class Tool:
    name: str
    description: str
    params: dict           # JSON-schema properties (rationale added automatically)
    required: list[str]
    handler: Callable[[dict], Any]
    moves_money: bool = False
    outflow: bool = False  # counts toward daily_outflow_limit
    derive: Callable[[dict], dict] | None = None      # computes policy inputs (amount, evidence, flags)
    on_blocked: Callable[[dict, Any], None] | None = None  # called on deny / escalate

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object",
                                 "properties": {**self.params, "rationale": RATIONALE},
                                 "required": [*self.required, "rationale"]}}


def _day_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


class Gateway:
    def __init__(self, *, payments, ledger: Ledger, audit: AuditLog, policy: PolicyEngine, memory, scenario: dict):
        self.pay = payments
        self.ledger = ledger
        self.audit = audit
        self.policy = policy
        self.memory = memory
        self.scenario = scenario
        self.tools: dict[str, Tool] = {}
        self.approvals: dict[str, dict] = {}
        self._appr_ids = itertools.count(1)
        self.accounts: dict[str, dict] = {}
        self._seed_accounts(scenario.get("accounts", []))
        self._register_default_tools()
        if scenario.get("recovery"):
            from .recovery import install
            install(self)

    # ---------------------------------------------------------------- state ----
    def _seed_accounts(self, accounts: list[dict]):
        for a in accounts:
            ref = a["ref"]
            self.accounts[ref] = {**a, "customer_id": None, "invoices": [dict(i) for i in a.get("invoices", [])],
                                  "payments": []}
            for inv in self.accounts[ref]["invoices"]:
                inv.setdefault("status", "open")
                inv["id"] = None
                self.ledger.invoice_issued(inv["amount"], ref=f"{ref}:{inv['number']}",
                                           memo=f"Invoice {inv['number']} to {a['name']}")

    def _ensure_provisioned(self, ref: str) -> dict:
        """Lazily mirror a seeded account into Stripe (or mock) the first time it is touched."""
        acc = self.accounts.get(ref)
        if not acc:
            raise KeyError(f"Unknown account '{ref}'. Known: {list(self.accounts)}")
        if not acc["customer_id"]:
            cust = self.pay.create_customer(acc["name"], acc["email"])
            acc["customer_id"] = cust["id"]
            for inv in acc["invoices"]:
                created = self.pay.create_invoice(cust["id"], inv["amount"], f"Invoice {inv['number']}", due_days=1)
                inv["id"] = created["id"]
                inv["url"] = created.get("hosted_invoice_url")
        return acc

    def _invoice(self, acc: dict, invoice_number: str) -> dict:
        for inv in acc["invoices"]:
            if inv["number"] == invoice_number or inv.get("id") == invoice_number:
                return inv
        raise KeyError(f"Invoice {invoice_number} not found for {acc['ref']}")

    def account_view(self, ref: str) -> dict:
        acc = self.accounts[ref]
        return {k: acc[k] for k in ("ref", "name", "email", "customer_id", "invoices", "payments", "notes") if k in acc}

    # -------------------------------------------------------------- execute ----
    def execute(self, name: str, args: dict, *, session: str, actor: str = "agent", force: bool = False) -> dict:
        tool = self.tools.get(name)
        if not tool:
            return {"status": "error", "error": f"Unknown tool {name}"}
        rationale = args.pop("rationale", None)
        decision_args = self._decision_args(name, args)
        outflow_today = self.audit.executed_amount_since(_day_start(), {t.name for t in self.tools.values() if t.outflow})
        decision = self.policy.evaluate(name, decision_args, outflow_today=outflow_today, is_outflow=tool.outflow)
        if decision_args.get("evidence") and decision.verdict != DENY:
            decision.reasons.append(f"Evidence: {decision_args['evidence']}")
        if decision.verdict != ALLOW and tool.on_blocked:
            tool.on_blocked(args, decision)

        if decision.verdict == DENY or (decision.verdict == ESCALATE and not force):
            approval_id = None
            if decision.verdict == ESCALATE:
                approval_id = f"apr_{next(self._appr_ids)}"
                self.approvals[approval_id] = {"id": approval_id, "tool": name, "args": {**args, "rationale": rationale},
                                               "session": session, "reasons": decision.reasons, "ts": time.time()}
            self.audit.append(actor=actor, event=decision.verdict, session=session, tool=name, args=args,
                              decision=decision.verdict, reasons=decision.reasons, rationale=rationale,
                              amount=decision_args.get("amount"), result={"approval_id": approval_id})
            out = {"status": "denied" if decision.verdict == DENY else "escalated_to_human",
                   "reasons": decision.reasons}
            if approval_id:
                out["approval_id"] = approval_id
                out["note"] = "Do not promise this outcome; tell the counterparty a colleague will confirm shortly."
            else:
                out["note"] = "Re-plan within policy; do not retry the same action."
            return out

        try:
            result = tool.handler(args)
        except Exception as e:  # surfaced to the LLM and logged, never swallowed
            self.audit.append(actor=actor, event="error", session=session, tool=name, args=args,
                              decision=decision.verdict, reasons=decision.reasons, rationale=rationale,
                              result={"error": str(e)})
            return {"status": "error", "error": str(e)}
        self.audit.append(actor=actor, event="executed", session=session, tool=name, args=args,
                          decision=ALLOW if not force else "approved_by_human", reasons=decision.reasons,
                          rationale=rationale, result=result,
                          amount=decision_args.get("amount") if tool.moves_money else None)
        return {"status": "ok", "result": result}

    def _decision_args(self, name: str, args: dict) -> dict:
        """Derive policy-relevant numbers (e.g. the money value of a % discount)."""
        d = dict(args)
        tool = self.tools.get(name)
        if tool and tool.derive:
            try:
                d.update(tool.derive(args))
            except Exception as e:
                d.update({"evidence_ok": False, "evidence": f"could not evaluate: {e}"})
            return d
        if name == "offer_discount":
            try:
                acc = self.accounts[args["account_ref"]]
                inv = self._invoice(acc, args["invoice_number"])
                d["amount"] = round(inv["amount"] * float(args["percent"]) / 100)
            except Exception:
                pass
        if name == "create_instalment_plan":
            try:
                acc = self.accounts[args["account_ref"]]
                d["amount"] = self._invoice(acc, args["invoice_number"])["amount"]
            except Exception:
                pass
        return d

    def approve(self, approval_id: str, approver: str = "human") -> dict:
        a = self.approvals.pop(approval_id, None)
        if not a:
            return {"status": "error", "error": "unknown approval"}
        return self.execute(a["tool"], dict(a["args"]), session=a["session"], actor=approver, force=True)

    def reject(self, approval_id: str, approver: str = "human") -> dict:
        a = self.approvals.pop(approval_id, None)
        if not a:
            return {"status": "error", "error": "unknown approval"}
        self.audit.append(actor=approver, event="rejected", session=a["session"], tool=a["tool"], args=a["args"],
                          decision="rejected_by_human", reasons=a["reasons"])
        return {"status": "rejected"}

    # --------------------------------------------------------------- tools -----
    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        allowed = set(self.scenario.get("tools", self.tools.keys()))
        return [t.schema() for n, t in self.tools.items() if n in allowed]

    def _register_default_tools(self):
        g = self

        def lookup_account(a):
            return g.account_view(g._ensure_provisioned(a["account_ref"])["ref"])

        def create_payment_link(a):
            acc = g._ensure_provisioned(a["account_ref"])
            inv = g._invoice(acc, a["invoice_number"])
            link = g.pay.create_payment_link(a["amount"], f"Payment for invoice {inv['number']}",
                                             {"account_ref": acc["ref"], "invoice_number": inv["number"]})
            inv.setdefault("links", []).append(link["url"])
            return link

        def offer_discount(a):
            acc = g._ensure_provisioned(a["account_ref"])
            inv = g._invoice(acc, a["invoice_number"])
            amount = round(inv["amount"] * float(a["percent"]) / 100)
            cn = g.pay.credit_note(inv["id"], amount, f"{a['percent']}% settlement discount")
            inv["amount"] -= amount
            g.ledger.discount_given(amount, ref=cn["id"], memo=f"{a['percent']}% discount on {inv['number']}")
            return {"credit_note": cn["id"], "discount_amount": amount, "new_amount_due": inv["amount"]}

        def create_instalment_plan(a):
            acc = g._ensure_provisioned(a["account_ref"])
            inv = g._invoice(acc, a["invoice_number"])
            if inv.get("id"):
                g.pay.void_invoice(inv["id"])
            plan = g.pay.create_instalment_plan(acc["customer_id"], inv["amount"], int(a["instalments"]),
                                                int(a["interval_days"]), f"Invoice {inv['number']} plan")
            inv["status"] = "rescheduled"
            inv["plan"] = plan["instalments"]
            return plan

        def charge_saved_card(a):
            acc = g._ensure_provisioned(a["account_ref"])
            pi = g.pay.charge_saved_card(acc["customer_id"], a["amount"], a.get("description", "Agreed payment"))
            if pi.get("status") == "succeeded":
                g.ledger.payment_received(a["amount"], ref=pi["id"], memo=f"Card charge {acc['name']}")
                acc["payments"].append({"id": pi["id"], "amount": a["amount"]})
            return pi

        def issue_refund(a):
            r = g.pay.refund(a["payment_intent_id"], a["amount"], a.get("reason", ""))
            g.ledger.refund_paid(a["amount"], ref=r["id"], memo=a.get("reason", "Refund"))
            return r

        def send_payout(a):
            t = g.pay.transfer(a["destination_account"], a["amount"], a.get("description", "Payout"))
            g.ledger.payout_sent(a["amount"], ref=t["id"], memo=a.get("description", "Payout"))
            return t

        def remember_fact(a):
            import asyncio
            if a["account_ref"] in g.accounts:
                g.accounts[a["account_ref"]].setdefault("learned", []).append(
                    f"{time.strftime('%Y-%m-%d')}: {a['fact']}")
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(g.memory.remember(a["account_ref"], a["fact"]))
            except RuntimeError:
                asyncio.run(g.memory.remember(a["account_ref"], a["fact"]))
            return {"remembered": a["fact"]}

        ref = {"account_ref": {"type": "string", "description": "Account reference, e.g. ACME"}}
        invn = {"invoice_number": {"type": "string", "description": "Invoice number, e.g. INV-1042"}}
        amt = {"amount": {"type": "integer", "description": "Amount in minor units (pence). £120.00 = 12000"}}

        self.register(Tool("lookup_account", "Read the counterparty's account, open invoices and history. No money moves.",
                           ref, ["account_ref"], lookup_account))
        self.register(Tool("create_payment_link", "Create a Stripe payment link for (part of) an invoice and share it.",
                           {**ref, **invn, **amt}, ["account_ref", "invoice_number", "amount"], create_payment_link))
        self.register(Tool("offer_discount", "Apply a % settlement discount to an invoice via a credit note.",
                           {**ref, **invn, "percent": {"type": "number"}}, ["account_ref", "invoice_number", "percent"],
                           offer_discount, moves_money=True, outflow=True))
        self.register(Tool("create_instalment_plan", "Replace an overdue invoice with N instalment invoices.",
                           {**ref, **invn, "instalments": {"type": "integer"}, "interval_days": {"type": "integer"}},
                           ["account_ref", "invoice_number", "instalments", "interval_days"], create_instalment_plan))
        self.register(Tool("charge_saved_card", "Charge the counterparty's saved card now (only with explicit consent).",
                           {**ref, **amt, "description": {"type": "string"}}, ["account_ref", "amount"],
                           charge_saved_card, moves_money=True))
        self.register(Tool("issue_refund", "Refund (part of) a payment.",
                           {"payment_intent_id": {"type": "string"}, **amt, "reason": {"type": "string"}},
                           ["payment_intent_id", "amount", "reason"], issue_refund, moves_money=True, outflow=True))
        self.register(Tool("send_payout", "Send money to a connected account (supplier, seller, contractor).",
                           {"destination_account": {"type": "string"}, **amt, "description": {"type": "string"}},
                           ["destination_account", "amount"], send_payout, moves_money=True, outflow=True))
        self.register(Tool("remember_fact", "Store a durable fact about a counterparty in agent memory.",
                           {**ref, "fact": {"type": "string"}}, ["account_ref", "fact"], remember_fact))

    # ------------------------------------------------------------ webhooks -----
    def record_inbound_payment(self, amount: int, ref: str, account_ref: str | None, source: str) -> dict:
        if any(e["ref"] == ref for e in self.ledger.entries(limit=1000)):
            return {"status": "duplicate"}
        self.ledger.payment_received(amount, ref=ref, memo=f"{source}: {account_ref or 'unknown'}")
        if account_ref in self.accounts:
            self.accounts[account_ref]["payments"].append({"id": ref, "amount": amount})
        self.audit.append(actor="stripe", event="money_in", tool=source, args={"account_ref": account_ref},
                          decision="observed", amount=amount, result={"ref": ref})
        return {"status": "posted"}
