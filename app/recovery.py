"""Revenue recovery: find revenue that contracts entitle us to but that was never billed.

The scanner is deterministic on purpose. It compares three sources for every customer:
  contract terms  (what we may bill)
  usage records   (what the customer actually consumed)
  billed items    (what Stripe actually invoiced)
and emits findings with the clause and records that prove each pound. The agent then decides,
per finding, whether to bill it, skip it or leave it to a human, and the policy gate checks
every bill against the finance clauses (R1-R5).
"""
from datetime import date

from .gateway import Tool


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _period_start(p: str) -> date:
    return _d(p + "-01")


def scan(accounts: dict, today: date) -> list[dict]:
    findings = []
    for ref, a in accounts.items():
        c = a.get("contract") or {}
        terms = c.get("terms") or {}
        clauses = c.get("clauses") or {}
        billed = {b["period"]: b for b in a.get("billed", [])}
        n = 0

        def add(kind, amount, periods, clause_no, evidence, calc):
            nonlocal n
            n += 1
            findings.append({
                "id": f"F-{ref}-{n}", "account_ref": ref, "customer": a["name"], "type": kind, "amount": amount,
                "periods": periods, "contract": c.get("id"), "clause": clause_no,
                "clause_text": clauses.get(clause_no, ""), "evidence": evidence, "calculation": calc,
                "age_days": max((today - _period_start(p)).days for p in periods), "status": "found"})

        # 1) usage above the allowance that was never billed
        if "included_calls" in terms:
            for u in a.get("usage", []):
                over = u["api_calls"] - terms["included_calls"]
                billed_over = sum(i["amount"] for i in billed.get(u["period"], {}).get("items", [])
                                  if "overage" in i["desc"].lower())
                due = over * terms["overage_per_call"] - billed_over
                if over > 0 and due > 0:
                    add("unbilled_overage", due, [u["period"]], terms["overage_clause"],
                        [f"{u['source']}: {u['api_calls']:,} API calls in {u['period']}",
                         f"{billed.get(u['period'], {}).get('invoice', 'no invoice')}: no overage line billed"],
                        f"({u['api_calls']:,} - {terms['included_calls']:,}) calls x £{terms['overage_per_call'] / 100:.2f}")

        # 2) a discount that kept being applied after it expired
        if "discount_until" in terms:
            until = _d(terms["discount_until"])
            leaked, periods, invs = 0, [], []
            for p, b in sorted(billed.items()):
                if _period_start(p) > until:
                    disc = -sum(i["amount"] for i in b["items"] if i["amount"] < 0 and "discount" in i["desc"].lower())
                    if disc > 0:
                        leaked += disc
                        periods.append(p)
                        invs.append(f"{b['invoice']} ({p}): discount line -£{disc / 100:.2f}")
            if leaked:
                add("expired_discount", leaked, periods, terms["discount_clause"],
                    [f"Contract {c.get('id')} clause {terms['discount_clause']}: discount ended {until.isoformat()}", *invs],
                    f"{len(periods)} months x {terms['discount_percent']}% of £{terms['plan_fee'] / 100:.2f}")

        # 3) more active seats than the contract includes
        if "seats_included" in terms:
            extra_total, periods, ev = 0, [], []
            for u in a.get("usage", []):
                extra = u["seats"] - terms["seats_included"]
                if extra > 0:
                    extra_total += extra
                    periods.append(u["period"])
                    ev.append(f"{u['source']}: {u['seats']} active seats in {u['period']}")
            if extra_total:
                add("extra_seats", extra_total * terms["seat_price"], periods, terms["seat_clause"],
                    [*ev, "Invoices for those months bill 10 seats only"],
                    f"{extra_total} extra seat-months x £{terms['seat_price'] / 100:.2f}")
    return findings


