"""Memory factory: build the backend from config (mem0 when enabled, no-op otherwise)."""
from __future__ import annotations

from src.config.settings import MemorySettings, LLMSettings
from src.memory.base import Memory, NoopMemory


def build_memory(memory_settings: MemorySettings, llm: LLMSettings | None = None) -> Memory:
    """Return a Memory backend.

    - disabled -> NoopMemory
    - provider 'mem0' -> Mem0Memory, with its LLM/embedder pointed at the configured
      OpenAI-compatible endpoint (reuses the same base_url/api_key as the chat LLM)
    """
    if not memory_settings.enabled:
        return NoopMemory()
    if memory_settings.provider == "mem0":
        from src.memory.mem0_client import Mem0Memory
        config = None
        if llm is not None and llm.base_url:
            config = {
                "llm": {"provider": "openai", "config": {
                    "api_key": llm.api_key, "openai_base_url": llm.base_url, "model": llm.model}},
                "embedder": {"provider": "openai", "config": {
                    "api_key": llm.api_key, "openai_base_url": llm.base_url,
                    "model": llm.embedding_model}},
            }
        return Mem0Memory(config)
    return NoopMemory()
