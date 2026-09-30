"""SQL loader: read an agent's SQL script (standard T-SQL) + inject runtime parameters.

The database is accessed via pyodbc, so SQL scripts run as-is (DECLARE / CTE / comments /
semicolons preserved) with no flattening. The loader only substitutes the default values in
DECLARE statements with runtime parameter values.
"""
from __future__ import annotations

import re
from pathlib import Path

# agents/ directory (project root, sibling of src/)
_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"


def load_sql(agent: str, filename: str) -> str:
    """Read an agent's raw SQL script (standard T-SQL, runnable in SSMS).

    agent:    agent sub-directory name
    filename: SQL file name
    """
    path = _AGENTS_DIR / agent / filename
    return path.read_text(encoding="utf-8")


def set_params(sql: str, params: dict) -> str:
    """Replace the default value in `DECLARE @name <type> = <default>` with the runtime value.

    Only the default value on DECLARE lines is changed; DECLARE / CTE / comments / semicolons
    are preserved (pyodbc runs standard T-SQL).
    params: key = parameter name (without @), value = SQL literal string (e.g. "7" / "117.8" / "NULL").
    DECLARE statements not present in params keep their original defaults.
    """
    def _repl(m: re.Match) -> str:
        name = m.group(1)
        if name in params:
            return f"DECLARE @{name}{m.group(2)}= {params[name]};"
        return m.group(0)

    # match: DECLARE @name <type> = <default>;  (type may contain parentheses like decimal(10,1))
    return re.sub(r"DECLARE\s+@(\w+)(\s+[\w(),]+\s*)=\s*[^;]+;", _repl, sql, flags=re.IGNORECASE)


def build_script(agent: str, filename: str, params: dict | None = None) -> str:
    """Read SQL + inject parameters -> a standard T-SQL script executable by pyodbc."""
    sql = load_sql(agent, filename)
    return set_params(sql, params or {})
