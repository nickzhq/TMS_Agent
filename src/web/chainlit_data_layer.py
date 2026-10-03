"""Custom Chainlit data layer backed by our SQLite sessions store (P7 batch-2, S5.1b).

Chainlit's native left-sidebar history + on_chat_resume require a BaseDataLayer. Rather than a
separate store (double-write — pitfall #3), this ADAPTS our SessionsDB: thread metadata lives in
the `sessions` table, message history (StepDicts) in the `steps` table. Graph STATE stays in the
LangGraph checkpointer under the SAME thread_id, so resuming a thread rehydrates the flow.

Single-user MVP: fixed 'default' user; elements / feedback / favorites are no-ops.
Generic glue (no business logic) -> committed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from chainlit.data.base import BaseDataLayer
from chainlit.types import Feedback, PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User

from src.persistence.sessions_db import SessionsDB

logger = logging.getLogger("estimate.web.datalayer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionsDataLayer(BaseDataLayer):
    """BaseDataLayer over SessionsDB (threads) + its steps table (message history)."""

    def __init__(self, sessions: SessionsDB, user_id: str = "default"):
        self.sessions = sessions
        self.user_id = user_id

    # ---- users (single-user MVP) ----
    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return PersistedUser(id=user.identifier, createdAt=_now(), identifier=user.identifier,
                             display_name=user.display_name, metadata=user.metadata or {})

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        return PersistedUser(id=identifier, createdAt=_now(), identifier=identifier,
                             display_name=None, metadata={})

    # ---- steps (message history) ----
    async def create_step(self, step_dict: dict) -> None:
        tid, sid = step_dict.get("threadId"), step_dict.get("id")
        if not (tid and sid):
            return
        await self.sessions.upsert_thread(tid, self.user_id)   # ensure the thread row exists
        await self.sessions.upsert_step(tid, sid, step_dict)
        logger.info("create_step thread=%s type=%s", tid[:8], step_dict.get("type"))

    async def update_step(self, step_dict: dict) -> None:
        await self.create_step(step_dict)                      # upsert semantics

    async def delete_step(self, step_id: str) -> None:
        await self.sessions.delete_step(step_id)

    # ---- threads ----
    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        row = await self.sessions.get(thread_id)
        if not row:
            return None
        return self._thread_dict(row, await self.sessions.list_steps(thread_id))

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        # filters/pagination are pydantic objects (NOT dicts) -> attribute access; and server.py calls
        # res.to_dict(), so return a PaginatedResponse INSTANCE (with a PageInfo instance), not a dict.
        # single-user MVP: always list our fixed user's threads (Chainlit sets filters.userId to the
        # persisted user's id; our threads live under 'default', so ignore it to avoid a mismatch).
        rows = await self.sessions.list(self.user_id)
        search = getattr(filters, "search", None)
        if search:
            rows = [r for r in rows if search.lower() in (r.get("title") or "").lower()]
        data = [self._thread_dict(r, []) for r in rows]        # list view: metadata only (steps lazy via get_thread)
        logger.info("list_threads(search=%s) -> %d threads", search, len(data))
        return PaginatedResponse(pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None), data=data)

    async def update_thread(self, thread_id: str, name: Optional[str] = None, user_id: Optional[str] = None,
                            metadata: Optional[dict] = None, tags: Optional[List[str]] = None) -> None:
        if name is not None:
            await self.sessions.rename(thread_id, name)
        else:
            await self.sessions.touch(thread_id)

    async def delete_thread(self, thread_id: str) -> None:
        await self.sessions.delete(thread_id)

    async def get_thread_author(self, thread_id: str) -> str:
        row = await self.sessions.get(thread_id)
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

    # ---- elements / feedback / favorites: not persisted in the MVP ----
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
