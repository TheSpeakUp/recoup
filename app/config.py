"""Runtime configuration, read from environment variables (see .env.example)."""
import os
from pathlib import Path

try:  # optional .env support without extra dependency
    for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")          # sk_test_... ; empty -> mock payments
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")  # whsec_... from `stripe listen`
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")          # empty -> scripted mock agent
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
SCENARIO = os.getenv("SCENARIO", "recovery")                  # file name in scenarios/
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "agentcore.db"))
USE_COGNEE = os.getenv("USE_COGNEE", "0") == "1"
CURRENCY = os.getenv("CURRENCY", "gbp")


def mode_summary() -> dict:
    return {
        "payments": "stripe-test" if STRIPE_SECRET_KEY.startswith("sk_test") else ("stripe-LIVE" if STRIPE_SECRET_KEY else "mock"),
        "llm": LLM_MODEL if ANTHROPIC_API_KEY else "scripted-mock",
        "memory": "cognee" if USE_COGNEE else "local",
        "scenario": SCENARIO,
    }
