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

logger = logging.getLogger("estimate.api.sessions")
router = APIRouter(prefix="/sessions", tags=["sessions"])


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
    await sessions.upsert_thread(thread_id, uid)     # ensure the thread row exists under this uid
    await sessions.upsert_step(thread_id, sid, step)
    return {"ok": True, "thread_id": thread_id, "step_id": sid}
