"""Postgres-backed user store (P8 batch-2) — SAME interface as UserStore (sqlite).

Uses the shared psycopg async pool. Passwords are stored ONLY as PBKDF2 hashes (see auth/security)
— never plaintext. Generic infrastructure -> committed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("estimate.auth.store_pg")

_DDL = """CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL)"""
_COLS = "id, username, password_hash, created_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresUserStore:
    """Async CRUD over the users table via a shared psycopg pool (interface == UserStore)."""

    def __init__(self, pool):
        self.pool = pool

    async def init(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(_DDL)

    async def create_user(self, username: str, password_hash: str) -> dict:
        """Insert a user -> {id, username, created_at}. Raises ValueError if the username is taken."""
        uid = uuid.uuid4().hex[:12]
        now = _now()
        async with self.pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at) VALUES (%s,%s,%s,%s)",
                    (uid, username, password_hash, now))
            except psycopg.errors.UniqueViolation:
                raise ValueError(f"username already exists: {username}")
        return {"id": uid, "username": username, "created_at": now}

    async def get_by_username(self, username: str) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(f"SELECT {_COLS} FROM users WHERE username=%s", (username,))
            return await cur.fetchone()

    async def get_by_id(self, uid: str) -> dict | None:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(f"SELECT {_COLS} FROM users WHERE id=%s", (uid,))
            return await cur.fetchone()
