"""LangGraph checkpointer on SQLite (stable on-disk path) — P7 persistence.

Graph state (per thread_id) is saved to a SQLite file under data/ (gitignore), NOT /tmp,
so conversation state survives process restarts (history restore + break-point resume).

Two flavors:
  - sync  SqliteSaver       -> graph.invoke / get_state   (tests, sync callers)
  - async AsyncSqliteSaver  -> graph.ainvoke / astream    (Chainlit async runtime, batch 2)

Generic infrastructure (no business logic) -> committed to the repo.
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def _ensure_parent(path: str) -> str:
    """Create the parent dir (e.g. data/) if missing; return the path unchanged."""
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _setup(saver):
    """Create checkpoint tables if the saver exposes setup() (idempotent across versions)."""
    setup = getattr(saver, "setup", None)
    if callable(setup):
        setup()
    return saver


def build_checkpointer(path: str) -> SqliteSaver:
    """Long-lived sync SqliteSaver on a stable file (app-scoped; caller closes on shutdown).

    check_same_thread=False: LangGraph offloads sync nodes to a threadpool, so the
    connection may be touched from different threads.
    """
    _ensure_parent(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    return _setup(SqliteSaver(conn))


@contextmanager
def sqlite_checkpointer(path: str):
    """Context-managed sync SqliteSaver (opens + closes the connection). Short-lived use / tests."""
    _ensure_parent(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        yield _setup(SqliteSaver(conn))
    finally:
        conn.close()


@asynccontextmanager
async def async_sqlite_checkpointer(path: str):
    """Context-managed AsyncSqliteSaver for Chainlit ainvoke/astream (batch 2)."""
    _ensure_parent(path)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield saver
