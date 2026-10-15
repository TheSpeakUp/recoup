"""Agent loop. With ANTHROPIC_API_KEY set it uses Claude tool-use; otherwise a scripted
mock agent drives the same gateway so the demo and tests run fully offline."""
import json
import logging
import uuid

from . import config

log = logging.getLogger("agent")


class Session:
    def __init__(self, account_ref: str):
        self.id = f"s_{uuid.uuid4().hex[:8]}"
        self.account_ref = account_ref
        self.messages: list[dict] = []      # Anthropic-format history
        self.transcript: list[dict] = []    # what the UI shows
        self.state: dict = {}               # scratch space for the mock agent


class Agent:
    def __init__(self, gateway, memory, scenario: dict):
        self.g = gateway
        self.memory = memory
        self.scenario = scenario
        self.sessions: dict[str, Session] = {}
        self.client = None
        if config.ANTHROPIC_API_KEY:
            import anthropic
            self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def new_session(self, account_ref: str) -> Session:
        s = Session(account_ref)
        self.sessions[s.id] = s
        return s

    async def system_prompt(self, s: Session) -> str:
        facts = await self.memory.recall(s.account_ref, "payment behaviour")
        return (
            f"{self.scenario['agent']['role'].strip()}\n\n"
            f"You are talking to the counterparty for account '{s.account_ref}'. Call lookup_account first.\n\n"
            f"Goals:\n{self.scenario['agent']['goals'].strip()}\n\n"
            f"Hard policy limits (enforced by the gateway; requests outside them are DENIED or ESCALATED):\n"
            f"{self.g.policy.describe()}\n\n"
            f"Style:\n{self.scenario['agent'].get('style', '').strip()}\n\n"
            f"Memory about this counterparty:\n" + ("\n".join(f"- {f}" for f in facts) or "- none yet") + "\n\n"
            "Rules: every tool call needs a short 'rationale'. If a tool is denied, re-plan within the limits and "
            "never claim an action happened unless the tool returned status ok. Amounts are integers in pence. "
            "Reply to the counterparty in 1-3 short sentences and include any payment URL verbatim."
        )

    async def handle(self, session_id: str, text: str) -> dict:
        s = self.sessions[session_id]
        s.transcript.append({"role": "counterparty", "text": text})
        if self.client:
            reply = await self._llm_turn(s, text)
        elif self.scenario.get("recovery"):
            reply = await self._mock_recovery_turn(s, text)
        else:
            reply = await self._mock_turn(s, text)
        s.transcript.append({"role": "agent", "text": reply})
        return {"reply": reply, "transcript": s.transcript}

    # ------------------------------------------------------------- audit ----
    async def run_audit(self) -> dict:
        """One autonomous pass over every customer: find leakage, check memory, bill or skip."""
        s = Session("ALL")
        s.id = "audit_" + s.id[2:]
        self.sessions[s.id] = s
        s.transcript.append({"role": "system", "text": "Monthly revenue recovery audit started."})
        if self.client:
            system = (f"{self.scenario['agent']['role'].strip()}\n\nGoals:\n{self.scenario['agent']['goals'].strip()}\n\n"
                      f"Finance policy (enforced by the gateway):\n{self.g.policy.describe()}\n\n"
                      "Rules: every tool call needs a short 'rationale' that names the clause you rely on. If a bill "
                      "is denied or escalated, do not retry it. Amounts are integers in pence. Finish with a summary of "
                      "at most 5 lines: found, billed, skipped, waiting for a human, blocked.")
            reply = await self._llm_loop(s, "Run this month's revenue recovery audit now.", system, max_steps=30)
        else:
            reply = self._mock_audit(s)
        s.transcript.append({"role": "agent", "text": reply})
        return {"session_id": s.id, "reply": reply, "transcript": s.transcript}

    def _mock_audit(self, s: Session) -> str:
        found = self._call(s, "scan_leakage", rationale="Monthly audit: compare contracts, usage and billing.")
        fs = found["result"]["findings"]
        billed = skipped = esc = blocked = 0
        for f in fs:
            mem = self._call(s, "recall_customer_memory", account_ref=f["account_ref"],
                             rationale=f"R5: check written commitments to {f['customer']} before billing.")
            facts = mem["result"]["facts"]
            waiver = next((x for x in facts if "waive" in x.lower() and f["type"].split("_")[-1] in x.lower()), None)
            if waiver:
                self._call(s, "skip_finding", finding_id=f["id"],
                           reason=f"R5: written commitment in memory: {waiver}",
                           rationale=f"R5: memory holds a written waiver ({waiver.split(': ', 1)[0]}); billing would break that promise.")
                skipped += f["amount"]
                continue
            out = self._call(s, "bill_leakage", finding_id=f["id"],
                             rationale=f"R1: clause {f['clause']} and the records support {f['calculation']}.")
            if out["status"] == "ok":
                billed += f["amount"]
            elif out["status"] == "escalated_to_human":
                esc += f["amount"]
            else:
                blocked += f["amount"]
        total = found["result"]["total_found"]
        g = lambda p: f"£{p / 100:,.2f}"  # noqa: E731
        return (f"Audit done. Found {g(total)} that was never billed across {len({f['account_ref'] for f in fs})} customers. "
                f"Billed {g(billed)} in Stripe, {g(esc)} is waiting for your approval, "
                f"skipped {g(skipped)} because of a written waiver, blocked {g(blocked)} by policy.")

    async def _llm_loop(self, s: Session, text: str, system: str, max_steps: int = 8) -> str:
        s.messages.append({"role": "user", "content": text})
        for _ in range(max_steps):
            resp = self.client.messages.create(model=config.LLM_MODEL, max_tokens=2048, system=system,
                                               tools=self.g.schemas(), messages=s.messages)
            blocks = []
            for b in resp.content:
                if b.type == "text":
                    blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            s.messages.append({"role": "assistant", "content": blocks})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            results = []
            for tu in tool_uses:
                out = self.g.execute(tu.name, dict(tu.input), session=s.id)
                s.transcript.append({"role": "tool", "text": f"{tu.name} -> {out.get('status')}", "detail": out})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str)})
            s.messages.append({"role": "user", "content": results})
        return "Stopping here; a colleague will review the rest."

    # ------------------------------------------------------------ Claude ----
    async def _llm_turn(self, s: Session, text: str) -> str:
        s.messages.append({"role": "user", "content": text})
        system = await self.system_prompt(s)
        for _ in range(8):
            resp = self.client.messages.create(model=config.LLM_MODEL, max_tokens=1024, system=system,
                                               tools=self.g.schemas(), messages=s.messages)
            blocks = []
            for b in resp.content:
                if b.type == "text":
                    blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            s.messages.append({"role": "assistant", "content": blocks})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            results = []
            for tu in tool_uses:
                out = self.g.execute(tu.name, dict(tu.input), session=s.id)
                s.transcript.append({"role": "tool", "text": f"{tu.name} -> {out.get('status')}", "detail": out})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str)})
            s.messages.append({"role": "user", "content": results})
        return "Let me check with a colleague and come back to you shortly."

    # ------------------------------------------------------ mock recovery ----
    async def _mock_recovery_turn(self, s: Session, text: str) -> str:
        t = text.lower()
        ref = s.account_ref
        acc = self._call(s, "lookup_account", account_ref=ref, rationale="Load the corrective invoice first.")["result"]
        inv = next((i for i in acc["invoices"] if i.get("status") in ("open", "rescheduled")), None)
        if ("why" in t or "explain" in t) and acc["invoices"]:
            last = acc["invoices"][-1]
            ex = self._call(s, "explain_finding", finding_id=last["finding"], rationale="Customer asked why.")["result"]
            return (f"Contract {ex['contract']}, clause {ex['clause']}: \"{ex['clause_text']}\" Records: "
                    + "; ".join(ex["evidence"]) + f". Calculation: {ex['calculation']} = £{ex['amount'] / 100:,.2f}.")
        if not inv:
            return f"Hi {acc['name']}, nothing is outstanding on your account. Thank you."
        if not s.state.get("greeted"):
            s.state["greeted"] = True
            ex = self._call(s, "explain_finding", finding_id=inv["finding"],
                            rationale="Explain the bill with the clause and the records before asking for money.")["result"]
            link = self._call(s, "create_payment_link", account_ref=ref, invoice_number=inv["number"],
                              amount=inv["amount"], rationale="Give the simplest way to pay in full.")
            url = link.get("result", {}).get("url", "")
            return (f"Hi {acc['name']}, while reconciling we found we had under-billed you. Contract {ex['contract']}, "
                    f"clause {ex['clause']}: \"{ex['clause_text']}\" Our records: {'; '.join(ex['evidence'])}. "
                    f"That's £{inv['amount'] / 100:,.2f} ({ex['calculation']}), invoice {inv['number']}. "
                    f"You can pay here: {url}. If timing is difficult, tell me and we'll find a plan.")
        if "why" in t or "explain" in t:
            ex = self._call(s, "explain_finding", finding_id=inv["finding"], rationale="Customer asked why.")["result"]
            return (f"Contract {ex['contract']}, clause {ex['clause']}: \"{ex['clause_text']}\" Records: "
                    + "; ".join(ex["evidence"]) + f". Calculation: {ex['calculation']} = £{ex['amount'] / 100:,.2f}.")
        import re
        months = [int(m) for m in re.findall(r"\b(\d{1,2})\s*(?:months?|parts?|instal)", t)]
        offers = s.state.setdefault("offers", {})
        if offers and (months or "partner" in t):
            m = months[0] if months else max(offers)
            if m not in offers:
                o = self._call(s, "request_financing_offer", account_ref=ref, invoice_number=inv["number"], months=m,
                               rationale=f"Customer asked for {m} months; ask partners for an offer.")
                if o["status"] == "ok" and o["result"].get("approved"):
                    offers[m] = o["result"]
            if m in offers:
                o = offers[m]
                r = self._call(s, "accept_financing", offer_id=o["offer_id"],
                               rationale=f"Customer agreed to {m} months via {o['partner']}; F1 fee check.")
                if r["status"] == "ok":
                    res = r["result"]
                    self._call(s, "remember_fact", account_ref=ref,
                               fact=f"{inv['number']} financed by {o['partner']} over {m} months; customer repays the partner.",
                               rationale="Remember how this invoice was settled.")
                    return (f"Done. {o['partner']} has paid us today, so your invoice {inv['number']} is settled. "
                            f"You'll repay {o['partner']} {res['customer_schedule'].split(' to ')[0]}, at no extra cost "
                            f"to you. Thank you for sorting this so quickly.")
                ok = [k for k in sorted(offers) if offers[k]["fee_percent"] <= 4]
                if ok:
                    return (f"I'm afraid {m} months isn't something I can approve. The longest I can offer is "
                            f"{ok[-1]} months through {offers[ok[-1]]['partner']}, "
                            f"£{offers[ok[-1]]['customer_monthly'] / 100:,.2f} a month. Shall I set that up?")
            return "Let me check with a colleague and come back to you shortly."
        if any(k in t for k in ("can't", "cannot", "tight", "split", "spread")) and not s.state.get("plan_tried"):
            s.state["plan_tried"] = True
            want = months[0] if months else 6
            self._call(s, "create_instalment_plan", account_ref=ref, invoice_number=inv["number"], instalments=want,
                       interval_days=30, rationale=f"Customer asked for {want} monthly parts.")
            for m in sorted({want, 6}):
                o = self._call(s, "request_financing_offer", account_ref=ref, invoice_number=inv["number"], months=m,
                               rationale=f"C2 caps our own plans at 3 parts; ask partners to fund {m} months.")
                if o["status"] == "ok" and o["result"].get("approved"):
                    offers[m] = o["result"]
            best = offers.get(6)
            third = round(inv["amount"] / 3)
            msg = (f"I understand. We can carry at most 3 monthly payments ourselves (£{third / 100:,.2f} a month).")
            if best:
                msg += (f" Or our partner {best['partner']} can spread it over 6 months, "
                        f"£{best['customer_monthly'] / 100:,.2f} a month, at no extra cost to you.")
            return msg + " Which would you prefer?"
        if ("3" in t or "three" in t) and s.state.get("plan_tried") and not inv.get("plan"):
            plan = self._call(s, "create_instalment_plan", account_ref=ref, invoice_number=inv["number"], instalments=3,
                              interval_days=30, rationale="Customer chose our own 3-part plan (C2).")
            if plan["status"] == "ok":
                s.state["first_part"] = plan["result"]["instalments"][0]["amount"]
                return (f"Done: 3 monthly payments of about £{s.state['first_part'] / 100:,.2f}. "
                        "Shall I take the first one from your card on file now?")
        if any(k in t for k in ("friday", "next week", "promise", "will pay")):
            self._call(s, "remember_fact", account_ref=ref, fact=f"Promised: \"{text.strip()}\" about {inv['number']}.",
                       rationale="Remember the customer's promise for the follow-up.")
            return "Thank you, noted. I'll check in after that date if it hasn't arrived."
        return await self._mock_turn(s, text)

    # -------------------------------------------------------------- mock ----
    def _call(self, s: Session, name: str, **args) -> dict:
        out = self.g.execute(name, dict(args), session=s.id)
        s.transcript.append({"role": "tool", "text": f"{name} -> {out.get('status')}", "detail": out})
        return out

    async def _mock_turn(self, s: Session, text: str) -> str:
        t = text.lower()
        ref = s.account_ref
        acc = self._call(s, "lookup_account", account_ref=ref,
                         rationale="Load current balance before responding.")["result"]
        inv = next((i for i in acc["invoices"] if i.get("status") in ("open", "rescheduled")), None)
        if not inv:
            return "Thanks, your account is fully settled. Nothing more is due."
        pounds = inv["amount"] / 100
        if not s.state.get("greeted"):
            s.state["greeted"] = True
            link = self._call(s, "create_payment_link", account_ref=ref, invoice_number=inv["number"],
                              amount=inv["amount"], rationale="Offer the simplest way to settle in full.")
            url = link.get("result", {}).get("url", "")
            return (f"Hi {acc['name']}, invoice {inv['number']} for £{pounds:,.2f} is now overdue. "
                    f"You can settle it here: {url}. If timing is a problem, tell me and we'll find a plan.")
        if "discount" in t or "cheaper" in t:
            first = self._call(s, "offer_discount", account_ref=ref, invoice_number=inv["number"], percent=10,
                               rationale="Customer asked for a discount; try 10% to close today.")
            if first["status"] != "ok":
                second = self._call(s, "offer_discount", account_ref=ref, invoice_number=inv["number"], percent=5,
                                    rationale="10% denied by policy; offer the maximum allowed 5% for paying today.")
                if second["status"] != "ok":
                    return "I can't offer a discount on this invoice, but I can split it into instalments."
            link = self._call(s, "create_payment_link", account_ref=ref, invoice_number=inv["number"],
                              amount=inv["amount"], rationale="Discounted amount, payable today.")
            return (f"I can do 5% off if you pay today: £{inv['amount'] / 100:,.2f} instead of £{pounds:,.2f}. "
                    f"Link: {link.get('result', {}).get('url', '')}")
        if any(k in t for k in ("can't", "cannot", "no money", "tight", "later", "instal", "split")):
            plan = self._call(s, "create_instalment_plan", account_ref=ref, invoice_number=inv["number"],
                              instalments=6, interval_days=30, rationale="Cash-flow issue; try 6 monthly parts.")
            if plan["status"] != "ok":
                plan = self._call(s, "create_instalment_plan", account_ref=ref, invoice_number=inv["number"],
                                  instalments=3, interval_days=30,
                                  rationale="6 parts denied; policy max is 3 instalments.")
            if plan["status"] == "ok":
                parts = plan["result"]["instalments"]
                s.state["first_part"] = parts[0]["amount"]
                self._call(s, "remember_fact", account_ref=ref,
                           fact=f"Agreed {len(parts)}-part plan for {inv['number']} due to cash-flow pressure.",
                           rationale="Remember payment behaviour for future conversations.")
                return (f"No problem. I've split it into {len(parts)} monthly payments of about "
                        f"£{parts[0]['amount'] / 100:,.2f}. Shall I take the first one from your card on file now?")
            return "Let me check with a colleague and come back to you shortly."
        if any(k in t for k in ("yes", "ok", "go ahead", "charge", "pay now")):
            amount = s.state.get("first_part", inv["amount"])
            pi = self._call(s, "charge_saved_card", account_ref=ref, amount=amount,
                            description=f"Payment towards {inv['number']}",
                            rationale="Counterparty gave explicit consent to charge the saved card.")
            if pi["status"] == "ok":
                return f"Done, £{amount / 100:,.2f} received. Thank you, you'll get a receipt by email."
            if pi["status"] == "escalated_to_human":
                return "Thanks. A colleague will confirm this payment within the hour."
            return "That payment didn't go through. Could you try the payment link instead?"
        return "Understood. Would paying in full via the link, or splitting into instalments, work better for you?"
