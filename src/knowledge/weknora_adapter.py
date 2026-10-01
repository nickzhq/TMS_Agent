"""WeKnora knowledge-base adapter (REST). Wired but disabled by default.

WeKnora is an external Go service providing RAG retrieval. This adapter calls its
REST API; once deployed, set knowledge_base.enabled=true + url — zero business-code change.
Retrieval is best-effort: any error returns empty (never breaks the main flow).
"""
from __future__ import annotations

import json
import logging
import urllib.request

from src.knowledge.base import KnowledgeBase

logger = logging.getLogger("estimate.knowledge")


class WeKnoraKnowledgeBase(KnowledgeBase):
    """KnowledgeBase over the WeKnora REST API."""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        req = urllib.request.Request(
            f"{self.url}/search",
            data=json.dumps({"query": query, "top_k": top_k}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("results", [])
        except Exception as exc:   # knowledge base is optional; never break the main flow
            logger.warning("knowledge base search failed: %s", type(exc).__name__)
            return []
