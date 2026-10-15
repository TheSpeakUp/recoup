"""Append-only, hash-chained audit log.

Every agent decision (proposed action, policy verdict, execution result, rationale)
is one row. Each row stores sha256(prev_hash + canonical_json(row)), so editing or
deleting any past row breaks the chain and `verify()` reports exactly where.
"""
import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, db_path):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                session TEXT,
                actor TEXT NOT NULL,
                event TEXT NOT NULL,
                tool TEXT,
                args TEXT,
                decision TEXT,
                reasons TEXT,
                rationale TEXT,
                result TEXT,
                amount INTEGER,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def _last_hash(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    @staticmethod
    def _digest(prev_hash: str, body: dict) -> str:
        return hashlib.sha256((prev_hash + _canon(body)).encode()).hexdigest()

    def append(self, *, actor: str, event: str, session: str | None = None, tool: str | None = None,
               args: dict | None = None, decision: str | None = None, reasons: list | None = None,
               rationale: str | None = None, result: Any = None, amount: int | None = None) -> dict:
        with self._lock:
            body = {
                "ts": round(time.time(), 3), "session": session, "actor": actor, "event": event,
                "tool": tool, "args": args or {}, "decision": decision, "reasons": reasons or [],
                "rationale": rationale, "result": result, "amount": amount,
            }
            prev = self._last_hash()
            h = self._digest(prev, body)
            cur = self.conn.execute(
                "INSERT INTO audit (ts,session,actor,event,tool,args,decision,reasons,rationale,result,amount,prev_hash,hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (body["ts"], session, actor, event, tool, _canon(body["args"]), decision,
                 _canon(body["reasons"]), rationale, _canon(result), amount, prev, h),
            )
            self.conn.commit()
            return {"seq": cur.lastrowid, **body, "hash": h, "prev_hash": prev}

    def rows(self, limit: int = 200) -> list[dict]:
        cur = self.conn.execute(
            "SELECT seq,ts,session,actor,event,tool,args,decision,reasons,rationale,result,amount,prev_hash,hash"
            " FROM audit ORDER BY seq DESC LIMIT ?", (limit,))
        out = []
        for r in cur.fetchall():
            out.append({
                "seq": r[0], "ts": r[1], "session": r[2], "actor": r[3], "event": r[4], "tool": r[5],
                "args": json.loads(r[6] or "{}"), "decision": r[7], "reasons": json.loads(r[8] or "[]"),
                "rationale": r[9], "result": json.loads(r[10] or "null"), "amount": r[11],
                "prev_hash": r[12], "hash": r[13],
            })
        return out

    def verify(self) -> dict:
        prev = GENESIS
        n = 0
        for r in reversed(self.rows(limit=10_000_000)):
            body = {k: r[k] for k in ("ts", "session", "actor", "event", "tool", "args", "decision",
                                       "reasons", "rationale", "result", "amount")}
            if r["prev_hash"] != prev or self._digest(prev, body) != r["hash"]:
                return {"ok": False, "broken_at_seq": r["seq"], "checked": n}
            prev = r["hash"]
            n += 1
        return {"ok": True, "checked": n, "head": prev}

    def executed_amount_since(self, since_ts: float, tools: set[str]) -> int:
        if not tools:
            return 0
        q = ",".join("?" * len(tools))
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM audit WHERE event='executed' AND ts>=? AND tool IN ({q})",
            (since_ts, *tools)).fetchone()
        return int(row[0] or 0)
