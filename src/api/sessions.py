"""Session CRUD REST (P8): the history list the frontend renders in its left sidebar.

Backed by SessionsDB (the same store the checkpointer keys threads by; no double-write). Every
route is authenticated (Depends(get_current_user)) and scoped to the caller's uid; per-thread
routes also check ownership (require_thread_owner). Generic infrastructure -> committed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import get_app, get_current_user, require_thread_owner
from src.persistence.sessions_db import PLACEHOLDER_TITLE

logger = logging.getLogger("estimate.api.sessions")
router = APIRouter(prefix="/sessions", tags=["sessions"])

# In-process tombstone: thread_ids deleted during this run. Guards against a stale step write
# (the frontend still flushing history for a just-deleted active chat) silently re-creating the
# session row via add_step's upsert_thread — which made chats look "undeletable". A deleted chat
# is gone from the list, so nothing writes to it after this short in-flight window; the set is
# cleared on restart, which is fine. (For a long-lived multi-worker deploy, replace with a short-TTL
# store or a soft-delete flag on the sessions row.)
_DELETED: set[str] = set()


class RenameIn(BaseModel):
    title: str


class StepIn(BaseModel):
    step: dict


@router.get("")
async def list_sessions(uid: str = Depends(get_current_user)) -> list[dict]:
    """All sessions of the AUTHENTICATED user, most-recently-updated first (feeds the history list)."""
    return await get_app()["sessions"].list(uid)


@router.get("/{thread_id}")
async def get_session(thread_id: str, uid: str = Depends(get_current_user)) -> dict:
    """One session's metadata + its stored steps (so the frontend can re-render the thread)."""
    await require_thread_owner(thread_id, uid)
    sessions = get_app()["sessions"]
    row = await sessions.get(thread_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    steps = await sessions.list_steps(thread_id)
    return {**row, "steps": steps}


@router.patch("/{thread_id}")
async def rename_session(thread_id: str, body: RenameIn, uid: str = Depends(get_current_user)) -> dict:
    await require_thread_owner(thread_id, uid)
    sessions = get_app()["sessions"]
    if not await sessions.get(thread_id):
        raise HTTPException(status_code=404, detail="session not found")
    await sessions.rename(thread_id, body.title)
    return {"ok": True, "thread_id": thread_id, "title": body.title}


@router.delete("/{thread_id}")
async def delete_session(thread_id: str, uid: str = Depends(get_current_user)) -> dict:
    await require_thread_owner(thread_id, uid)
    await get_app()["sessions"].delete(thread_id)
    _DELETED.add(thread_id)                            # tombstone: don't let a stale step resurrect it
    return {"ok": True, "thread_id": thread_id}


@router.post("/{thread_id}/steps")
async def add_step(thread_id: str, body: StepIn, uid: str = Depends(get_current_user)) -> dict:
    """Persist one Chainlit StepDict (the frontend data layer proxies here, so the backend stays the
    single source of truth for message history / resume re-rendering)."""
    await require_thread_owner(thread_id, uid)
    step = body.step or {}
    sid = step.get("id")
    if not sid:
        raise HTTPException(status_code=400, detail="step missing id")
    sessions = get_app()["sessions"]
    if thread_id in _DELETED and await sessions.get(thread_id) is None:
        return {"ok": True, "thread_id": thread_id, "step_id": sid, "ignored": True}  # deleted -> do not resurrect
    # The row must exist BEFORE the first step lands, otherwise the very first messages (the welcome
    # note, the user's own first message, the "按示例执行" echo) are dropped. It also has to stay VISIBLE
    # in the history list: Chainlit bounces a fresh chat back to the newest listed thread when the
    # current one is missing, which looked like "New chat jumped back to the previous conversation".
    # So: create with a placeholder title (renamed by the WS "query" handler on the first message) and
    # only touch an existing row — never overwrite a real title with the placeholder.
    if await sessions.get(thread_id) is None:
        await sessions.upsert_thread(thread_id, uid, title=PLACEHOLDER_TITLE)
        # Opening the page repeatedly must not pile up empty "新对话" rows: keep only this one.
        await sessions.prune_placeholders(uid, keep_thread_id=thread_id)
    else:
        await sessions.upsert_thread(thread_id, uid)   # title=None -> COALESCE keeps the real title
    await sessions.upsert_step(thread_id, sid, step)
    return {"ok": True, "thread_id": thread_id, "step_id": sid}


@router.post("/{thread_id}/ensure")
async def ensure_session(thread_id: str, uid: str = Depends(get_current_user)) -> dict:
    """Create the thread row up-front (idempotent) so a page refresh can always resume it.

    A fresh chat otherwise has no row until its first message step arrives; Chainlit resumes from the
    URL as soon as the chat opens, so a refresh inside that window probed 404 and popped
    "Couldn't resume chat, threadID not found".
    """
    await require_thread_owner(thread_id, uid)
    sessions = get_app()["sessions"]
    if await sessions.get(thread_id) is None:
        await sessions.upsert_thread(thread_id, uid, title=PLACEHOLDER_TITLE)
        await sessions.prune_placeholders(uid, keep_thread_id=thread_id)
    return {"ok": True, "thread_id": thread_id}
