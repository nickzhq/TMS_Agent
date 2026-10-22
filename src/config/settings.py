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
    """mem0 vector store: Qdrant, in either local (embedded) or server mode.

    mode="local"  -> embedded, on-disk under `path` (single-process; simple dev). `path` MUST be a
                     stable in-project dir (NOT /tmp: mem0's default /tmp/qdrant is wiped on every
                     start, so memories would not persist).
    mode="server" -> a standalone Qdrant (Docker), reached by host/port; multi-process/multi-user
                     friendly (embedded mode takes a single-process lock on `path`).
    `embedding_model_dims` MUST match the embedding model's real output dims.
    """
    provider: str = "qdrant"
    mode: str = "local"                 # local (embedded, path/on_disk) | server (host/port)
    path: str = "data/qdrant"           # local mode
    on_disk: bool = True                # local mode
    host: str = "localhost"             # server mode
    port: int = 6333                     # server mode
    collection_name: str = "user_preferences"
    embedding_model_dims: int = 1024


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
    """Web-layer persistence: LangGraph checkpointer + session metadata + auth user store.

    provider selects the backend for ALL THREE (P8 batch-2):
      - "sqlite"   -> files under data/ (gitignore); single-process; default for dev/tests
      - "postgres" -> a shared Postgres (see PostgresSettings); multi-process/multi-user production
    The *_path fields are used only in sqlite mode.
    """
    provider: str = "sqlite"                        # sqlite | postgres
    checkpointer_path: str = "data/langgraph.db"    # sqlite mode: LangGraph SqliteSaver file
    sessions_db_path: str = "data/sessions.db"      # sqlite mode: session metadata table


class PostgresSettings(BaseModel):
    """Postgres connection (P8 batch-2; used when persistence.provider == "postgres").

    dsn comes from env (POSTGRES_DSN), never committed. One shared async pool (psycopg v3) serves
    the checkpointer + sessions + auth stores. pool_min/pool_max size the pool.
    """
    dsn: str = ""                                   # e.g. postgresql://user:pass@localhost:5432/dbname
    pool_min: int = 1
    pool_max: int = 20


class ApiSettings(BaseModel):
    """FastAPI backend server (P8 front/back split): bind address + CORS for the frontend."""
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])   # restrict to the frontend origin in production


class AuthSettings(BaseModel):
    """Real auth (P8): JWT signing + a password-hashed user store (replaces the mock login).

    jwt_secret MUST come from an env var (never committed); generate one with
    `python -c "import secrets; print(secrets.token_urlsafe(48))"`. user_db_path is SQLite for
    batch-1 (-> Postgres in a later batch). allow_registration gates the public /auth/register
    endpoint (default off; create the first account via `python -m src.auth.cli create-user`).
    Password hashing uses the stdlib (hashlib.pbkdf2_hmac) -> no passlib/bcrypt dependency.
    """
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720          # 12h
    user_db_path: str = "data/auth.db"
    allow_registration: bool = False


class BackendSettings(BaseModel):
    """Where the (Chainlit) frontend reaches the FastAPI backend (P8 front/back split)."""
    base_url: str = "http://127.0.0.1:8000"
    ws_url: str = "ws://127.0.0.1:8000"


class BillSettings(BaseModel):
    """Bill Agent: output location and asset paths for generated billing packages.

    output_root is the root folder of a billing package; products are laid out as
    {user}/{shipper_short_name}/{YYYYMM} under it. Empty values fall back to the default
    paths documented in config.yaml and agents/bill/.
    """

    output_root: str = ""              # billing output root (empty = default desktop folder)
    output_user: str = "ai_gen"         # user folder under output_root (CLI placeholder; web uses the login name)
    run_dir: str = "data/bill_run"     # staging dir (written first, then moved as a whole to output_root)
    templates_dir: str = ""            # template dir (empty = agents/bill/templates)
    shippers_file: str = ""            # shipper registry (empty = agents/bill/shippers.yaml)
    logo_path: str = ""                # logo for the charge-list PDF (empty = agents/bill/assets/logo.png)
    prior_ledger: str = ""             # prior-period baseline file .xlsx/.xls/.txt (empty = data/_xls_dump.txt)
    build_vouchers: bool = True       # also generate the per-B/L voucher collection (many PDFs; can be turned off)
    ledger_db: str = ""                 # sqlite ledger DB path (postgres backend ignores it)
    ledger_provider: str = "postgres"   # ledger backend: postgres | sqlite
    ledger_dsn: str = ""                  # postgres DSN (empty = POSTGRES_DSN env var)
    ledger_schema: str = "bill"           # postgres schema holding the ledger tables (isolated from app tables)


class Settings(BaseModel):
    """Top-level configuration."""
    llm: LLMSettings = Field(default_factory=LLMSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    amap: AmapSettings = Field(default_factory=AmapSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    knowledge_base: KnowledgeBaseSettings = Field(default_factory=KnowledgeBaseSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    backend: BackendSettings = Field(default_factory=BackendSettings)
    bill: BillSettings = Field(default_factory=BillSettings)


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
