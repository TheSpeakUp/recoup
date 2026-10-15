# Recoup

**Recoup finds revenue your company forgot to bill. It bills it in Stripe and gets it paid, inside a finance policy, and it can explain every pound.**

Briefcase x Stripe AI FinTech Hackathon, London, 15 October 2026. Track 02: Agents That Collect, plus the Cognee memory side quest.

## The problem

SaaS and usage-based companies lose revenue in boring ways. Usage overages never get billed. A launch discount keeps applying after it expires. Customers use more seats than they pay for. Nobody owns finding it, and when finance does find it, the money still has to be collected from a customer who did not budget for it.

## What Recoup does

1. **Finds the leak.** It compares three sources for every customer: contract terms (what we may bill), usage logs (what they consumed) and what Stripe actually invoiced. No human raises the bill. The trigger is the contract and the usage logs.
2. **Checks memory before billing.** Some commitments live outside the billing system, for example "sales promised Delta to waive overage until 30 November". Recoup recalls customer memory (Cognee) and will not bill against a written promise.
3. **Bills in Stripe through a policy gate.** Every bill passes finance clauses R1 to R5. The gate returns ALLOW, DENY or ESCALATE with the clause it relied on.
4. **Collects, and negotiates within policy.** It explains the bill with the clause and the records and sends a payment link. If cash is tight, it offers our own plan (at most 3 parts), or routes the invoice to a **financing partner**: the partner pays us today, and the customer repays the partner monthly at no extra cost to them. The fee we pay is capped by clause F1.
5. **Explains every pound.** Each finding shows the contract clause, the usage and billing records, the calculation, the memory consulted and every decision taken on it. Every action is written to a hash-chained audit log and posted to a double-entry ledger.

## The demo in numbers (seed data)

| Customer | Leak | Amount | Outcome |
|---|---|---|---|
| Acme Robotics | 1,240 API calls over the allowance, clause 4.2 | £62.00 | Billed in Stripe (R1) |
| Bluebird Coffee | 20% launch discount kept applying after 31 Aug, clause 3.1 | £180.00 | Billed, then financed: £174.60 received today |
| Cobalt Health | 6 extra seats for 2 months, clause 2.3 | £1,200.00 | Above £500, waits for a human (R2) |
| Delta Freight | 3,000 calls over the allowance | £150.00 | Skipped: written waiver in memory (R5) |
| Echo Media | 4,800 calls over the allowance | £240.00 | Blocked: open dispute (R3) |

The negotiation with Bluebird runs into two different guardrails before it settles:

- "Spread it over 12 months" → our own 12-part plan is **DENIED** (C2, at most 3 parts). Recoup asks the financing partners for offers.
- "12 months through the partner then" → **DENIED** (F1, the 5.5% fee is above our 4% cap).
- "6 months works" → **ALLOWED**. Northwind Capital pays £174.60 into Stripe now, the invoice is marked paid, and we pay a £5.40 fee. The customer repays the partner £30 a month.

## Architecture

```
contracts + usage logs + Stripe billing ─▶ scan_leakage ─▶ findings
                                                 │
LLM (Claude) proposes a tool call ─▶ policy gate ─┼▶ allow    ─▶ Stripe (test mode) ─▶ double-entry ledger
     ▲                                            ├▶ deny     ─▶ clause + reason back to the LLM, which re-plans
     │ customer memory (Cognee)                   └▶ escalate ─▶ human approval queue
     └──────────────────────────────── every step ─▶ hash-chained audit log (tamper-evident)
```

| File | What it does |
|---|---|
| `app/recovery.py` | Leakage scanner, bill and skip tools, explanations, partner financing |
| `app/policy.py` | Clause-based policy engine: limits, evidence requirements, dispute and waiver blocks, age limits, escalation |
| `app/gateway.py` | The only path to money: policy, then execute, then audit, then ledger. Holds the approval queue |
| `app/payments.py` | Stripe test mode (invoices, payment links, card charges, partner advances, out-of-band settlement), plus an offline mock |
| `app/audit.py` | Append-only SQLite log. Each row is `sha256(prev_hash + row)`. `verify()` pinpoints edits |
| `app/ledger.py` | Double-entry journal; debits must equal credits |
| `app/agent.py` | Claude tool-use loop for the audit and for customer conversations; scripted fallback for offline demos |
| `app/memory.py` | Customer memory in Cognee (`USE_COGNEE=1`) or local JSON |
| `scenarios/recovery.yaml` | Customers, contracts, usage, billing history, memory, finance clauses and financing partners |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # add STRIPE_SECRET_KEY=sk_test_... and ANTHROPIC_API_KEY; leave empty to run offline
uvicorn app.main:app --port 8000
# open http://localhost:8000, click "Run audit", then "Contact customer" for Bluebird Coffee
```

Live Stripe keys are refused. Tests: `pytest -q`.

## Honest notes

- The generic core (policy gate, audit chain, ledger, Stripe wrapper) was scaffolded earlier on the day of the event as a starter, which the rules allow. Recoup itself (the leakage scanner, clause-based policy, memory checks, partner financing, explanations and the UI) was built during the hackathon.
- The financing partners are simulated. Their advance is a real Stripe test-mode payment, and the invoice is settled out of band in Stripe.
- Customer data is synthetic.

Built by Nikita Zhuchkov (SpeakUp).
