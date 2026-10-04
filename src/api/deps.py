"""Shared backend runtime + dependencies (P8).

Holds the app-level singleton registry (_APP) + accessors, so routers reach the shared deps
WITHOUT importing app.py (app.py includes the routers -> importing it back would be circular),
plus the auth dependencies (get_current_user / ws_uid / require_thread_owner).
Generic infrastructure -> committed.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.security import decode_token

logger = logging.getLogger("estimate.api.deps")

# Bearer scheme with auto_error=False so a missing/invalid token becomes OUR 401 (not FastAPI's default).
_bearer = HTTPBearer(auto_error=False)

# App-level singletons, published atomically by the lifespan (shared across all connections).
_APP: dict[str, Any] = {}


def get_app() -> dict[str, Any]:
    """Accessor for the shared singletons (used by routers). Empty until the lifespan runs."""
    return _APP


def publish(deps: dict[str, Any]) -> None:
    """Atomically publish the built singletons (the lifespan calls this once all deps are ready,
    so a mid-init failure can never leave a half-filled registry)."""
    _APP.update(deps)


def clear_app() -> None:
    """Drop all singletons (called on shutdown)."""
    _APP.clear()


def get_memory():
    """Lazily build mem0 (guarded). Qdrant local is single-process; on init failure degrade to
    Noop so a memory problem never breaks a request (mirrors the Chainlit _get_memory pattern)."""
    if "memory" not in _APP:
        from src.memory.factory import build_memory
        s = _APP["settings"]
        try:
            _APP["memory"] = build_memory(s.memory, s.llm)
        except Exception as exc:
            logger.warning("mem0 init failed (%s); memory disabled this run", type(exc).__name__)
            from src.memory.base import NoopMemory
            _APP["memory"] = NoopMemory()
    return _APP["memory"]


def _auth_cfg():
    """The AuthSettings sub-model (jwt secret/algorithm)."""
    return _APP["settings"].auth


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Resolve the Bearer JWT -> uid; 401 if missing/invalid/expired. FastAPI dependency for
    protected REST routes."""
    cfg = _auth_cfg()
    token = credentials.credentials if credentials else None
    uid = decode_token(token, cfg.jwt_secret, cfg.jwt_algorithm) if token else None
    if not uid:
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return uid


def ws_uid(websocket: WebSocket) -> str | None:
    """Resolve uid from a WS connect (?token=<JWT>). Browsers can't set WS headers, so the token
    rides in the query string. None if absent/invalid -> the endpoint closes the socket."""
    cfg = _auth_cfg()
    token = websocket.query_params.get("token")
    return decode_token(token, cfg.jwt_secret, cfg.jwt_algorithm) if token else None


async def require_thread_owner(thread_id: str, uid: str) -> None:
    """403 if the thread EXISTS and belongs to a DIFFERENT user. A brand-new thread (no row yet) is
    allowed — it is created under this uid on first persist. thread_id is the Chainlit-generated id
    (an opaque UUID), so ownership comes from the sessions row's user_id, NOT the id format."""
    row = await _APP["sessions"].get(thread_id)
    if row and row.get("user_id") and row.get("user_id") != uid:
        raise HTTPException(status_code=403, detail="not your session")
