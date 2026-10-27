"""CLI to manage auth users (P8): create accounts without opening public registration.

Usage:
  python -m src.auth.cli create-user <username> [--role viewer|finance|admin] [--password-stdin]
  python -m src.auth.cli set-role <username> <viewer|finance|admin>
  python -m src.auth.cli list-users

The password is read from the console (hidden) so it never lands in shell history; with
`--password-stdin` it is read as ONE line from stdin instead (Windows getpass talks to the console
directly, so piping into the interactive prompt just blocks).

Roles (S1.5): viewer < finance < admin, default viewer. Roles live in the DB and are read per
session (deliberately NOT baked into the JWT), so granting a role takes effect immediately without
forcing the user to log in again.

Honors persistence.provider: the SQLite store (provider=sqlite) or the Postgres store
(provider=postgres -> needs Docker Postgres up). Generic tooling -> committed.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from contextlib import asynccontextmanager

from src.auth.security import hash_password
from src.auth.store import ROLES, build_user_store
from src.config.settings import load_settings


@asynccontextmanager
async def _store():
    """按 persistence.provider 打开用户库（provider=postgres 时用共享连接池）。"""
    s = load_settings()
    provider = (s.persistence.provider or "sqlite").lower()
    if provider == "postgres":
        if not s.postgres.dsn:
            raise RuntimeError("provider=postgres but POSTGRES_DSN is unset")
        from src.persistence.pg_pool import pg_pool
        async with pg_pool(s.postgres.dsn, s.postgres.pool_min, s.postgres.pool_max) as pool:
            store = build_user_store(s.persistence, s.auth, pool)
            await store.init()
            yield store, provider
    else:
        store = build_user_store(s.persistence, s.auth)
        await store.init()
        yield store, provider


async def _create_user(username: str, role: str, password_stdin: bool = False) -> int:
    if password_stdin:
        # 非交互（初始化/自动化）：从 stdin 读一行，不调 getpass ——
        # Windows 下 getpass 直连控制台，管道输入会一直阻塞。
        pw = sys.stdin.readline().rstrip("\r\n")
        pw2 = pw
    else:
        pw = getpass.getpass(f"password for {username}: ")
        pw2 = getpass.getpass("confirm password: ")
    if not pw:
        print("empty password; aborted")
        return 1
    if pw != pw2:
        print("passwords do not match; aborted")
        return 1
    async with _store() as (store, provider):
        try:
            user = await store.create_user(username, hash_password(pw), role=role)
        except ValueError as exc:
            print(f"failed: {exc}")
            return 1
    print(f"created user id={user['id']} username={user['username']} "
          f"role={user['role']} (provider={provider})")
    return 0


async def _set_role(username: str, role: str) -> int:
    async with _store() as (store, provider):
        try:
            user = await store.set_role(username, role)
        except ValueError as exc:
            print(f"failed: {exc}")
            return 1
        if user is None:
            print(f"failed: user not found: {username} (provider={provider})")
            return 1
    print(f"updated username={user['username']} role={user['role']} (provider={provider})")
    return 0


async def _list_users() -> int:
    async with _store() as (store, provider):
        users = await store.list_users()
    print(f"{len(users)} user(s), provider={provider}")
    print(f"{'username':<16}{'role':<10}{'id':<16}created_at")
    for u in users:
        print(f"{u['username']:<16}{u['role']:<10}{u['id']:<16}{u['created_at']}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.auth.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-user", help="新建账号")
    p_create.add_argument("username")
    p_create.add_argument("--role", default="viewer", choices=list(ROLES))
    p_create.add_argument("--password-stdin", action="store_true",
                          help="从 stdin 读一行密码（非交互初始化用；不进 shell 历史）")

    p_role = sub.add_parser("set-role", help="修改账号角色")
    p_role.add_argument("username")
    p_role.add_argument("role", choices=list(ROLES))

    sub.add_parser("list-users", help="列出账号与角色")

    args = ap.parse_args(argv)

    # psycopg async needs SelectorEventLoop on Windows (it rejects the default ProactorEventLoop).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if args.cmd == "create-user":
        return asyncio.run(_create_user(args.username, args.role, args.password_stdin))
    if args.cmd == "set-role":
        return asyncio.run(_set_role(args.username, args.role))
    return asyncio.run(_list_users())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
