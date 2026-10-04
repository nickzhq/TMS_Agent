"""CLI to manage auth users (P8): create the first account without opening public registration.

Usage:
  python -m src.auth.cli create-user <username>
The password is read from stdin (hidden) so it never lands in shell history.
Generic tooling -> committed.
"""
from __future__ import annotations

import asyncio
import getpass
import sys

from src.config.settings import load_settings
from src.auth.store import UserStore
from src.auth.security import hash_password


async def _create_user(username: str) -> None:
    s = load_settings()
    pw = getpass.getpass(f"password for {username}: ")
    pw2 = getpass.getpass("confirm password: ")
    if not pw:
        print("empty password; aborted")
        return
    if pw != pw2:
        print("passwords do not match; aborted")
        return
    store = UserStore(s.auth.user_db_path)
    await store.init()
    try:
        user = await store.create_user(username, hash_password(pw))
    except ValueError as exc:
        print(f"failed: {exc}")
        return
    print(f"created user id={user['id']} username={user['username']} (user_db={s.auth.user_db_path})")


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] != "create-user":
        print("usage: python -m src.auth.cli create-user <username>")
        return
    asyncio.run(_create_user(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
