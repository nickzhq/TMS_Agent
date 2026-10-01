"""Knowledge-base factory: build from config (WeKnora when enabled, no-op otherwise)."""
from __future__ import annotations

from src.config.settings import KnowledgeBaseSettings
from src.knowledge.base import KnowledgeBase, NoopKnowledgeBase


def build_knowledge_base(settings: KnowledgeBaseSettings) -> KnowledgeBase:
    """Return a KnowledgeBase. Disabled by default -> NoopKnowledgeBase (wired but off)."""
    if not settings.enabled:
        return NoopKnowledgeBase()
    if settings.provider == "weknora":
        from src.knowledge.weknora_adapter import WeKnoraKnowledgeBase
        return WeKnoraKnowledgeBase(settings.url)
    return NoopKnowledgeBase()
