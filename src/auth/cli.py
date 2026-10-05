"""CLI to manage auth users (P8): create the first account without opening public registration.

Usage:
  python -m src.auth.cli create-user <username>
The password is read from stdin (hidden) so it never lands in shell history. Honors
persistence.provider: creates the user in the SQLite store (provider=sqlite) or the Postgres store
(provider=postgres -> needs Docker Postgres up). Generic tooling -> committed.
"""
from __future__ import annotations

import asyncio
import getpass
import sys

from src.config.settings import load_settings
from src.auth.store import build_user_store
from src.auth.security import hash_password


async def _create_in(store, username: str, pw: str, provider: str) -> None:
    await store.init()
    try:
        user = await store.create_user(username, hash_password(pw))
    except ValueError as exc:
        print(f"failed: {exc}")
        return
    print(f"created user id={user['id']} username={user['username']} (provider={provider})")


async def _create_user(username: str) -> None:
    s = load_settings()
    provider = (s.persistence.provider or "sqlite").lower()
    pw = getpass.getpass(f"password for {username}: ")
    pw2 = getpass.getpass("confirm password: ")
    if not pw:
        print("empty password; aborted")
        return
    if pw != pw2:
        print("passwords do not match; aborted")
        return
    if provider == "postgres":
        if not s.postgres.dsn:
            print("provider=postgres but POSTGRES_DSN is unset; aborted")
            return
        from src.persistence.pg_pool import pg_pool
        async with pg_pool(s.postgres.dsn, s.postgres.pool_min, s.postgres.pool_max) as pool:
            await _create_in(build_user_store(s.persistence, s.auth, pool), username, pw, provider)
    else:
        await _create_in(build_user_store(s.persistence, s.auth), username, pw, provider)


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] != "create-user":
        print("usage: python -m src.auth.cli create-user <username>")
        return
    # psycopg async needs SelectorEventLoop on Windows (it rejects the default ProactorEventLoop).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_create_user(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
