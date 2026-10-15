"""Money-movement backends. `StripePayments` uses the real Stripe API (test mode);
`MockPayments` returns realistic fake objects so the whole loop runs offline.
All amounts are integers in minor units (pence/cents)."""
import itertools
import time
from typing import Any

from . import config


class MockPayments:
    name = "mock"

    def __init__(self):
        self._ids = itertools.count(1)
        self.objects: dict[str, dict] = {}

    def _id(self, prefix: str) -> str:
        return f"{prefix}_mock{next(self._ids):04d}"

    def _save(self, obj: dict) -> dict:
        self.objects[obj["id"]] = obj
        return obj

    def create_customer(self, name: str, email: str) -> dict:
        return self._save({"id": self._id("cus"), "name": name, "email": email, "default_payment_method": "pm_card_visa"})

    def create_invoice(self, customer_id: str, amount: int, description: str, due_days: int = 14) -> dict:
        iid = self._id("in")
        return self._save({"id": iid, "customer": customer_id, "amount_due": amount, "status": "open",
                           "description": description, "due_date": int(time.time()) + due_days * 86400,
                           "hosted_invoice_url": f"https://invoice.stripe.com/mock/{iid}"})

    def create_payment_link(self, amount: int, description: str, metadata: dict | None = None) -> dict:
        pid = self._id("plink")
        return self._save({"id": pid, "url": f"https://buy.stripe.com/test_mock/{pid}", "amount": amount,
                           "description": description, "metadata": metadata or {}})

    def create_instalment_plan(self, customer_id: str, total: int, instalments: int, interval_days: int,
                               description: str) -> dict:
        base = total // instalments
        parts = [base] * instalments
        parts[-1] += total - base * instalments
        invs = [self.create_invoice(customer_id, p, f"{description} ({i + 1}/{instalments})", interval_days * i + 1)
                for i, p in enumerate(parts)]
        return {"instalments": [{"invoice": i["id"], "amount": i["amount_due"], "url": i["hosted_invoice_url"],
                                 "due_date": i["due_date"]} for i in invs]}

    def void_invoice(self, invoice_id: str) -> dict:
        inv = self.objects.get(invoice_id, {"id": invoice_id})
        inv["status"] = "void"
        return {"id": invoice_id, "status": "void"}

    def credit_note(self, invoice_id: str, amount: int, reason: str) -> dict:
        inv = self.objects.get(invoice_id)
        if inv:
            inv["amount_due"] = max(0, inv["amount_due"] - amount)
        return self._save({"id": self._id("cn"), "invoice": invoice_id, "amount": amount, "reason": reason})

    def charge_saved_card(self, customer_id: str, amount: int, description: str) -> dict:
        return self._save({"id": self._id("pi"), "customer": customer_id, "amount": amount, "status": "succeeded",
                           "description": description})

    def refund(self, payment_intent_id: str, amount: int, reason: str) -> dict:
        return self._save({"id": self._id("re"), "payment_intent": payment_intent_id, "amount": amount,
                           "status": "succeeded", "reason": reason})

    def transfer(self, destination_account: str, amount: int, description: str) -> dict:
        return self._save({"id": self._id("tr"), "destination": destination_account, "amount": amount,
                           "description": description})

    # --- customer balance (support / netting) ---------------------------------
    def credit_balance(self, customer_id: str, amount: int, description: str) -> dict:
        c = self.objects.setdefault(customer_id, {"id": customer_id})
        c["balance"] = c.get("balance", 0) - amount
        return self._save({"id": self._id("cbtxn"), "customer": customer_id, "amount": -amount,
                           "ending_balance": c["balance"], "description": description})

    def debit_balance(self, customer_id: str, amount: int, description: str) -> dict:
        c = self.objects.setdefault(customer_id, {"id": customer_id})
        c["balance"] = c.get("balance", 0) + amount
        return self._save({"id": self._id("cbtxn"), "customer": customer_id, "amount": amount,
                           "ending_balance": c["balance"], "description": description})

    def balance(self, customer_id: str) -> int:
        return self.objects.get(customer_id, {}).get("balance", 0)

    def bill_now(self, customer_id: str, amount: int, description: str) -> dict:
        """Invoice and collect immediately. Credit balance is applied first, the card covers the rest."""
        c = self.objects.setdefault(customer_id, {"id": customer_id})
        credit = max(0, -c.get("balance", 0))
        from_balance = min(credit, amount)
        c["balance"] = c.get("balance", 0) + from_balance
        iid = self._id("in")
        return self._save({"id": iid, "customer": customer_id, "total": amount, "paid_from_balance": from_balance,
                           "paid_from_card": amount - from_balance, "status": "paid", "description": description,
                           "hosted_invoice_url": f"https://invoice.stripe.com/mock/{iid}"})


    # --- partner financing ------------------------------------------------------
    def receive_partner_advance(self, partner: str, amount: int, description: str, metadata: dict) -> dict:
        return self._save({"id": self._id("pi"), "payer": partner, "amount": amount, "status": "succeeded",
                           "description": description, "metadata": metadata})

    def mark_invoice_paid(self, invoice_id: str, note: str) -> dict:
        inv = self.objects.get(invoice_id, {"id": invoice_id})
        inv["status"] = "paid"
        return {"id": invoice_id, "status": "paid", "note": note}


