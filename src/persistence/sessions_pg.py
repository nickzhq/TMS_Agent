"""Postgres-backed session metadata store (P8 batch-2) — SAME interface as SessionsDB (sqlite).

Uses the shared psycopg async pool (see pg_pool). SQL differs from sqlite (%s placeholders, dict_row
results), but ISO-timestamp strings + JSON step blobs are stored as TEXT so the returned dicts are
IDENTICAL to the sqlite store — callers never branch on provider. autocommit is on at the pool, so
writes need no explicit commit. Generic infrastructure -> committed.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from psycopg.rows import dict_row

logger = logging.getLogger("estimate.persistence.sessions_pg")

_DDL = [
    """CREATE TABLE IF NOT EXISTS sessions (
        thread_id  TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        title      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS steps (
        thread_id  TEXT NOT NULL,
        step_id    TEXT NOT NULL,
        created_at TEXT,
        data       TEXT NOT NULL,
        PRIMARY KEY (thread_id, step_id))""",
]
_COLS = "thread_id, user_id, title, created_at, updated_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresSessionsDB:
    """Async CRUD over the session tables via a shared psycopg pool (interface == SessionsDB)."""

    def __init__(self, pool):
        self.pool = pool

    async def init(self) -> None:
        async with self.pool.connection() as conn:
            for ddl in _DDL:
                await conn.execute(ddl)

    async def create(self, user_id: str, title: str | None = None, session_id: str | None = None) -> str:
        from src.persistence.sessions_db import make_thread_id
        thread_id = make_thread_id(user_id, session_id)
        now = _now()
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO sessions (thread_id, user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s,%s)",
                (thread_id, user_id, title, now, now))
        return thread_id

    async def list(self, user_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                f"SELECT {_COLS} FROM sessions WHERE user_id=%s ORDER BY updated_at DESC", (user_id,))
            return await cur.fetchall()

    async def get(self, thread_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(f"SELECT {_COLS} FROM sessions WHERE thread_id=%s", (thread_id,))
            return await cur.fetchone()

    async def rename(self, thread_id: str, title: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET title=%s, updated_at=%s WHERE thread_id=%s", (title, _now(), thread_id))

    async def touch(self, thread_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE sessions SET updated_at=%s WHERE thread_id=%s", (_now(), thread_id))

    async def delete(self, thread_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM steps WHERE thread_id=%s", (thread_id,))       # cascade
            await conn.execute("DELETE FROM sessions WHERE thread_id=%s", (thread_id,))

    async def upsert_thread(self, thread_id: str, user_id: str, title: str | None = None) -> None:
        now = _now()
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO sessions (thread_id, user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (thread_id) DO UPDATE SET updated_at=EXCLUDED.updated_at, "
                "title=COALESCE(EXCLUDED.title, sessions.title)",
                (thread_id, user_id, title, now, now))

    async def upsert_step(self, thread_id: str, step_id: str, data: dict) -> None:
        created = data.get("createdAt") or _now()
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO steps (thread_id, step_id, created_at, data) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (thread_id, step_id) DO UPDATE SET data=EXCLUDED.data, created_at=EXCLUDED.created_at",
                (thread_id, step_id, created, json.dumps(data, ensure_ascii=False)))

    async def list_steps(self, thread_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(
                "SELECT data FROM steps WHERE thread_id=%s ORDER BY created_at ASC", (thread_id,))
            rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except Exception:
                continue
        return out

    async def delete_step(self, step_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM steps WHERE step_id=%s", (step_id,))
