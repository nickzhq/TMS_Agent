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
    """LLM connection (bare OpenAI-compatible base_url; provider-switchable)."""
    base_url: str = ""
    api_key: str = ""
    model: str = ""


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


class MemorySettings(BaseModel):
    """mem0: stores user preferences only."""
    enabled: bool = True
    provider: str = "mem0"


class KnowledgeBaseSettings(BaseModel):
    """WeKnora knowledge base: wired but disabled by default; set enabled=true once deployed."""
    enabled: bool = False
    provider: str = "weknora"
    url: str = ""


class Settings(BaseModel):
    """Top-level configuration."""
    llm: LLMSettings = Field(default_factory=LLMSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    amap: AmapSettings = Field(default_factory=AmapSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    knowledge_base: KnowledgeBaseSettings = Field(default_factory=KnowledgeBaseSettings)


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