class StripePayments:
    name = "stripe"

    def __init__(self, api_key: str):
        import stripe
        self.client = stripe.StripeClient(api_key)
        self.cur = config.CURRENCY

    @staticmethod
    def _d(obj: Any) -> dict:
        return obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)

    def create_customer(self, name: str, email: str) -> dict:
        c = self.client.v1.customers.create(params={"name": name, "email": email})
        # attach Stripe's test card so off-session charges work in test mode
        pm = self.client.v1.payment_methods.attach("pm_card_visa", params={"customer": c.id})
        self.client.v1.customers.update(c.id, params={"invoice_settings": {"default_payment_method": pm.id}})
        return {"id": c.id, "name": name, "email": email, "default_payment_method": pm.id}

    def create_invoice(self, customer_id: str, amount: int, description: str, due_days: int = 14) -> dict:
        inv = self.client.v1.invoices.create(params={
            "customer": customer_id, "collection_method": "send_invoice", "days_until_due": max(1, due_days),
            "currency": self.cur, "description": description, "pending_invoice_items_behavior": "exclude"})
        self.client.v1.invoice_items.create(params={
            "customer": customer_id, "invoice": inv.id, "amount": amount, "currency": self.cur,
            "description": description})
        inv = self.client.v1.invoices.finalize_invoice(inv.id)
        return {"id": inv.id, "customer": customer_id, "amount_due": inv.amount_due, "status": inv.status,
                "hosted_invoice_url": inv.hosted_invoice_url, "due_date": inv.due_date}

    def create_payment_link(self, amount: int, description: str, metadata: dict | None = None) -> dict:
        price = self.client.v1.prices.create(params={
            "unit_amount": amount, "currency": self.cur, "product_data": {"name": description[:250]}})
        link = self.client.v1.payment_links.create(params={
            "line_items": [{"price": price.id, "quantity": 1}], "metadata": metadata or {},
            "payment_intent_data": {"metadata": metadata or {}}})
        return {"id": link.id, "url": link.url, "amount": amount, "description": description}

    def create_instalment_plan(self, customer_id: str, total: int, instalments: int, interval_days: int,
                               description: str) -> dict:
        base = total // instalments
        parts = [base] * instalments
        parts[-1] += total - base * instalments
        out = []
        for i, p in enumerate(parts):
            inv = self.create_invoice(customer_id, p, f"{description} ({i + 1}/{instalments})",
                                      due_days=interval_days * i + 1)
            out.append({"invoice": inv["id"], "amount": p, "url": inv["hosted_invoice_url"], "due_date": inv["due_date"]})
        return {"instalments": out}

    def void_invoice(self, invoice_id: str) -> dict:
        inv = self.client.v1.invoices.void_invoice(invoice_id)
        return {"id": inv.id, "status": inv.status}

    def credit_note(self, invoice_id: str, amount: int, reason: str) -> dict:
        cn = self.client.v1.credit_notes.create(params={
            "invoice": invoice_id, "lines": [{"type": "custom_line_item", "unit_amount": amount, "quantity": 1,
                                              "description": reason[:250]}]})
        return {"id": cn.id, "invoice": invoice_id, "amount": cn.amount, "reason": reason}

    def charge_saved_card(self, customer_id: str, amount: int, description: str) -> dict:
        cust = self.client.v1.customers.retrieve(customer_id)
        pm = cust.invoice_settings.default_payment_method if cust.invoice_settings else None
        pi = self.client.v1.payment_intents.create(params={
            "amount": amount, "currency": self.cur, "customer": customer_id, "payment_method": pm,
            "off_session": True, "confirm": True, "description": description})
        return {"id": pi.id, "customer": customer_id, "amount": pi.amount, "status": pi.status, "description": description}

    def refund(self, payment_intent_id: str, amount: int, reason: str) -> dict:
        r = self.client.v1.refunds.create(params={
            "payment_intent": payment_intent_id, "amount": amount, "metadata": {"agent_reason": reason[:450]}})
        return {"id": r.id, "payment_intent": payment_intent_id, "amount": r.amount, "status": r.status, "reason": reason}

    def transfer(self, destination_account: str, amount: int, description: str) -> dict:
        t = self.client.v1.transfers.create(params={
            "amount": amount, "currency": self.cur, "destination": destination_account, "description": description})
        return {"id": t.id, "destination": destination_account, "amount": t.amount, "description": description}

    # --- customer balance (support / netting) ---------------------------------
    def credit_balance(self, customer_id: str, amount: int, description: str) -> dict:
        t = self.client.v1.customers.balance_transactions.create(customer_id, params={
            "amount": -amount, "currency": self.cur, "description": description[:350]})
        return {"id": t.id, "customer": customer_id, "amount": t.amount, "ending_balance": t.ending_balance,
                "description": description}

    def debit_balance(self, customer_id: str, amount: int, description: str) -> dict:
        t = self.client.v1.customers.balance_transactions.create(customer_id, params={
            "amount": amount, "currency": self.cur, "description": description[:350]})
        return {"id": t.id, "customer": customer_id, "amount": t.amount, "ending_balance": t.ending_balance,
                "description": description}

    def balance(self, customer_id: str) -> int:
        return self.client.v1.customers.retrieve(customer_id).balance or 0

    def bill_now(self, customer_id: str, amount: int, description: str) -> dict:
        """Invoice and collect immediately. Stripe applies the credit balance first, the card covers the rest."""
        inv = self.client.v1.invoices.create(params={
            "customer": customer_id, "collection_method": "charge_automatically", "currency": self.cur,
            "description": description, "pending_invoice_items_behavior": "exclude", "auto_advance": False})
        self.client.v1.invoice_items.create(params={
            "customer": customer_id, "invoice": inv.id, "amount": amount, "currency": self.cur,
            "description": description})
        inv = self.client.v1.invoices.finalize_invoice(inv.id, params={"auto_advance": False})
        total, due = inv.total, inv.amount_due          # credit balance is applied at finalization
        if inv.status == "open" and due > 0:
            inv = self.client.v1.invoices.pay(inv.id)
        from_card = due
        return {"id": inv.id, "customer": customer_id, "total": total, "paid_from_balance": total - due,
                "paid_from_card": from_card, "status": inv.status, "description": description,
                "hosted_invoice_url": inv.hosted_invoice_url}


    # --- partner financing ------------------------------------------------------
    def receive_partner_advance(self, partner: str, amount: int, description: str, metadata: dict) -> dict:
        """The financing partner pays us now. In test mode the partner is a Stripe customer with a test card."""
        if not getattr(self, "_partner_id", None):
            c = self.create_customer(partner, "funding@partner.test")
            self._partner_id = c["id"]
        pi = self.client.v1.payment_intents.create(params={
            "amount": amount, "currency": self.cur, "customer": self._partner_id,
            "payment_method": "pm_card_visa", "confirm": True, "off_session": True,
            "description": description[:300], "metadata": {k: str(v)[:450] for k, v in metadata.items()}})
        return {"id": pi.id, "payer": partner, "amount": pi.amount, "status": pi.status, "description": description}

    def mark_invoice_paid(self, invoice_id: str, note: str) -> dict:
        inv = self.client.v1.invoices.pay(invoice_id, params={"paid_out_of_band": True})
        return {"id": inv.id, "status": inv.status, "note": note}


def build_payments():
    if config.STRIPE_SECRET_KEY:
        if not config.STRIPE_SECRET_KEY.startswith(("sk_test", "rk_test")):
            raise RuntimeError("Refusing to run with a live Stripe key. Use a test key (sk_test_...).")
        return StripePayments(config.STRIPE_SECRET_KEY)
    return MockPayments()
