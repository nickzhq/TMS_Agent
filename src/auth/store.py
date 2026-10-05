"""User store (P8): SQLite-backed username -> password_hash (batch-1; -> Postgres in a later batch).

Kept separate from sessions_db / the checkpointer (no double-write). Passwords are stored ONLY as
PBKDF2 hashes (see security.py) — never plaintext. Async, one short-lived connection per call
(same pattern as SessionsDB). Generic infrastructure -> committed.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""

_COLS = "id, username, password_hash, created_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserStore:
    """Async CRUD over the users table."""

    def __init__(self, path: str):
        self.path = path
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _db(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(_SCHEMA)        # idempotent guard
            yield db

    async def init(self) -> None:
        """Ensure the schema exists (call once at app startup)."""
        async with self._db():
            pass

    async def create_user(self, username: str, password_hash: str) -> dict:
        """Insert a user -> {id, username, created_at}. Raises ValueError if the username is taken."""
        uid = uuid.uuid4().hex[:12]
        now = _now()
        async with self._db() as db:
            try:
                await db.execute(
                    "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
                    (uid, username, password_hash, now),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"username already exists: {username}")
        return {"id": uid, "username": username, "created_at": now}

    async def get_by_username(self, username: str) -> dict | None:
        async with self._db() as db:
            cur = await db.execute(f"SELECT {_COLS} FROM users WHERE username=?", (username,))
            r = await cur.fetchone()
        return dict(r) if r else None

    async def get_by_id(self, uid: str) -> dict | None:
        async with self._db() as db:
            cur = await db.execute(f"SELECT {_COLS} FROM users WHERE id=?", (uid,))
            r = await cur.fetchone()
        return dict(r) if r else None


def build_user_store(persistence, auth_settings, pool=None):
    """Factory: UserStore (sqlite) or PostgresUserStore (postgres; needs a shared pool), chosen by
    persistence.provider (all three stores share ONE provider). Same interface either way; psycopg
    is imported lazily -> a sqlite-only environment needs neither installed."""
    if (getattr(persistence, "provider", "sqlite") or "sqlite") == "postgres":
        from src.auth.store_pg import PostgresUserStore
        if pool is None:
            raise RuntimeError("persistence.provider=postgres needs a pool (build pg_pool first)")
        return PostgresUserStore(pool)
    return UserStore(auth_settings.user_db_path)
