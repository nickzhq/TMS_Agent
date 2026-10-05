"""Memory factory: build the backend from config (mem0 when enabled, no-op otherwise)."""
from __future__ import annotations

from src.config.settings import MemorySettings, LLMSettings
from src.memory.base import Memory, NoopMemory


def _mem0_config(memory_settings: MemorySettings, llm: LLMSettings | None) -> dict:
    """Build the mem0 `from_config` dict (pure; no side effects -> verifiable offline).

    - llm      : the chat endpoint (mem0 uses it for fact extraction); OpenAIConfig accepts openai_base_url.
    - embedder : a SEPARATE OpenAI-compatible embeddings endpoint when configured (the chat
                 MaaS endpoint serves no /embeddings); falls back to the chat endpoint.
    - vector_store : Qdrant local (embedded) on-disk -> memories persist across restarts.
                     QdrantConfig is STRICT (rejects extra fields): only allowed keys are sent.
    Field names verified against mem0 2.2.1 (OpenAIConfig / BaseEmbedderConfig / QdrantConfig).
    """
    config: dict = {}
    if llm is not None and llm.base_url:
        config["llm"] = {"provider": "openai", "config": {
            "api_key": llm.api_key, "openai_base_url": llm.base_url, "model": llm.model}}
    if llm is not None:
        emb_base = llm.embedding_base_url or llm.base_url
        emb_key = llm.embedding_api_key or llm.api_key
        if emb_base and llm.embedding_model:
            emb_cfg = {"api_key": emb_key, "openai_base_url": emb_base, "model": llm.embedding_model}
            dims = memory_settings.vector_store.embedding_model_dims
            if dims:
                emb_cfg["embedding_dims"] = dims   # request exactly `dims` so embedder and vector store agree
            config["embedder"] = {"provider": "openai", "config": emb_cfg}

    vs = memory_settings.vector_store
    # QdrantConfig is STRICT (rejects extra fields): send only the keys the mode needs — server mode
    # uses host/port (multi-process/multi-user friendly), local (embedded) uses path/on_disk.
    if vs.mode == "server":
        vs_cfg = {"collection_name": vs.collection_name, "embedding_model_dims": vs.embedding_model_dims,
                  "host": vs.host, "port": vs.port}
    else:
        vs_cfg = {"collection_name": vs.collection_name, "embedding_model_dims": vs.embedding_model_dims,
                  "path": vs.path, "on_disk": vs.on_disk}
    config["vector_store"] = {"provider": vs.provider, "config": vs_cfg}
    if memory_settings.history_db_path:
        config["history_db_path"] = memory_settings.history_db_path
    return config


def build_memory(memory_settings: MemorySettings, llm: LLMSettings | None = None) -> Memory:
    """Return a Memory backend: NoopMemory when disabled, else Mem0Memory over _mem0_config()."""
    if not memory_settings.enabled:
        return NoopMemory()
    if memory_settings.provider == "mem0":
        from src.memory.mem0_client import Mem0Memory
        return Mem0Memory(_mem0_config(memory_settings, llm) or None)
    return NoopMemory()
