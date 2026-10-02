"""mem0-backed memory: long-term user preferences.

mem0 handles extraction / dedup / update of memories internally. We only feed it
user-preference text; authoritative business facts stay in the DB/files (never here).
The optional config points mem0's LLM/embedder at an OpenAI-compatible endpoint.
"""
from __future__ import annotations

from mem0 import Memory as _Mem0

from src.memory.base import Memory


class Mem0Memory(Memory):
    """Memory implementation over mem0."""

    def __init__(self, config: dict | None = None):
        self._m = _Mem0.from_config(config) if config else _Mem0()

    def add(self, user_id: str, content: str, metadata: dict | None = None) -> None:
        self._m.add(content, user_id=user_id, metadata=metadata or {})

    def search(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        # mem0 2.x: search takes top_k + filters (a top-level user_id is rejected);
        # older builds used limit=/user_id=. Scope by user so preferences don't cross users.
        res = self._m.search(query, top_k=top_k, filters={"user_id": user_id})
        if isinstance(res, dict):
            return res.get("results", [])
        return list(res)
