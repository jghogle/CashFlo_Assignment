"""
sql_generator.py — NLP-to-SQL engine using Claude + semantic layer.

Pipeline per question:
  1. Resolve synonyms and metrics from semantic layer
  2. Build system prompt (schema DDL + semantic layer summary)
  3. Send to Claude → get structured JSON response
  4. Validate SQL syntax and table/column names
  5. Execute against SQLite
  6. Suggest visualization type based on result shape
  7. Return full QueryResult
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
import yaml

from db import get_connection
from schema_discovery import schema_as_ddl, discover


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    question: str
    sql: str
    columns: List[str]
    rows: List[List[Any]]
    explanation: str
    assumptions: str
    is_ambiguous: bool
    clarification_question: str
    clarification_options: List[str]   # clickable quick-reply options
    viz_type: str               # bar | line | pie | table | scalar | none
    viz_config: Dict[str, Any]
    error: str
    cache_hit: bool = False
    cache_similarity: float = 0.0
    row_count: int = 0


# ---------------------------------------------------------------------------
# Visualization suggestion
# ---------------------------------------------------------------------------

def _suggest_viz(columns: List[str], rows: List[List]) -> tuple[str, Dict]:
    """Heuristic: pick the best chart type for the result shape."""
    if not rows:
        return "none", {}
    if len(rows) == 1 and len(columns) == 1:
        return "scalar", {"value": rows[0][0], "label": columns[0]}

    col_lower = [c.lower() for c in columns]

    # Time series: one date col + one numeric col
    date_cols = [c for c in col_lower if any(k in c for k in ("date", "month", "year", "quarter", "week", "period"))]
    num_cols  = [c for c in col_lower if any(k in c for k in ("total", "amount", "sum", "count", "avg", "value", "revenue", "payment", "rank"))]

    if date_cols and num_cols and len(rows) > 1:
        return "line", {"x": date_cols[0], "y": num_cols[0]}

    # Ranking / top-N: label + one numeric
    if len(columns) == 2 and len(rows) > 1:
        if any(k in col_lower[1] for k in ("total", "amount", "sum", "count", "value", "rank")):
            return "bar", {"x": columns[0], "y": columns[1]}

    # Category distribution (small result set)
    if len(columns) == 2 and 2 <= len(rows) <= 10:
        return "pie", {"label": columns[0], "value": columns[1]}

    # Default: table
    return "table", {}


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(ddl: str, semantic: Dict) -> str:
    # Flatten metrics
    metrics_text = ""
    for name, m in semantic.get("metrics", {}).items():
        metrics_text += f"  - {name}: {m.get('description','')} → SQL: {m.get('sql','')}\n"

    # Flatten synonyms
    syn_text = ""
    for word, meaning in semantic.get("synonyms", {}).items():
        syn_text += f"  '{word}' → {meaning}\n"

    # Relationships
    rel_text = ""
    for r in semantic.get("relationships", []):
        rel_text += f"  {r['from']} JOIN {r['to']} ON {r['join']}\n"

    return textwrap.dedent(f"""
    You are an expert SQL assistant for a B2B Accounts Payable platform called Cashflo.
    Your job is to convert natural-language questions into correct SQLite SQL queries.

    ════════════════════════════════════════
    DATABASE SCHEMA (SQLite)
    ════════════════════════════════════════
    {ddl}

    ════════════════════════════════════════
    SEMANTIC LAYER
    ════════════════════════════════════════
    SYNONYMS (resolve before generating SQL):
    {syn_text}

    BUSINESS METRICS:
    {metrics_text}

    JOIN PATHS (use these for multi-table queries):
    {rel_text}

    TEMPORAL NOTES:
    - Use DATE('now') for today in SQLite.
    - "last month" → strftime('%Y-%m', DATE('now','-1 month'))
    - "this month"  → strftime('%Y-%m', DATE('now'))
    - "last quarter" → compute as 3-month period ending before current quarter
    - "this year" / "this FY" → strftime('%Y', DATE('now')) or April–March for Indian FY
    - "overdue" → due_date < DATE('now') AND status NOT IN ('paid','rejected')

    ════════════════════════════════════════
    RULES
    ════════════════════════════════════════
    1. Always return ONLY valid SQLite SQL — no CTEs unless necessary.
    2. Prefer readable aliases (v for vendors, i for invoices, p for payments, etc.).
    3. For window functions (RANK, LAG, SUM OVER), use SQLite-compatible syntax.
    4. Limit results to 100 rows unless the user specifies otherwise.
    5. Never use column names that don't exist in the schema.
    6. Use ROUND(..., 2) for all monetary amounts.
    7. VENDOR RATING SORT ORDER: vendors.rating is a letter grade where A=best and D=worst.
       - "top/best vendors by rating" or "highest rated" → ORDER BY rating ASC (A first)
       - "worst/lowest rated vendors"                    → ORDER BY rating DESC (D first)
       - "sort by rating" alone (no top/best/worst qualifier) → ORDER BY rating ASC (best first)
       Use CASE WHEN rating='A' THEN 1 WHEN rating='B' THEN 2 WHEN rating='C' THEN 3 WHEN rating='D' THEN 4 END
       for explicit numeric ordering when needed.

    ════════════════════════════════════════
    AMBIGUITY HANDLING
    ════════════════════════════════════════
    A question is ambiguous when the metric or dimension to use is genuinely unclear
    and multiple valid interpretations exist that would produce different SQL.

    Examples of AMBIGUOUS questions:
    - "Show me the top vendors"          → top by what? (total value / invoice count / rating)
    - "Who are our best customers?"      → best by what? (revenue / on-time payment / volume)
    - "Which products are most popular?" → by quantity ordered / invoice count / revenue?

    Examples of NON-AMBIGUOUS questions (just state your assumption):
    - "List all vendors on the watchlist"  → clear, no assumption needed
    - "What was revenue last quarter?"     → clear metric (SUM of paid invoices)
    - "Show unpaid invoices > 1L"          → clear filter

    When a question IS ambiguous:
    - Set is_ambiguous = true
    - Ask a short, specific clarification_question
    - List 2-4 concrete clarification_options (these become clickable buttons)
    - STILL generate the most reasonable SQL as a default
    - State your default assumption explicitly

    When a question is NOT ambiguous:
    - Set is_ambiguous = false
    - Leave clarification_question and clarification_options empty
    - If you made any minor assumptions, describe them in the assumptions field

    ════════════════════════════════════════
    OUTPUT FORMAT (strict JSON — no markdown)
    ════════════════════════════════════════
    {{
      "sql": "<single SQL statement>",
      "explanation": "<plain-English explanation of what the SQL does>",
      "assumptions": "<assumptions made, especially for ambiguous queries>",
      "is_ambiguous": <true | false>,
      "clarification_question": "<short question to the user if ambiguous, else empty string>",
      "clarification_options": ["<option 1>", "<option 2>", "<option 3>"]
    }}

    CRITICAL: All string values must be on a single line. Do NOT include literal
    newline characters inside JSON string values. Use a space instead of \\n.
    Return ONLY the JSON object — no prose, no markdown, no code fences.
    """).strip()


# ---------------------------------------------------------------------------
# Rule-based ambiguity detector (backstop for LLM inconsistency)
# ---------------------------------------------------------------------------

_AMBIGUITY_RULES: List[Dict] = [
    {
        "pattern": re.compile(
            r'\b(top|best|leading|highest[- ]rated?|greatest)\b.{0,30}\b(vendor|supplier)s?\b',
            re.I
        ),
        "question": "What metric should I use to rank vendors?",
        "options": [
            "Top vendors by total invoice value",
            "Top vendors by invoice count",
            "Top vendors by vendor rating (A–D)",
            "Top vendors by on-time payment rate",
        ],
    },
    {
        "pattern": re.compile(
            r'\b(top|best|most popular|highest[- ]value?d?|best[- ]selling)\b.{0,30}\b(product|item)s?\b',
            re.I
        ),
        "question": "What metric should I use to rank products?",
        "options": [
            "Top products by total invoiced value",
            "Top products by quantity ordered",
            "Top products by invoice count",
        ],
    },
    {
        "pattern": re.compile(
            r'\b(top|best|most valuable|biggest)\b.{0,30}\b(customer|client|buyer|compan)(?:ies|y|s)?\b',
            re.I
        ),
        "question": "What metric should I use to rank customers?",
        "options": [
            "Customers by total invoice value",
            "Customers by invoice count",
            "Customers by on-time payment rate",
        ],
    },
    {
        "pattern": re.compile(
            r'\b(top|most expensive|costliest|highest[- ]value?d?)\b.{0,20}\b(invoice|bill)s?\b',
            re.I
        ),
        "question": "Do you want the most expensive individual invoices, or vendors by total value?",
        "options": [
            "Most expensive individual invoices",
            "Vendors with highest total invoice value",
        ],
    },
    {
        "pattern": re.compile(
            r'\b(most|top|best)\b.{0,30}\b(depart?ment)s?\b',
            re.I
        ),
        "question": "What metric should I use to rank departments?",
        "options": [
            "Departments by total invoice spend",
            "Departments by number of purchase orders",
            "Departments by budget utilisation",
        ],
    },
]


def _detect_ambiguity(question: str):
    """Return (clarification_question, options) if question is a known ambiguous pattern, else None."""
    for rule in _AMBIGUITY_RULES:
        if rule["pattern"].search(question):
            return rule["question"], rule["options"]
    return None


# ---------------------------------------------------------------------------
# SQL validator
# ---------------------------------------------------------------------------

def _validate_sql(sql: str, schema: Dict) -> Optional[str]:
    """Light validation: check referenced tables exist. Returns error string or None."""
    table_names = set(schema.get("table_names", []))
    # Extract table names from SQL (rough regex)
    referenced = set(re.findall(r'\bFROM\s+(\w+)|\bJOIN\s+(\w+)', sql, re.IGNORECASE))
    referenced_flat = {t for pair in referenced for t in pair if t}
    unknown = referenced_flat - table_names
    if unknown:
        return f"Unknown table(s) in SQL: {unknown}"
    return None


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class SQLGenerator:
    def __init__(
        self,
        api_key: str,
        model: str,
        semantic_path: str | Path,
    ):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.con = get_connection()          # shared in-memory DB
        self.ddl = schema_as_ddl(self.con)
        self.schema = discover(self.con)

        with open(semantic_path, encoding="utf-8") as f:
            self.semantic = yaml.safe_load(f)

        self.system_prompt = _build_system_prompt(self.ddl, self.semantic)

    # ── public ──────────────────────────────────────────────────────────────

    def generate(
        self,
        question: str,
        conversation_history: Optional[List[Dict]] = None,
    ) -> QueryResult:
        """Full pipeline: NL → SQL → execute → explain → viz."""

        messages = []

        # Include prior conversation turns for multi-turn support
        if conversation_history:
            for turn in conversation_history[-10:]:  # cap at last 10 turns
                messages.append({"role": turn["role"], "content": turn["content"]})

        messages.append({"role": "user", "content": question})

        parsed, messages = self._call_claude(messages)
        if "error" in parsed:
            return self._error_result(question, parsed["error"], raw_sql=parsed.get("sql", ""))

        sql                   = parsed.get("sql", "").strip()
        explanation           = parsed.get("explanation", "")
        assumptions           = parsed.get("assumptions", "")
        is_ambiguous          = bool(parsed.get("is_ambiguous", False))
        clarification         = parsed.get("clarification_question", "")
        clarification_options = parsed.get("clarification_options", [])

        # Rule-based backstop: if Claude missed the ambiguity, catch it here
        if not is_ambiguous:
            detected = _detect_ambiguity(question)
            if detected:
                is_ambiguous          = True
                clarification         = detected[0]
                clarification_options = detected[1]

        # Validate — if bad tables, retry once with the error fed back to Claude
        val_err = _validate_sql(sql, self.schema)
        if val_err:
            valid_tables = sorted(self.schema.get("table_names", []))
            fix_prompt = (
                f"The SQL you generated has an error: {val_err}. "
                f"The only valid tables are: {', '.join(valid_tables)}. "
                f"Rewrite the SQL using only those tables and return the same JSON format."
            )
            messages.append({"role": "assistant", "content": parsed.get("_raw", sql)})
            messages.append({"role": "user", "content": fix_prompt})

            parsed2, _ = self._call_claude(messages)
            if "error" in parsed2:
                return self._error_result(question, val_err, sql)

            sql2      = parsed2.get("sql", "").strip()
            val_err2  = _validate_sql(sql2, self.schema)
            if val_err2:
                return self._error_result(question, val_err2, sql2)

            sql         = sql2
            explanation = parsed2.get("explanation", explanation)
            assumptions = parsed2.get("assumptions", assumptions)

        # Execute
        columns, rows, exec_err = self._execute(sql)
        if exec_err:
            return self._error_result(question, exec_err, sql)

        # Visualization
        viz_type, viz_config = _suggest_viz(columns, rows)

        return QueryResult(
            question=question,
            sql=sql,
            columns=columns,
            rows=rows,
            explanation=explanation,
            assumptions=assumptions,
            is_ambiguous=is_ambiguous,
            clarification_question=clarification,
            clarification_options=clarification_options,
            viz_type=viz_type,
            viz_config=viz_config,
            error="",
            row_count=len(rows),
        )

    # ── private ─────────────────────────────────────────────────────────────

    def _call_claude(self, messages: List[Dict]):
        """Call Claude and return (parsed_dict, updated_messages). Stores raw text in parsed['_raw']."""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                system=self.system_prompt,
                messages=messages,
            )
            raw = response.content[0].text.strip()
        except Exception as exc:
            return {"error": f"LLM error: {exc}"}, messages

        parsed = self._parse_response(raw)
        parsed["_raw"] = raw   # stash for retry context
        return parsed, messages

    def _parse_response(self, raw: str) -> Dict:
        # Strip markdown fences
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

        # Find the outermost { ... } block
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return {"error": f"Could not parse LLM response as JSON: {raw[:300]}"}

        json_str = m.group()

        # Replace literal control characters (newlines, tabs, etc.) that appear
        # INSIDE JSON string values — these make json.loads fail with
        # "Invalid control character". We replace them with their escaped forms
        # only when they appear inside a quoted string.
        def _escape_controls(s: str) -> str:
            result = []
            in_string = False
            escaped = False
            for ch in s:
                if escaped:
                    result.append(ch)
                    escaped = False
                elif ch == "\\":
                    result.append(ch)
                    escaped = True
                elif ch == '"':
                    result.append(ch)
                    in_string = not in_string
                elif in_string and ch == "\n":
                    result.append("\\n")
                elif in_string and ch == "\r":
                    result.append("\\r")
                elif in_string and ch == "\t":
                    result.append("\\t")
                else:
                    result.append(ch)
            return "".join(result)

        try:
            return json.loads(_escape_controls(json_str))
        except json.JSONDecodeError:
            # Last resort: extract fields manually with regex
            try:
                sql = re.search(r'"sql"\s*:\s*"(.*?)"(?=\s*,\s*")', json_str, re.DOTALL)
                exp = re.search(r'"explanation"\s*:\s*"(.*?)"(?=\s*,\s*"|\s*\})', json_str, re.DOTALL)
                return {
                    "sql": sql.group(1).replace("\\n", " ") if sql else "",
                    "explanation": exp.group(1) if exp else "",
                    "assumptions": "",
                    "is_ambiguous": False,
                    "clarification_question": "",
                    "clarification_options": [],
                }
            except Exception as e2:
                return {"error": f"JSON parse error: {e2}. Raw: {raw[:300]}"}

    def _execute(self, sql: str) -> tuple[List[str], List[List], str]:
        try:
            cur = self.con.cursor()
            cur.execute(sql)
            rows_raw = cur.fetchall()
            if not rows_raw:
                return [], [], ""
            columns = list(rows_raw[0].keys())
            rows = [list(r) for r in rows_raw]
            return columns, rows, ""
        except sqlite3.Error as e:
            return [], [], f"SQL execution error: {e}"

    def _error_result(self, question: str, error: str, raw_sql: str = "") -> QueryResult:
        return QueryResult(
            question=question, sql=raw_sql, columns=[], rows=[],
            explanation="", assumptions="", is_ambiguous=False,
            clarification_question="", clarification_options=[],
            viz_type="none", viz_config={},
            error=error, row_count=0,
        )
