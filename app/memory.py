"""Agent memory about counterparties (customers, suppliers). Uses Cognee when
USE_COGNEE=1 (side-quest friendly), otherwise a tiny local JSON store with the same API."""
import asyncio
import json
import logging

from . import config

log = logging.getLogger("memory")


class LocalMemory:
    name = "local"

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}

    async def remember(self, subject: str, fact: str):
        self.data.setdefault(subject, []).append(fact)
        self.path.write_text(json.dumps(self.data, indent=1))

    async def recall(self, subject: str, query: str = "") -> list[str]:
        return self.data.get(subject, [])[-10:]


class CogneeMemory:
    name = "cognee"

    def __init__(self, fallback: LocalMemory):
        import cognee  # noqa: F401  (fails fast if not installed)
        self.fallback = fallback

    async def remember(self, subject: str, fact: str):
        import cognee
        await self.fallback.remember(subject, fact)
        try:
            await asyncio.wait_for(cognee.add(f"[{subject}] {fact}", dataset_name="agentcore"), 20)
            await asyncio.wait_for(cognee.cognify(datasets=["agentcore"]), 60)
        except Exception as e:  # never let memory break a money flow
            log.warning("cognee remember failed: %s", e)

    async def recall(self, subject: str, query: str = "") -> list[str]:
        import cognee
        try:
            res = await asyncio.wait_for(cognee.search(query_text=f"What do we know about {subject}? {query}"), 20)
            facts = [str(r) for r in (res or [])][:10]
            if facts:
                return facts
        except Exception as e:
            log.warning("cognee recall failed: %s", e)
        return await self.fallback.recall(subject, query)


def build_memory():
    local = LocalMemory(config.BASE_DIR / "memory.json")
    if config.USE_COGNEE:
        try:
            return CogneeMemory(local)
        except Exception as e:
            log.warning("Cognee unavailable (%s); using local memory", e)
    return local
