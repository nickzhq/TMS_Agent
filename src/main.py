"""Application entry point (skeleton).

Loads and verifies configuration (config.yaml + env vars -> Settings).
Later stages wire in the orchestration graph, tools, and sub-agent nodes.
"""
from __future__ import annotations

from src.config.settings import load_settings


def main() -> None:
    settings = load_settings()

    print("=== Logistics Quotation System - bootstrap ===")
    print(f"LLM      : model={settings.llm.model or '<unset>'} base_url={settings.llm.base_url or '<unset>'}")
    print(f"Embedder : model={settings.llm.embedding_model or '<unset>'} base_url={settings.llm.embedding_base_url or '(same as LLM)'}")
    print(f"DB       : {'configured' if settings.db.host else '<unset>'} (host / database / password redacted)")
    print(f"Amap     : api_key={'configured' if settings.amap.api_key else '<unset>'}")
    vs = settings.memory.vector_store
    vs_loc = f"{vs.host}:{vs.port}" if vs.mode == "server" else f"path={vs.path}"
    print(f"Memory   : enabled={settings.memory.enabled} provider={settings.memory.provider} "
          f"vector_store={vs.provider} mode={vs.mode or 'local'} ({vs_loc}, dims={vs.embedding_model_dims})")
    print(f"Knowledge: enabled={settings.knowledge_base.enabled} provider={settings.knowledge_base.provider} (wired, disabled)")
    print(f"Persist  : provider={settings.persistence.provider or 'sqlite'} "
          f"checkpointer={settings.persistence.checkpointer_path} sessions={settings.persistence.sessions_db_path}")
    print(f"Postgres : dsn={'configured' if settings.postgres.dsn else '<unset>'} pool={settings.postgres.pool_min}-{settings.postgres.pool_max}")
    print(f"API      : http://{settings.api.host}:{settings.api.port} (cors={settings.api.cors_origins})")
    print(f"Auth     : jwt_secret={'configured' if settings.auth.jwt_secret else '<unset>'} "
          f"user_db={settings.auth.user_db_path} register={settings.auth.allow_registration}")
    print(f"Backend  : base={settings.backend.base_url} ws={settings.backend.ws_url}")
    print("=== configuration loaded ===")


if __name__ == "__main__":
    main()
