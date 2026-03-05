"""
schema_discovery.py — Auto-discovers the schema from the shared in-memory SQLite DB.
"""

from __future__ import annotations
import sqlite3
from typing import Any, Dict, List


def discover(con: sqlite3.Connection) -> Dict[str, Any]:
    """Return full schema: tables → columns, types, FK relationships, sample values."""
    cur = con.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    table_names = [r[0] for r in cur.fetchall()]

    tables: Dict[str, Any] = {}
    for tname in table_names:
        cols = cur.execute(f"PRAGMA table_info('{tname}')").fetchall()
        columns = {}
        for c in cols:
            columns[c[1]] = {           # c[1] = name
                "type":    c[2] or "TEXT",
                "notnull": bool(c[3]),
                "pk":      bool(c[5]),
                "default": c[4],
            }

        fks = cur.execute(f"PRAGMA foreign_key_list('{tname}')").fetchall()
        fk_list = [
            {"from_col": fk[3], "to_table": fk[2], "to_col": fk[4]}
            for fk in fks
        ]

        sample_vals: Dict[str, List] = {}
        for col in columns:
            try:
                rows = cur.execute(
                    f'SELECT DISTINCT "{col}" FROM "{tname}" WHERE "{col}" IS NOT NULL LIMIT 10'
                ).fetchall()
                vals = [r[0] for r in rows]
                if vals and len(vals) <= 10:
                    sample_vals[col] = vals
            except Exception:
                pass

        count = cur.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]

        tables[tname] = {
            "columns":      columns,
            "foreign_keys": fk_list,
            "sample_values": sample_vals,
            "row_count":    count,
        }

    return {"tables": tables, "table_names": table_names}


def schema_as_ddl(con: sqlite3.Connection) -> str:
    """Return all CREATE TABLE statements as a single string (for LLM context)."""
    cur = con.cursor()
    cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
    )
    return "\n\n".join(r[0] for r in cur.fetchall())


def schema_summary(con: sqlite3.Connection) -> str:
    """One-line-per-table summary."""
    schema = discover(con)
    lines = []
    for tname, tinfo in schema["tables"].items():
        cols = ", ".join(tinfo["columns"].keys())
        lines.append(f"  {tname} ({tinfo['row_count']} rows): {cols}")
    return "\n".join(lines)