def install(g):
    """Register the recovery tools on a Gateway."""
    today = _d(g.scenario.get("today", date.today().isoformat()))
    g.findings = {}

    def ensure_scanned():
        if not g.findings:
            for f in scan(g.accounts, today):
                g.findings[f["id"]] = f
        return g.findings

    def memory_facts(ref):
        return list(g.accounts[ref].get("memory", [])) + list(g.accounts[ref].get("learned", []))

    def waiver_for(f):
        a = g.accounts[f["account_ref"]]
        for w in a.get("waivers", []):
            if w["type"] in f["type"] and _d(w["until"]) >= today:
                return memory_facts(f["account_ref"])[w.get("memory_index", 0)]
        return None

    def open_dispute(ref):
        return next((d for d in g.accounts[ref].get("disputes", []) if d.get("status") == "open"), None)

    # ---- handlers -----------------------------------------------------------------
    def scan_leakage(a):
        fs = ensure_scanned()
        return {"findings": [{k: f[k] for k in ("id", "customer", "account_ref", "type", "amount", "calculation",
                                                 "clause", "clause_text", "evidence", "status")} for f in fs.values()],
                "total_found": sum(f["amount"] for f in fs.values())}

    def recall_customer_memory(a):
        ref = a["account_ref"]
        facts = memory_facts(ref)
        out = {"account_ref": ref, "source": g.memory.name, "facts": facts}
        if g.memory.name == "cognee":  # ask the knowledge graph too, in its own event loop
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            try:
                with ThreadPoolExecutor(1) as ex:
                    out["graph_answer"] = ex.submit(lambda: asyncio.run(g.memory.recall(
                        ref, "waivers, promises, disputes"))).result(timeout=25)
            except Exception as e:
                out["graph_error"] = str(e)
        return out

    def bill_leakage(a):
        f = ensure_scanned()[a["finding_id"]]
        acc = g._ensure_provisioned(f["account_ref"])
        desc = (f"Corrective invoice: {f['type'].replace('_', ' ')} {', '.join(f['periods'])} "
                f"(contract {f['contract']} cl. {f['clause']})")
        inv = g.pay.create_invoice(acc["customer_id"], f["amount"], desc, due_days=14)
        number = f"REC-{f['account_ref']}-{len(acc['invoices']) + 1}"
        acc["invoices"].append({"number": number, "id": inv["id"], "amount": f["amount"], "status": "open",
                                "url": inv.get("hosted_invoice_url"), "finding": f["id"], "description": desc})
        g.ledger.invoice_issued(f["amount"], ref=inv["id"], memo=f"Recovered: {f['customer']} {f['type']}")
        f.update(status="billed", invoice=number, stripe_invoice=inv["id"], url=inv.get("hosted_invoice_url"))
        return {"invoice_number": number, "stripe_invoice": inv["id"], "amount": f["amount"],
                "hosted_invoice_url": inv.get("hosted_invoice_url")}

    def skip_finding(a):
        f = ensure_scanned()[a["finding_id"]]
        f.update(status="skipped", skip_reason=a["reason"])
        return {"finding_id": f["id"], "status": "skipped", "reason": a["reason"]}

    def explain_finding(a):
        f = ensure_scanned()[a["finding_id"]]
        trail = [{"seq": r["seq"], "event": r["event"], "tool": r["tool"], "reasons": r["reasons"],
                  "rationale": r["rationale"], "actor": r["actor"]}
                 for r in reversed(g.audit.rows(limit=500))
                 if (r["args"] or {}).get("finding_id") == f["id"]]
        return {**f, "memory": memory_facts(f["account_ref"]), "decisions": trail}

    # ---- policy inputs ------------------------------------------------------------
    def derive_bill(a):
        f = ensure_scanned().get(a.get("finding_id"))
        if not f:
            return {"evidence_ok": False, "evidence": f"finding {a.get('finding_id')} does not exist"}
        d = {"amount": f["amount"], "age_days": f["age_days"],
              "evidence_ok": bool(f["clause_text"]) and bool(f["evidence"]) and f["status"] in ("found", "escalated", "blocked"),
             "evidence": f"contract {f['contract']} cl. {f['clause']} ({f['clause_text']}); "
                         + "; ".join(f["evidence"]) + f"; calc: {f['calculation']}"}
        if f["status"] == "billed":
            d["evidence"] = "already billed"
        disp = open_dispute(f["account_ref"])
        if disp:
            d["dispute_open"] = True
            d["dispute_open_detail"] = f"{disp['invoice']} disputed since {disp['opened']}: {disp['reason']}"
        w = waiver_for(f)
        if w:
            d["waived_in_memory"] = True
            d["waived_in_memory_detail"] = f'"{w}"'
        return d

    fid = {"finding_id": {"type": "string", "description": "Finding id from scan_leakage, e.g. F-ACME-1"}}
    ref = {"account_ref": {"type": "string", "description": "Customer reference, e.g. ACME"}}
    g.register(Tool("scan_leakage", "Compare contracts, usage logs and Stripe billing for all customers and list "
                    "revenue that was never billed. No money moves.", {}, [], scan_leakage))
    g.register(Tool("recall_customer_memory", "Recall what we know about a customer outside the billing system: "
                    "emails, waivers, promises, disputes, payment behaviour.", ref, ["account_ref"],
                    recall_customer_memory))
    def blocked(a, decision):
        f = g.findings.get(a.get("finding_id"))
        if f and f["status"] == "found":
            f["status"] = "escalated" if decision.verdict == "escalate" else "blocked"
            f["block_reasons"] = decision.reasons

    g.register(Tool("bill_leakage", "Bill one finding in Stripe as a corrective invoice.", fid, ["finding_id"],
                    bill_leakage, moves_money=True, derive=derive_bill, on_blocked=blocked))
    g.register(Tool("skip_finding", "Decide not to bill a finding, with the reason (cite memory or clause).",
                    {**fid, "reason": {"type": "string"}}, ["finding_id", "reason"], skip_finding))
    g.register(Tool("explain_finding", "Explain a finding: clause, records, calculation, memory and every "
                    "decision taken on it. No money moves.", fid, ["finding_id"], explain_finding))

    # ---- partner financing: the customer gets months, we get paid today --------------------------
    partners = {p["name"]: p for p in g.scenario.get("financing_partners", [])}
    g.offers = {}

    def request_financing_offer(a):
        acc = g._ensure_provisioned(a["account_ref"])
        inv = g._invoice(acc, a["invoice_number"])
        months = int(a["months"])
        disp = open_dispute(acc["ref"])
        best = None
        for p in partners.values():
            fee_pct = p["fee_percent_by_months"].get(months) or p["fee_percent_by_months"].get(str(months))
            if fee_pct is None or inv["amount"] > p["max_amount"] or disp:
                continue
            if not best or fee_pct < best[1]:
                best = (p, fee_pct)
        if not best:
            return {"approved": False, "reason": "No partner offers this term for this customer."}
        p, fee_pct = best
        fee = round(inv["amount"] * fee_pct / 100)
        oid = f"OFF-{len(g.offers) + 1}"
        offer = {"offer_id": oid, "partner": p["name"], "account_ref": acc["ref"], "invoice_number": inv["number"],
                 "months": months, "fee_percent": fee_pct, "fee": fee, "advance": inv["amount"] - fee,
                 "customer_monthly": round(inv["amount"] / months), "customer_pays_extra": 0,
                 "approved": True, "underwriting": p.get("underwriting", "Instant decision on company data.")}
        g.offers[oid] = offer
        return offer

    def accept_financing(a):
        o = g.offers[a["offer_id"]]
        acc = g._ensure_provisioned(o["account_ref"])
        inv = g._invoice(acc, o["invoice_number"])
        pi = g.pay.receive_partner_advance(o["partner"], o["advance"],
                                           f"Advance for {inv['number']} ({o['months']} months, fee {o['fee_percent']}%)",
                                           {"invoice": inv["number"], "account_ref": acc["ref"], "offer": o["offer_id"]})
        if inv.get("id"):
            g.pay.mark_invoice_paid(inv["id"], f"Financed by {o['partner']}")
        g.ledger.post([("Cash at Stripe", o["advance"], 0), ("Financing Fees", o["fee"], 0),
                       ("Accounts Receivable", 0, inv["amount"])],
                      memo=f"{o['partner']} advance for {inv['number']}", ref=pi["id"])
        inv["status"] = "financed"
        acc["payments"].append({"id": pi["id"], "amount": o["advance"], "via": o["partner"]})
        o["status"] = "accepted"
        f = g.findings.get(inv.get("finding"))
        if f:
            f["status"] = "paid"
        return {"advance_payment": pi["id"], "received_now": o["advance"], "fee": o["fee"], "partner": o["partner"],
                "customer_schedule": f"{o['months']} x £{o['customer_monthly'] / 100:,.2f} to {o['partner']}"}

    def derive_financing(a):
        o = g.offers.get(a.get("offer_id"))
        if not o:
            return {"evidence_ok": False, "evidence": "no such offer"}
        return {"amount": o["fee"] + o["advance"], "percent": o["fee_percent"], "partner": o["partner"],
                "evidence_ok": o.get("approved", False),
                "evidence": f"{o['partner']} offer {o['offer_id']}: {o['months']} months, fee {o['fee_percent']}% "
                            f"(£{o['fee'] / 100:,.2f}) paid by us, we receive £{o['advance'] / 100:,.2f} today"}

    if partners:
        inv_args = {"account_ref": {"type": "string"}, "invoice_number": {"type": "string"}}
        g.register(Tool("request_financing_offer", "Ask our financing partners to fund an invoice: the partner pays us "
                        "now, the customer repays the partner monthly. Returns an offer; no money moves.",
                        {**inv_args, "months": {"type": "integer"}}, ["account_ref", "invoice_number", "months"],
                        request_financing_offer))
        g.register(Tool("accept_financing", "Accept a financing offer the customer agreed to. The partner's advance "
                        "lands in Stripe now and the invoice is marked paid.", {"offer_id": {"type": "string"}},
                        ["offer_id"], accept_financing, moves_money=True, derive=derive_financing))
    return g
