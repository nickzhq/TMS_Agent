"""Postgres-backed user store (P8 batch-2) — SAME interface as UserStore (sqlite).

Uses the shared psycopg async pool. Passwords are stored ONLY as PBKDF2 hashes (see auth/security)
— never plaintext. Generic infrastructure -> committed.

role（S1.5 新增）：viewer / finance / admin，默认 viewer；init() 会给老库补列。
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
    created_at    TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer')"""
_MIGRATE_ROLE = "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'viewer'"
_COLS = "id, username, password_hash, created_at, role"

ROLES = ("viewer", "finance", "admin")
DEFAULT_ROLE = "viewer"


def _check_role(role: str) -> str:
    r = (role or DEFAULT_ROLE).strip().lower()
    if r not in ROLES:
        raise ValueError(f"unknown role: {role} (allowed: {', '.join(ROLES)})")
    return r


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PostgresUserStore:
    """Async CRUD over the users table via a shared psycopg pool (interface == UserStore)."""

    def __init__(self, pool):
        self.pool = pool

    async def init(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(_DDL)
            await conn.execute(_MIGRATE_ROLE)     # 老库补 role 列（Postgres 支持 IF NOT EXISTS）

    async def create_user(self, username: str, password_hash: str,
                          role: str = DEFAULT_ROLE) -> dict:
        """Insert a user -> {id, username, created_at, role}. Raises ValueError if taken."""
        uid = uuid.uuid4().hex[:12]
        now = _now()
        role = _check_role(role)
        async with self.pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at, role) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (uid, username, password_hash, now, role))
            except psycopg.errors.UniqueViolation:
                raise ValueError(f"username already exists: {username}")
        return {"id": uid, "username": username, "created_at": now, "role": role}

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

    async def set_role(self, username: str, role: str) -> dict | None:
        """改角色；账号不存在返回 None（调用方负责报错，不静默创建）。"""
        role = _check_role(role)
        async with self.pool.connection() as conn:
            cur = await conn.execute("UPDATE users SET role=%s WHERE username=%s", (role, username))
            if cur.rowcount == 0:
                return None
        return await self.get_by_username(username)

    async def list_users(self) -> list[dict]:
        async with self.pool.connection() as conn:
            conn.row_factory = dict_row
            cur = await conn.execute(f"SELECT {_COLS} FROM users ORDER BY created_at")
            return list(await cur.fetchall())
