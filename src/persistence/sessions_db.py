"""Session metadata store (left-side history list) — async, aiosqlite-backed.

Kept SEPARATE from the LangGraph checkpointer on purpose (no double-write):
  - checkpointer (SqliteSaver) -> owns graph STATE per thread_id
  - this store (SessionsDB)     -> owns the session LIST metadata (title/timestamps) for the UI

Stable on-disk path under data/ (gitignore), NOT /tmp -> survives restarts.
Generic infrastructure (no business logic) -> committed to the repo.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_INSERT = "INSERT INTO sessions (thread_id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)"
_COLS = "thread_id, user_id, title, created_at, updated_at"


def make_thread_id(user_id: str, session_id: str | None = None) -> str:
    """thread_id = user-{uid}-session-{sid}: the session-isolation key shared by the
    checkpointer and this store. `session_id` defaults to a random hex."""
    sid = session_id or uuid.uuid4().hex[:12]
    return f"user-{user_id}-session-{sid}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionsDB:
    """Async CRUD over the session metadata table (one short-lived connection per call)."""

    def __init__(self, path: str):
        self.path = path
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _db(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(_SCHEMA)   # idempotent guard (CREATE TABLE IF NOT EXISTS)
            yield db

    async def init(self) -> None:
        """Ensure the schema exists (call once at app startup)."""
        async with self._db():
            pass

    async def create(self, user_id: str, title: str | None = None, session_id: str | None = None) -> str:
        """Insert a new session; returns its thread_id."""
        thread_id = make_thread_id(user_id, session_id)
        now = _now()
        async with self._db() as db:
            await db.execute(_INSERT, (thread_id, user_id, title, now, now))
            await db.commit()
        return thread_id

    async def list(self, user_id: str) -> list[dict]:
        """All sessions for a user, most-recently-updated first (feeds the history list)."""
        async with self._db() as db:
            cur = await db.execute(
                f"SELECT {_COLS} FROM sessions WHERE user_id=? ORDER BY updated_at DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get(self, thread_id: str) -> dict | None:
        async with self._db() as db:
            cur = await db.execute(f"SELECT {_COLS} FROM sessions WHERE thread_id=?", (thread_id,))
            r = await cur.fetchone()
        return dict(r) if r else None

    async def rename(self, thread_id: str, title: str) -> None:
        async with self._db() as db:
            await db.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE thread_id=?",
                (title, _now(), thread_id),
            )
            await db.commit()

    async def touch(self, thread_id: str) -> None:
        """Bump updated_at (e.g. after a new turn) so the session rises in the list."""
        async with self._db() as db:
            await db.execute("UPDATE sessions SET updated_at=? WHERE thread_id=?", (_now(), thread_id))
            await db.commit()

    async def delete(self, thread_id: str) -> None:
        async with self._db() as db:
            await db.execute("DELETE FROM sessions WHERE thread_id=?", (thread_id,))
            await db.commit()
