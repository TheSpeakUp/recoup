"""Minimal double-entry ledger. Every money event the agent causes is posted as
balanced journal lines, so the books always reconcile against Stripe."""
import sqlite3
import threading
import time


class Ledger:
    def __init__(self, db_path):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                entry TEXT NOT NULL,
                account TEXT NOT NULL,
                debit INTEGER NOT NULL DEFAULT 0,
                credit INTEGER NOT NULL DEFAULT 0,
                memo TEXT,
                ref TEXT
            )"""
        )
        self.conn.commit()

    def post(self, lines: list[tuple[str, int, int]], memo: str = "", ref: str = "") -> str:
        """lines = [(account, debit, credit), ...]; must balance."""
        dr = sum(l[1] for l in lines)
        cr = sum(l[2] for l in lines)
        if dr != cr:
            raise ValueError(f"Unbalanced entry: debit {dr} != credit {cr}")
        with self._lock:
            entry = f"JE-{int(time.time() * 1000)}"
            ts = time.time()
            for acct, d, c in lines:
                self.conn.execute(
                    "INSERT INTO journal (ts,entry,account,debit,credit,memo,ref) VALUES (?,?,?,?,?,?,?)",
                    (ts, entry, acct, d, c, memo, ref))
            self.conn.commit()
            return entry

    def entries(self, limit: int = 200) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id,ts,entry,account,debit,credit,memo,ref FROM journal ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(zip(("id", "ts", "entry", "account", "debit", "credit", "memo", "ref"), r)) for r in cur]

    def balances(self) -> dict:
        cur = self.conn.execute("SELECT account, SUM(debit)-SUM(credit) FROM journal GROUP BY account ORDER BY account")
        bal = {a: int(v) for a, v in cur}
        return {"accounts": bal, "balanced": sum(bal.values()) == 0}

    # Convenience postings used by the gateway / webhooks ---------------------------
    def invoice_issued(self, amount: int, ref: str, memo: str = "Invoice issued"):
        return self.post([("Accounts Receivable", amount, 0), ("Revenue", 0, amount)], memo, ref)

    def discount_given(self, amount: int, ref: str, memo: str = "Settlement discount"):
        return self.post([("Discounts Allowed", amount, 0), ("Accounts Receivable", 0, amount)], memo, ref)

    def payment_received(self, amount: int, ref: str, memo: str = "Payment received via Stripe"):
        return self.post([("Cash at Stripe", amount, 0), ("Accounts Receivable", 0, amount)], memo, ref)

    def refund_paid(self, amount: int, ref: str, memo: str = "Refund issued"):
        return self.post([("Refunds", amount, 0), ("Cash at Stripe", 0, amount)], memo, ref)

    def payout_sent(self, amount: int, ref: str, memo: str = "Payout / transfer"):
        return self.post([("Accounts Payable", amount, 0), ("Cash at Stripe", 0, amount)], memo, ref)
