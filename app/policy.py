"""Declarative policy engine.

Policies live in the scenario YAML, written by finance in plain clauses. Each tool can
carry a `clause` (the human-readable rule) plus machine rules. The engine returns
ALLOW / DENY / ESCALATE with reasons that cite the clause. The reasons go back to the LLM,
so it can re-plan, and are written to the audit log, so every pound can be explained.

Supported per-tool rules (all optional):
  clause: "P2 ..."             -> cited in every verdict for this tool
  enabled: false               -> always DENY
  requires_human: true         -> always ESCALATE
  max_amount: 20000            -> DENY if amount > value (minor units, e.g. pence)
  escalate_above: 10000        -> ESCALATE if amount > value
  max_percent: 5               -> DENY if args.percent > value
  max_instalments / max_interval_days
  allowed_values: {field: [..]}
  require_evidence: true       -> DENY unless the gateway found evidence (args.evidence_ok)
  once_per: field              -> DENY if this was already done for args[field] (args.already_done)
  cap_with_history: 2500       -> amount + args.history_total must stay within the cap
  cap_action: escalate|deny    -> what to do when the cap is exceeded (default deny)
Global rules:
  daily_outflow_limit: 50000   -> DENY if today's executed outflow + amount > value
"""
from dataclasses import dataclass, field
from typing import Any

ALLOW, DENY, ESCALATE = "allow", "deny", "escalate"


@dataclass
class Decision:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    clause: str | None = None

    def to_dict(self):
        return {"verdict": self.verdict, "reasons": self.reasons, "clause": self.clause}


def _fmt(amount: int | None) -> str:
    return "n/a" if amount is None else f"£{amount / 100:,.2f}"


def clause_id(clause: str | None) -> str:
    return (clause or "").split(" ", 1)[0] if clause else ""


class PolicyEngine:
    def __init__(self, policy: dict):
        self.policy = policy or {}
        self.global_rules = self.policy.get("global", {})
        self.tool_rules = self.policy.get("tools", {})

    def rules_for(self, tool: str) -> dict:
        return self.tool_rules.get(tool, {})

    def evaluate(self, tool: str, args: dict[str, Any], *, outflow_today: int = 0, is_outflow: bool = False) -> Decision:
        r = self.rules_for(tool)
        clause = r.get("clause")
        cid = clause_id(clause)
        tag = f"[{cid}] " if cid else ""
        rc = r.get("rule_clauses") or {}

        def t(rule: str) -> str:
            return f"[{rc[rule]}] " if rule in rc else tag
        deny, escalate = [], []
        amount = args.get("amount")

        if r.get("enabled") is False:
            deny.append(f"{tag}Tool '{tool}' is disabled by policy.")
        if amount is not None and amount <= 0:
            deny.append(f"{tag}Amount must be positive.")
        if "max_amount" in r and amount is not None and amount > r["max_amount"]:
            deny.append(f"{tag}Amount {_fmt(amount)} exceeds max {_fmt(r['max_amount'])}.")
        if "max_percent" in r and args.get("percent") is not None and float(args["percent"]) > r["max_percent"]:
            deny.append(f"{tag}{args['percent']}% exceeds the {r['max_percent']}% limit.")
        if "max_instalments" in r and args.get("instalments") is not None and args["instalments"] > r["max_instalments"]:
            deny.append(f"{tag}{args['instalments']} instalments exceeds the limit of {r['max_instalments']}.")
        if "max_interval_days" in r and args.get("interval_days") is not None and args["interval_days"] > r["max_interval_days"]:
            deny.append(f"{tag}Interval of {args['interval_days']} days exceeds {r['max_interval_days']} days.")
        for fld, allowed in (r.get("allowed_values") or {}).items():
            if fld in args and args[fld] not in allowed:
                deny.append(f"{tag}{fld}='{args[fld]}' is not allowed (allowed: {allowed}).")
        for flag, msg in (r.get("deny_if") or {}).items():
            if args.get(flag):
                deny.append(f"{t(flag)}{msg} {args.get(flag + '_detail', '')}".strip())
        if "max_age_days" in r and args.get("age_days") is not None and args["age_days"] > r["max_age_days"]:
            deny.append(f"{t('max_age_days')}Oldest item is {args['age_days']} days old; limit is {r['max_age_days']} days.")
        if r.get("require_evidence") and not args.get("evidence_ok"):
            deny.append(f"{t('require_evidence')}No evidence: {args.get('evidence') or 'nothing in the records supports this'}.")
        if r.get("once_per") and args.get("already_done"):
            deny.append(f"{tag}Already done for {r['once_per']}={args.get(r['once_per'])}.")
        if "cap_with_history" in r and amount is not None:
            used = int(args.get("history_total") or 0)
            cap = r["cap_with_history"]
            if used + amount > cap:
                msg = (f"{tag}{_fmt(amount)} on top of {_fmt(used)} already given breaks the {_fmt(cap)} cap "
                       f"(remaining {_fmt(max(0, cap - used))}).")
                (escalate if r.get("cap_action") == "escalate" else deny).append(msg)

        limit = self.global_rules.get("daily_outflow_limit")
        if is_outflow and limit is not None and amount is not None and outflow_today + amount > limit:
            deny.append(f"[G1] Daily outflow limit {_fmt(limit)} would be exceeded "
                        f"(already {_fmt(outflow_today)} today).")

        if r.get("requires_human"):
            escalate.append(f"{tag}Tool '{tool}' always requires human approval.")
        if "escalate_above" in r and amount is not None and amount > r["escalate_above"]:
            escalate.append(f"{t('escalate_above')}{_fmt(amount)} is above the auto-approve threshold {_fmt(r['escalate_above'])}.")

        if deny:
            return Decision(DENY, deny, clause)
        if escalate:
            return Decision(ESCALATE, escalate, clause)
        return Decision(ALLOW, [f"{tag}Within policy." + (f" {clause.split(' ', 1)[1]}" if cid else "")], clause)

    def describe(self) -> str:
        """Plain-text summary injected into the agent's system prompt."""
        lines = [f"- {c['id']}: {c['text']}" for c in self.policy.get("clauses", [])]
        for k, v in self.global_rules.items():
            lines.append(f"- global {k}: {v}")
        for tool, rules in self.tool_rules.items():
            c = rules.get("clause")
            rest = ", ".join(f"{k}={v}" for k, v in rules.items() if k != "clause")
            lines.append(f"- {tool}: {c + ' | ' if c else ''}{rest}")
        return "\n".join(lines) or "- (no explicit limits)"

    def clauses(self) -> list[dict]:
        if self.policy.get("clauses"):
            return [{"id": c["id"], "text": c["text"]} for c in self.policy["clauses"]]
        out = []
        for tool, rules in self.tool_rules.items():
            if rules.get("clause"):
                cid = clause_id(rules["clause"])
                out.append({"id": cid, "tool": tool, "text": rules["clause"].split(" ", 1)[1]})
        if "daily_outflow_limit" in self.global_rules:
            out.append({"id": "G1", "tool": "*",
                        "text": f"Total money out per day is capped at {_fmt(self.global_rules['daily_outflow_limit'])}."})
        return out
