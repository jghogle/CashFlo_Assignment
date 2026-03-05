"""
main.py — CLI entry point for the NLP-to-SQL pipeline.

Usage:
  python main.py                          # interactive REPL
  python main.py --question "..."         # single question
  python main.py --setup-db              # (re)create the database from SQL file
  python main.py --schema                # print schema summary
  python main.py --discover-semantic     # auto-generate a draft semantic layer
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

_CONFIG_PATH = BASE / "config.yaml"


def _load_config():
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _print_result(result) -> None:
    print(f"\n{'─'*60}")
    print(f"  Question   : {result.question}")
    print(f"  SQL        :\n{result.sql}")
    if result.assumptions:
        print(f"  Assumptions: {result.assumptions}")
    if result.is_ambiguous:
        print(f"  ⚠ Ambiguous: {result.clarification_question}")
    if result.error:
        print(f"  ❌ Error    : {result.error}")
    else:
        print(f"  Rows       : {result.row_count}")
        if result.columns:
            header = " | ".join(f"{c:>18}" for c in result.columns[:6])
            print(f"\n  {header}")
            print(f"  {'─'*len(header)}")
            for row in result.rows[:10]:
                line = " | ".join(f"{str(v):>18}" for v in row[:6])
                print(f"  {line}")
            if result.row_count > 10:
                print(f"  … {result.row_count - 10} more rows")
        print(f"\n  📝 {result.explanation}")
        print(f"  📊 Suggested viz: {result.viz_type.upper()}")
    print(f"{'─'*60}\n")


def cmd_schema(cfg) -> None:
    from db import get_connection
    from schema_discovery import schema_summary
    print("\nSchema Summary:")
    print(schema_summary(get_connection()))


def cmd_question(cfg, question: str) -> None:
    from sql_generator import SQLGenerator
    from query_cache import QueryCache

    sem_path = BASE / cfg["semantic_layer"]["path"]
    cache    = QueryCache(BASE / cfg["cache"]["path"])

    hit = cache.lookup(question)
    if hit:
        print(f"\n⚡ Cache hit (similarity={hit['similarity']:.0%}) — reusing SQL:")
        print(f"  {hit['sql']}")
        print(f"  {hit['explanation']}")
        return

    gen = SQLGenerator(api_key=cfg["llm"]["api_key"], model=cfg["llm"]["model"],
                       semantic_path=sem_path)
    result = gen.generate(question)
    _print_result(result)

    if not result.error:
        cache.store(question, result.sql, result.rows[:5], result.explanation, result.assumptions)


def cmd_repl(cfg) -> None:
    from sql_generator import SQLGenerator
    from query_cache import QueryCache

    sem_path = BASE / cfg["semantic_layer"]["path"]
    cache    = QueryCache(BASE / cfg["cache"]["path"])
    gen      = SQLGenerator(cfg["llm"]["api_key"], cfg["llm"]["model"], sem_path)

    print("\n🤖 Cashflo NLP-to-SQL REPL  (type 'exit' to quit)\n")
    history = []

    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q"):
            break

        hit = cache.lookup(q)
        if hit:
            print(f"⚡ Cache hit ({hit['similarity']:.0%}): {hit['explanation']}")
            print(f"   SQL: {hit['sql']}\n")
            continue

        result = gen.generate(q, conversation_history=history)
        _print_result(result)

        history.append({"role": "user",      "content": q})
        history.append({"role": "assistant",  "content": result.explanation})

        if not result.error:
            cache.store(q, result.sql, result.rows[:5], result.explanation, result.assumptions)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cashflo NLP-to-SQL CLI")
    parser.add_argument("--question", "-q", help="Run a single question")
    parser.add_argument("--schema",   action="store_true", help="Print schema summary")
    parser.add_argument("--setup-db", action="store_true", help="(Re)create the database")
    args = parser.parse_args()

    cfg = _load_config()

    if args.setup_db:
        import subprocess
        sql_file = BASE / cfg["database"]["source_sql"]
        db_path  = BASE / cfg["database"]["path"]
        if not sql_file.exists():
            print(f"SQL file not found: {sql_file}")
            sys.exit(1)
        if db_path.exists():
            db_path.unlink()
        with open(sql_file, "r") as f:
            sql = f.read()
        import sqlite3
        con = sqlite3.connect(str(db_path))
        con.executescript(sql)
        con.close()
        print(f"✓ Database loaded from {sql_file.name} → {db_path}")
        return

    if args.schema:
        cmd_schema(cfg)
        return

    if args.question:
        cmd_question(cfg, args.question)
        return

    cmd_repl(cfg)


if __name__ == "__main__":
    main()
