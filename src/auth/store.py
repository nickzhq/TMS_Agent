"""User store (P8): SQLite-backed username -> password_hash + role (batch-1; -> Postgres later).

Kept separate from sessions_db / the checkpointer (no double-write). Passwords are stored ONLY as
PBKDF2 hashes (see security.py) — never plaintext. Async, one short-lived connection per call
(same pattern as SessionsDB). Generic infrastructure -> committed.

role（S1.5 新增）：viewer / finance / admin，默认 viewer。
- 老库没有该列 -> `_ensure_role_column()` 自动 ALTER TABLE 补上，已有账号全部变 viewer；
- 角色**不写进 JWT**，每次从库里取 -> 改完权限立即生效，不用重新登录。
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
    created_at    TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer'
)
"""

_COLS = "id, username, password_hash, created_at, role"

ROLES = ("viewer", "finance", "admin")
DEFAULT_ROLE = "viewer"


def _check_role(role: str) -> str:
    r = (role or DEFAULT_ROLE).strip().lower()
    if r not in ROLES:
        raise ValueError(f"unknown role: {role} (allowed: {', '.join(ROLES)})")
    return r


async def _ensure_role_column(db) -> None:
    """老库补 role 列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。"""
    cur = await db.execute("PRAGMA table_info(users)")
    cols = {row["name"] for row in await cur.fetchall()}
    if "role" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
        await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UserStore:
    """Async CRUD over the users table."""

    def __init__(self, path: str):
        self.path = path
        self._migrated = False
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def _db(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(_SCHEMA)        # idempotent guard
            if not self._migrated:
                await _ensure_role_column(db)
                self._migrated = True
            yield db

    async def init(self) -> None:
        """Ensure the schema exists (call once at app startup)."""
        async with self._db():
            pass

    async def create_user(self, username: str, password_hash: str,
                          role: str = DEFAULT_ROLE) -> dict:
        """Insert a user -> {id, username, created_at, role}. Raises ValueError if taken."""
        uid = uuid.uuid4().hex[:12]
        now = _now()
        role = _check_role(role)
        async with self._db() as db:
            try:
                await db.execute(
                    "INSERT INTO users (id, username, password_hash, created_at, role) "
                    "VALUES (?,?,?,?,?)",
                    (uid, username, password_hash, now, role),
                )
                await db.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"username already exists: {username}")
        return {"id": uid, "username": username, "created_at": now, "role": role}

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

    async def set_role(self, username: str, role: str) -> dict | None:
        """改角色；账号不存在返回 None（调用方负责报错，不静默创建）。"""
        role = _check_role(role)
        async with self._db() as db:
            cur = await db.execute("UPDATE users SET role=? WHERE username=?", (role, username))
            await db.commit()
            if cur.rowcount == 0:
                return None
        return await self.get_by_username(username)

    async def list_users(self) -> list[dict]:
        async with self._db() as db:
            cur = await db.execute(f"SELECT {_COLS} FROM users ORDER BY created_at")
            return [dict(r) for r in await cur.fetchall()]


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
