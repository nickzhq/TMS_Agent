"""Memory abstraction: store/retrieve user preferences (long-term, cross-session).

Business code depends on this interface; concrete backends (mem0 now, others later)
implement it, so swapping backends never touches business logic.

Boundary (important): only *user preferences* (habits, frequent choices) are stored here.
Authoritative business facts live in the DB / files and must never be put into memory.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Memory(ABC):
    """Minimal long-term memory interface (user preferences only)."""

    @abstractmethod
    def add(self, user_id: str, content: str, metadata: dict | None = None) -> None:
        """Store a preference extracted from a conversation."""
        ...

    @abstractmethod
    def search(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        """Retrieve the most relevant stored preferences for a user."""
        ...


class NoopMemory(Memory):
    """No-op memory (used when disabled): stores nothing, searches return empty."""

    def add(self, user_id: str, content: str, metadata: dict | None = None) -> None:
        return None

    def search(self, user_id: str, query: str, top_k: int = 5) -> list[dict]:
        return []
