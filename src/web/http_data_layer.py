"""Chainlit data layer over the backend's /sessions REST (P8 front/back split).

Replaces the in-process SessionsDataLayer: threads + steps now live in the BACKEND (single source
of truth). This adapter proxies Chainlit's data-layer calls to the backend over httpx.

TOKEN RESOLUTION (important): Chainlit CACHES the data layer as a SINGLETON, built lazily — often
during login, before a session context exists — so a token captured in __init__ would be empty. We
therefore resolve the backend JWT PER CALL (the current context user's metadata token, else the
login-time cache keyed by identifier; see backend_client.cached_token).

Chainlit types note (P7 pitfall #13): ThreadFilter / PaginatedResponse / PageInfo are pydantic
(attribute access + construct instances); ThreadDict / StepDict are TypedDicts (plain dicts).
Generic glue -> committed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.types import Feedback, PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User

from src.web.backend_client import SessionsREST, cached_token, latest_token

logger = logging.getLogger("estimate.web.datalayer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HttpSessionsDataLayer(BaseDataLayer):
    """BaseDataLayer that proxies to the backend /sessions REST, resolving the JWT per call."""

    def __init__(self, base_url: str, user_id: str = "default"):
        self.base_url = base_url
        self.user_id = user_id

    def _rest(self) -> SessionsREST:
        """Build a per-call REST client with the CURRENT user's backend token. The layer is a Chainlit
        singleton built during login (no session context -> empty token), so the token must NOT be
        captured in __init__; resolve it here from the context user, else the login-time cache."""
        identifier, token = self.user_id, ""
        try:
            user = cl.context.user
            if user is not None:
                identifier = user.identifier or identifier
                token = (user.metadata or {}).get("token", "")
        except Exception:
            pass   # no session context (e.g. during login) -> fall back to the cache
        return SessionsREST(self.base_url, token or cached_token(identifier) or latest_token())

    # ---- users ----
    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return PersistedUser(id=user.identifier, createdAt=_now(), identifier=user.identifier,
                             display_name=user.display_name, metadata=user.metadata or {})

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        # Hand back the cached backend token so cl.context.user.metadata.token survives Chainlit's
        # per-request user rebuild (Chainlit's own JWT stores only the identifier, not metadata).
        return PersistedUser(id=identifier, createdAt=_now(), identifier=identifier,
                             display_name=None, metadata={"token": cached_token(identifier)})

    # ---- steps (message history) ----
    async def create_step(self, step_dict: dict) -> None:
        tid = step_dict.get("threadId")
        if not tid:
            return
        try:
            await self._rest().add_step(tid, step_dict)
        except Exception:
            logger.warning("create_step failed thread=%s", str(tid)[:8], exc_info=True)

    async def update_step(self, step_dict: dict) -> None:
        await self.create_step(step_dict)                    # upsert semantics

    async def delete_step(self, step_id: str) -> None:
        return None           # steps cascade-delete with their thread on the backend

    # ---- threads ----
    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        try:
            row = await self._rest().get(thread_id)
        except Exception:
            logger.warning("get_thread failed thread=%s", str(thread_id)[:8], exc_info=True)
            return None
        if not row:
            return None
        return self._thread_dict(row, row.get("steps", []))

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        # filters/pagination are pydantic (attribute access); server.py calls res.to_dict() -> return
        # a PaginatedResponse INSTANCE (with a PageInfo instance), not a dict.
        try:
            rows = await self._rest().list()
        except Exception:
            logger.warning("list_threads failed", exc_info=True)
            rows = []
        search = getattr(filters, "search", None)
        if search:
            rows = [r for r in rows if search.lower() in (r.get("title") or "").lower()]
        data = [self._thread_dict(r, []) for r in rows]       # list view: metadata only (steps lazy via get_thread)
        return PaginatedResponse(pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None), data=data)

    async def update_thread(self, thread_id: str, name: Optional[str] = None, user_id: Optional[str] = None,
                            metadata: Optional[dict] = None, tags: Optional[List[str]] = None) -> None:
        if name is not None:
            try:
                await self._rest().rename(thread_id, name)
            except Exception:
                logger.warning("update_thread rename failed thread=%s", str(thread_id)[:8], exc_info=True)

    async def delete_thread(self, thread_id: str) -> None:
        try:
            await self._rest().delete(thread_id)
        except Exception:
            logger.warning("delete_thread failed thread=%s", str(thread_id)[:8], exc_info=True)

    async def get_thread_author(self, thread_id: str) -> str:
        row = await self._rest().get(thread_id)
        return (row or {}).get("user_id") or self.user_id

    def _thread_dict(self, row: dict, steps: list) -> ThreadDict:
        return {
            "id": row["thread_id"],
            "createdAt": row.get("created_at") or _now(),
            "name": row.get("title"),
            "userId": row.get("user_id") or self.user_id,
            "userIdentifier": row.get("user_id") or self.user_id,
            "tags": [],
            "metadata": {},
            "steps": steps,
            "elements": [],
        }

    # ---- elements / feedback / favorites: not persisted ----
    async def create_element(self, element: Any) -> None:
        return None

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None) -> None:
        return None

    async def get_element(self, thread_id: str, element_id: str) -> Optional[dict]:
        return None

    async def upsert_feedback(self, feedback: Feedback) -> str:
        fid = feedback.get("id") if isinstance(feedback, dict) else getattr(feedback, "id", None)
        return fid or ""

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def get_favorite_steps(self, user_id: str) -> List[dict]:
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        return None
