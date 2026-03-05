"""
db.py — Loads the official SQL file into an in-memory SQLite database at startup.

No .db file is created on disk. All modules import `get_connection()` from here
to share the same in-memory database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml

_BASE = Path(__file__).parent
_cfg_path = _BASE / "config.yaml"
with open(_cfg_path, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

_SQL_PATH = _BASE / _cfg["database"]["source_sql"]

# ---------------------------------------------------------------------------
# Load SQL file into a persistent in-memory connection (shared singleton)
# ---------------------------------------------------------------------------

_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Return the shared in-memory SQLite connection (lazy-initialised)."""
    global _connection
    if _connection is None:
        _connection = _load()
    return _connection


def _load() -> sqlite3.Connection:
    if not _SQL_PATH.exists():
        raise FileNotFoundError(
            f"SQL file not found: {_SQL_PATH}\n"
            "Make sure 'cashflo_sample_schema_and_data (1).sql' is in the Problem_B folder."
        )
    sql = _SQL_PATH.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(sql)
    con.commit()
    print(f"[DB] Loaded '{_SQL_PATH.name}' into in-memory SQLite.")
    return con


def reload() -> None:
    """Drop and re-load the in-memory database from the SQL file."""
    global _connection
    if _connection:
        _connection.close()
    _connection = _load()
