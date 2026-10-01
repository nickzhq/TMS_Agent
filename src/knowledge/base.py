"""Knowledge base abstraction: RAG retrieval over external documents.

Business code depends on this interface; concrete backends (WeKnora now) implement it.
Wired but disabled by default — a no-op is used until a real service is deployed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class KnowledgeBase(ABC):
    """Minimal knowledge-base (RAG) interface."""

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Retrieve the top-k relevant document chunks for a query."""
        ...


class NoopKnowledgeBase(KnowledgeBase):
    """No-op knowledge base (used when disabled): searches return empty."""

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        return []
