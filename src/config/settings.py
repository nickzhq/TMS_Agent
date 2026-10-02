"""Configuration layer: load config.yaml + expand env vars -> Settings (pydantic-validated).

Skeleton only: loads configuration, contains no business logic.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class LLMSettings(BaseModel):
    """LLM connection (bare OpenAI-compatible base_url; provider-switchable).

    The mem0 embedder can point at a SEPARATE OpenAI-compatible endpoint: the chat
    endpoint may not serve /embeddings. Empty embedding_base_url / embedding_api_key
    fall back to base_url / api_key.
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    embedding_base_url: str = ""                 # mem0 embedder endpoint (falls back to base_url)
    embedding_api_key: str = ""                  # mem0 embedder key (falls back to api_key)
    embedding_model: str = ""                      # mem0 embedder model; set EMBEDDING_MODEL in .env (verify via tests/test_p7_embedding)


class DbSettings(BaseModel):
    """Database connection (pyodbc, read-only account; credentials via env vars, never committed).

    connection_string takes precedence (full string); falls back to host/database/user/password fields.
    """
    connection_string: str = ""
    host: str = ""
    database: str = ""
    user: str = ""
    password: str = ""


class AmapSettings(BaseModel):
    """Amap MCP (maps_geo geocoding)."""
    api_key: str = ""


class VectorStoreSettings(BaseModel):
    """mem0 vector store: Qdrant local (embedded) mode with on-disk persistence.

    `path` MUST be a stable in-project dir (NOT /tmp): mem0's default Qdrant lives in
    /tmp/qdrant and is wiped/recreated on every start, so memories would not persist.
    `embedding_model_dims` MUST match the embedding model's real output dims (verify in P7-6).
    """
    provider: str = "qdrant"
    path: str = "data/qdrant"
    on_disk: bool = True
    collection_name: str = "user_preferences"
    embedding_model_dims: int = 1024    # placeholder; confirm against the real embeddings dims in P7-6


class MemorySettings(BaseModel):
    """mem0: stores user preferences only (business facts stay in DB/files)."""
    enabled: bool = True
    provider: str = "mem0"
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    history_db_path: str = "data/mem0_history.db"   # mem0 local SQLite history (default ~/.mem0/history.db)


class KnowledgeBaseSettings(BaseModel):
    """WeKnora knowledge base: wired but disabled by default; set enabled=true once deployed."""
    enabled: bool = False
    provider: str = "weknora"
    url: str = ""


class PersistenceSettings(BaseModel):
    """Web-layer persistence (P7): LangGraph checkpointer + session metadata.

    Both are stable file paths under data/ (gitignore), NOT /tmp -> survive restarts.
    """
    checkpointer_path: str = "data/langgraph.db"    # LangGraph SqliteSaver file (-> PostgresSaver for multi-user later)
    sessions_db_path: str = "data/sessions.db"      # session metadata table (drives the left-side history list)


class Settings(BaseModel):
    """Top-level configuration."""
    llm: LLMSettings = Field(default_factory=LLMSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    amap: AmapSettings = Field(default_factory=AmapSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    knowledge_base: KnowledgeBaseSettings = Field(default_factory=KnowledgeBaseSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)


_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _expand_env(value):
    """Recursively replace ${VAR} with env var values (empty string if unset)."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load config.yaml + expand env vars -> Settings."""
    try:
        from dotenv import load_dotenv
        load_dotenv()               # load .env (if present) into the environment
    except ImportError:
        pass                        # python-dotenv not installed; skip (optional)

    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    config_path = Path(config_path)

    raw: dict = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    return Settings(**_expand_env(raw))
