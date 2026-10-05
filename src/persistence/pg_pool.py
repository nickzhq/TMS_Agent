"""Shared async Postgres connection pool (P8 batch-2).

One psycopg v3 AsyncConnectionPool serves the checkpointer + sessions + auth stores (rather than
each opening its own connections). Built/opened in the app lifespan, closed on shutdown. psycopg is
imported lazily by callers so a sqlite-only environment needs neither psycopg nor a running Postgres.
Generic infrastructure -> committed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager


@asynccontextmanager
async def pg_pool(dsn: str, min_size: int = 1, max_size: int = 20):
    """Async context manager yielding an opened AsyncConnectionPool (closed on exit).

    autocommit=True: both the LangGraph Postgres checkpointer and our CRUD expect autocommit.
    prepare_threshold=0: avoid prepared-statement caching issues behind poolers / across processes.
    """
    from psycopg_pool import AsyncConnectionPool   # lazy: only needed in postgres mode
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()
