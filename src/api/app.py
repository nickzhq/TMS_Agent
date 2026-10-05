"""FastAPI Agent backend (P8 front/back split): runs the LangGraph estimate flow behind REST + WS.

Topology: this is the Agent BACKEND process. The Chainlit frontend (src/web) drives an inquiry
over WebSocket (/ws/{thread_id}) and reads the history list over REST (/sessions). Business
logic stays in src/graph + src/agents; this layer is generic glue (no business rules) -> committed.

App-level singletons (settings/llm/checkpointer/sessions/amap/memory) are built once in the
lifespan and shared across all connections — mirroring the Chainlit _app_deps atomic-publish
pattern (a mid-init failure must never leave a half-filled dict). The registry lives in
src/api/deps.py so routers can reach it without importing this module (avoids a circular import).

Run: uvicorn src.api.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.settings import Settings, load_settings
from src.llm.openai_compat import OpenAICompatClient
from src.tools.amap import AmapMCPClient
from src.persistence.checkpointer import async_sqlite_checkpointer
from src.persistence.sessions_db import build_sessions
from src.auth.store import build_user_store
from src.api.deps import publish, clear_app, get_app
from src.api.auth_router import router as auth_router
from src.api.sessions import router as sessions_router
from src.api.ws import router as ws_router

logger = logging.getLogger("estimate.api")

_SETTINGS: Settings | None = None


def _settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the app-level singletons once; release async resources on shutdown.

    persistence.provider selects the backend for the checkpointer + sessions + auth store:
      - sqlite   -> AsyncSqliteSaver + SessionsDB + UserStore (files under data/; dev/tests)
      - postgres -> ONE shared psycopg pool -> AsyncPostgresSaver + PostgresSessionsDB + PostgresUserStore
    The graph runs via ainvoke/astream -> needs the ASYNC checkpointer. Postgres deps are imported
    lazily, so a sqlite-only environment needs neither psycopg nor a running Postgres.
    """
    s = _settings()
    stack = contextlib.AsyncExitStack()
    provider = (s.persistence.provider or "sqlite").lower()

    pool = None
    if provider == "postgres":
        from src.persistence.pg_pool import pg_pool
        from src.persistence.checkpointer import async_postgres_checkpointer
        if not s.postgres.dsn:
            raise RuntimeError("persistence.provider=postgres requires postgres.dsn (set POSTGRES_DSN in .env)")
        pool = await stack.enter_async_context(pg_pool(s.postgres.dsn, s.postgres.pool_min, s.postgres.pool_max))
        checkpointer = await stack.enter_async_context(async_postgres_checkpointer(pool))
    else:
        checkpointer = await stack.enter_async_context(async_sqlite_checkpointer(s.persistence.checkpointer_path))

    sessions = build_sessions(s.persistence, pool)
    users = build_user_store(s.persistence, s.auth, pool)
    await sessions.init()
    await users.init()

    built = {
        "settings": s,
        "llm": OpenAICompatClient(s.llm),
        "sessions": sessions,
        "users": users,
        "amap": AmapMCPClient(s.amap.api_key) if s.amap.api_key else None,
        "checkpointer": checkpointer,
        "stack": stack,
    }
    publish(built)              # atomic publish (only after every dep is ready)
    if not s.auth.jwt_secret:
        logger.warning("AUTH_JWT_SECRET is not set; /auth/login and protected routes will reject until configured")
    logger.info("backend startup: deps ready (provider=%s: llm + checkpointer + sessions + users)", provider)
    try:
        yield
    finally:
        await stack.aclose()
        clear_app()
        logger.info("backend shutdown: deps released")


def create_app() -> FastAPI:
    s = _settings()
    app = FastAPI(title="EstimatePrice Agent API", version="0.8.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_router)
    app.include_router(sessions_router)
    app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "deps": sorted(k for k in get_app() if k != "stack")}

    return app


app = create_app()


if __name__ == "__main__":
    # Backend entry point: run via `python -m src.api.app` (NOT `uvicorn src.api.app:app`).
    # On Windows, uvicorn's own loop factory (uvicorn.loops.asyncio.asyncio_loop_factory) returns
    # ProactorEventLoop, which psycopg async REJECTS ("Psycopg cannot use the 'ProactorEventLoop'"),
    # and it ignores set_event_loop_policy. So we drive the server ourselves with an explicit
    # SelectorEventLoop factory (Python 3.12+ asyncio.run(loop_factory=...)). amap's npx subprocess
    # is unaffected: geocode._amap_geocode creates its own ProactorEventLoop in a worker thread.
    import uvicorn
    _s = _settings()
    if sys.platform == "win32":
        _server = uvicorn.Server(uvicorn.Config(app, host=_s.api.host, port=_s.api.port))
        asyncio.run(_server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        uvicorn.run(app, host=_s.api.host, port=_s.api.port)
