"""Session metadata store (left-side history list) — async, aiosqlite-backed.

Kept SEPARATE from the LangGraph checkpointer on purpose (no double-write):
  - checkpointer (SqliteSaver) -> owns graph STATE per thread_id
  - this store (SessionsDB)     -> owns the session LIST metadata (title/timestamps) for the UI

Stable on-disk path under data/ (gitignore), NOT /tmp -> survives restarts.
Generic infrastructure (no business logic) -> committed to the repo.
"""
from __future__ import annotations

import json
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

# Title given to a chat that exists only because the page was opened; the WS "query" handler renames
# it with the user's first message. Shared with the Postgres backend so both behave identically.
PLACEHOLDER_TITLE = "新对话"

# steps: opaque StepDict JSON per (thread, step) — the Chainlit data layer's message history,
# so a resumed thread can re-render its past conversation.
_STEPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
    thread_id  TEXT NOT NULL,
    step_id    TEXT NOT NULL,
    created_at TEXT,
    data       TEXT NOT NULL,
    PRIMARY KEY (thread_id, step_id)
)
"""


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
            await db.execute(_SCHEMA)        # idempotent guard (CREATE TABLE IF NOT EXISTS)
            await db.execute(_STEPS_SCHEMA)  # steps table (Chainlit data-layer message history)
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
        """All sessions for a user, most-recently-updated first (feeds the history list).

        Untitled rows are excluded, and so are placeholder rows ("新对话"): a history entry must
        mean "a real conversation". Rows are created only by the first user message now, so this
        filters legacy leftovers (and the brief moment before the WS renames the row).
        """
        async with self._db() as db:
            cur = await db.execute(
                f"SELECT {_COLS} FROM sessions WHERE user_id=? "
                "AND (title IS NOT NULL AND title <> '' AND title <> ?) ORDER BY updated_at DESC",
                (user_id, PLACEHOLDER_TITLE),
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

    async def prune_placeholders(self, user_id: str, keep_thread_id: str, keep_recent: int = 2) -> int:
        """Delete OLD untouched chats (placeholder title), keeping the current one plus the newest few.

        A chat created by merely opening the page carries PLACEHOLDER_TITLE until the first message
        names it. Keeping all of them clutters the history list; deleting them all breaks any other
        tab still sitting on one (its next refresh resumes a deleted id). So keep the current chat and
        the most recent `keep_recent - 1` others, and drop the rest.
        """
        async with self._db() as db:
            cur = await db.execute(
                "SELECT thread_id FROM sessions WHERE user_id=? AND title=? AND thread_id<>? "
                "ORDER BY updated_at DESC",
                (user_id, PLACEHOLDER_TITLE, keep_thread_id),
            )
            ids = [r["thread_id"] for r in await cur.fetchall()]
            drop = ids[max(keep_recent - 1, 0):]
            for tid in drop:
                await db.execute("DELETE FROM steps WHERE thread_id=?", (tid,))
                await db.execute("DELETE FROM sessions WHERE thread_id=?", (tid,))
            await db.commit()
        return len(drop)

    async def delete(self, thread_id: str) -> None:
        async with self._db() as db:
            await db.execute("DELETE FROM steps WHERE thread_id=?", (thread_id,))   # cascade
            await db.execute("DELETE FROM sessions WHERE thread_id=?", (thread_id,))
            await db.commit()

    async def upsert_thread(self, thread_id: str, user_id: str, title: str | None = None) -> None:
        """Create-or-touch a thread by an EXPLICIT thread_id (the Chainlit data layer owns ids)."""
        now = _now()
        async with self._db() as db:
            await db.execute(
                "INSERT INTO sessions (thread_id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET updated_at=excluded.updated_at, "
                "title=COALESCE(excluded.title, sessions.title)",
                (thread_id, user_id, title, now, now),
            )
            await db.commit()

    async def upsert_step(self, thread_id: str, step_id: str, data: dict) -> None:
        """Insert-or-replace one step (opaque StepDict JSON) for a thread."""
        created = data.get("createdAt") or _now()
        async with self._db() as db:
            await db.execute(
                "INSERT INTO steps (thread_id, step_id, created_at, data) VALUES (?,?,?,?) "
                "ON CONFLICT(thread_id, step_id) DO UPDATE SET data=excluded.data, created_at=excluded.created_at",
                (thread_id, step_id, created, json.dumps(data, ensure_ascii=False)),
            )
            await db.commit()

    async def list_steps(self, thread_id: str) -> list[dict]:
        """All steps of a thread (parsed StepDicts), oldest first."""
        async with self._db() as db:
            cur = await db.execute("SELECT data FROM steps WHERE thread_id=? ORDER BY created_at ASC", (thread_id,))
            rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except Exception:
                continue
        return out

    async def delete_step(self, step_id: str) -> None:
        async with self._db() as db:
            await db.execute("DELETE FROM steps WHERE step_id=?", (step_id,))
            await db.commit()


def build_sessions(persistence, pool=None):
    """Factory: SessionsDB (sqlite) or PostgresSessionsDB (postgres; needs a shared pool), chosen by
    persistence.provider. Both expose the SAME interface, so callers never branch. Postgres/psycopg
    is imported lazily -> a sqlite-only environment needs neither installed nor a running Postgres."""
    if (getattr(persistence, "provider", "sqlite") or "sqlite") == "postgres":
        from src.persistence.sessions_pg import PostgresSessionsDB
        if pool is None:
            raise RuntimeError("persistence.provider=postgres needs a pool (build pg_pool first)")
        return PostgresSessionsDB(pool)
    return SessionsDB(persistence.sessions_db_path)
