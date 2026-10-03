"""Database client: pyodbc direct connection to SQL Server (read-only account), runs standard T-SQL.

The database is accessed via pyodbc directly rather than an MCP query tool, because such tools
often reject CTE / DECLARE / multi-statement scripts, while the SQL assets here are full T-SQL.
pyodbc runs them as-is (zero rewrite; safety ensured by a read-only account, db_datareader).
The map service still uses MCP (see tools/amap.py).

Prerequisite: ODBC Driver for SQL Server must be installed on the system.
"""
from __future__ import annotations

import re

import pyodbc

from src.config.settings import DbSettings


def build_connection_string(db: DbSettings, driver: str = "ODBC Driver 18 for SQL Server") -> str:
    """Assemble the pyodbc connection string (read-only account).

    Prefers db.connection_string (full string). If DRIVER is missing it is prepended, and
    node mssql (tedious) style keywords/booleans are normalized to what ODBC Driver 18 accepts
    (otherwise encryption negotiation fails, sqlstate 08001). Falls back to assembling from the
    host/database/user/password fields. The driver name depends on the installed ODBC Driver version.
    """
    cs = db.connection_string.strip()
    if cs:
        # a full connection string may lack DRIVER (some MCP servers handle it internally); pyodbc needs it
        if "driver=" not in cs.lower():
            cs = f"DRIVER={{{driver}}};" + cs
        # node tedious / ADO.NET style keywords -> ODBC standard (User Id->UID, Password->PWD), else login fails (28000)
        cs = re.sub(r"\bUser Id\s*=", "UID=", cs, flags=re.IGNORECASE)
        cs = re.sub(r"\bPassword\s*=", "PWD=", cs, flags=re.IGNORECASE)
        # node mssql (tedious) uses Encrypt=False/True; ODBC Driver 18 only accepts no/yes -> normalize
        cs = re.sub(r"(Encrypt\s*=\s*)False\b", r"\1no", cs, flags=re.IGNORECASE)
        cs = re.sub(r"(Encrypt\s*=\s*)True\b", r"\1yes", cs, flags=re.IGNORECASE)
        cs = re.sub(r"(TrustServerCertificate\s*=\s*)True\b", r"\1yes", cs, flags=re.IGNORECASE)
        cs = re.sub(r"(TrustServerCertificate\s*=\s*)False\b", r"\1no", cs, flags=re.IGNORECASE)
        return cs
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={db.host};"
        f"DATABASE={db.database};"
        f"UID={db.user};"
        f"PWD={db.password};"
        f"TrustServerCertificate=yes;"
    )


class MssqlClient:
    """pyodbc wrapper: runs standard T-SQL (DECLARE / CTE / multi-statement) and returns result sets."""

    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self._conn = None

    def __enter__(self) -> "MssqlClient":
        self._conn = pyodbc.connect(self.connection_string, autocommit=True)
        return self

    def __exit__(self, *exc) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run SQL and return the **first** result set as list[dict] (column -> value)."""
        self._ensure_conn()
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return self._fetch_current(cur)

    def query_all_sets(self, sql: str, params: tuple = ()) -> list[list[dict]]:
        """Run a script with multiple SELECTs and return all **non-empty** result sets."""
        self._ensure_conn()
        cur = self._conn.cursor()
        cur.execute(sql, params)
        sets: list[list[dict]] = []
        while True:
            rows = self._fetch_current(cur)
            if rows:
                sets.append(rows)
            if not cur.nextset():
                break
        return sets

    def _ensure_conn(self) -> None:
        # Lazily (re)open. A cached client can outlive its connection: e.g. the web session's
        # on_chat_end fires on a transient websocket drop and closes the connection, while a
        # compiled graph still references this client. Reopening on demand keeps a resumed query
        # from failing on a closed connection (read-only account; the `with` __exit__ still frees it).
        if self._conn is None:
            self._conn = pyodbc.connect(self.connection_string, autocommit=True)

    def is_open(self) -> bool:
        """True if a connection is currently open (health check for callers that cache the client)."""
        return self._conn is not None

    @staticmethod
    def _fetch_current(cur) -> list[dict]:
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
