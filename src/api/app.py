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

import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.settings import Settings, load_settings
from src.llm.openai_compat import OpenAICompatClient
from src.tools.amap import AmapMCPClient
from src.persistence.checkpointer import async_sqlite_checkpointer
from src.persistence.sessions_db import SessionsDB
from src.auth.store import UserStore
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
    """Build the app-level singletons once; release async resources on shutdown."""
    s = _settings()
    stack = contextlib.AsyncExitStack()
    built = {
        "settings": s,
        "llm": OpenAICompatClient(s.llm),
        "sessions": SessionsDB(s.persistence.sessions_db_path),
        "users": UserStore(s.auth.user_db_path),
        "amap": AmapMCPClient(s.amap.api_key) if s.amap.api_key else None,
    }
    # The graph runs via ainvoke/astream -> needs the ASYNC checkpointer (a sync saver raises
    # NotImplementedError on its async methods).
    built["checkpointer"] = await stack.enter_async_context(
        async_sqlite_checkpointer(s.persistence.checkpointer_path))
    await built["sessions"].init()
    await built["users"].init()
    built["stack"] = stack
    publish(built)              # atomic publish (only after every dep is ready)
    if not s.auth.jwt_secret:
        logger.warning("AUTH_JWT_SECRET is not set; /auth/login and protected routes will reject until configured")
    logger.info("backend startup: deps ready (llm + async checkpointer + sessions + users)")
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
