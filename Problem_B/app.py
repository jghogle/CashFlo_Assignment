"""
app.py — FastAPI backend for the Cashflo NLP-to-SQL engine.

Usage:
  cd Problem_B
  uvicorn app:app --reload --port 8001
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))

from db import get_connection
from query_cache import QueryCache
from schema_discovery import discover, schema_as_ddl, schema_summary
from sql_generator import SQLGenerator

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(_CONFIG_PATH, encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)

_BASE          = Path(__file__).parent
_SEM_PATH      = _BASE / _cfg["semantic_layer"]["path"]
_CACHE_PATH    = _BASE / _cfg["cache"]["path"]
_API_KEY       = _cfg["llm"]["api_key"]
_MODEL         = _cfg["llm"]["model"]
_CACHE_ENABLED = bool(_cfg["cache"].get("enabled", True))
_SIM_THRESHOLD = float(_cfg["cache"].get("similarity_threshold", 0.75))

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_generator: Optional[SQLGenerator] = None
_cache = QueryCache(_CACHE_PATH, threshold=_SIM_THRESHOLD)

# In-memory conversation sessions: session_id → list of turns
_sessions: Dict[str, List[Dict]] = {}


def _get_generator() -> SQLGenerator:
    global _generator
    if _generator is None:
        if not _API_KEY or _API_KEY.startswith("sk-ant-your"):
            raise HTTPException(status_code=503, detail="Anthropic API key not set in config.yaml")
        _generator = SQLGenerator(_API_KEY, _MODEL, _SEM_PATH)
    return _generator


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Cashflo NLP-to-SQL Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None   # for multi-turn conversations
    use_cache: bool = True


class FeedbackRequest(BaseModel):
    question: str
    sql: str
    correct: bool
    corrected_sql: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_dict(result, cache_hit: bool = False, similarity: float = 0.0) -> Dict:
    return {
        "question":              result.question,
        "sql":                   result.sql,
        "columns":               result.columns,
        "rows":                  result.rows,
        "row_count":             result.row_count,
        "explanation":           result.explanation,
        "assumptions":           result.assumptions,
        "is_ambiguous":           result.is_ambiguous,
        "clarification_question": result.clarification_question,
        "clarification_options":  result.clarification_options,
        "viz_type":              result.viz_type,
        "viz_config":            result.viz_config,
        "error":                 result.error,
        "cache_hit":             cache_hit,
        "cache_similarity":      similarity,
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/query")
def run_query(req: QueryRequest):
    """Main NLP-to-SQL endpoint."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    # Session management
    session_id = req.session_id or str(uuid.uuid4())
    history = _sessions.get(session_id, [])

    # Cache lookup
    if req.use_cache and _CACHE_ENABLED:
        cached = _cache.lookup(question)
        if cached:
            _cache.increment_hit(cached["question"])
            return {
                **cached,
                "session_id": session_id,
                "columns": cached.get("columns", []),
                "rows": cached.get("rows", []),
                "clarification_options": cached.get("clarification_options", []),
            }

    # Generate
    gen = _get_generator()
    result = gen.generate(question, conversation_history=history)

    # Update session history
    history.append({"role": "user", "content": question})
    history.append({
        "role": "assistant",
        "content": f"SQL: {result.sql}\n\nExplanation: {result.explanation}",
    })
    _sessions[session_id] = history[-20:]  # keep last 20 turns

    # Cache if successful
    if not result.error and _CACHE_ENABLED:
        preview = result.rows[:5]
        _cache.store(
            question=question,
            sql=result.sql,
            result_preview=preview,
            explanation=result.explanation,
            assumptions=result.assumptions,
        )
        # Store columns/rows in cache entry for cache hits
        for entry in _cache.entries:
            if entry["question"] == question:
                entry["columns"] = result.columns
                entry["rows"] = result.rows[:100]
                entry["row_count"] = result.row_count
                entry["viz_type"] = result.viz_type
                entry["viz_config"] = result.viz_config
                entry["is_ambiguous"] = result.is_ambiguous
                entry["clarification_question"] = result.clarification_question
                entry["clarification_options"] = result.clarification_options
                _cache._save()
                break

    return {**_result_to_dict(result), "session_id": session_id}


@app.get("/api/schema")
def get_schema():
    """Return the full discovered schema."""
    con = get_connection()
    schema = discover(con)
    ddl = schema_as_ddl(con)
    return {"schema": schema, "ddl": ddl}


@app.get("/api/semantic-layer")
def get_semantic_layer():
    """Return the semantic layer YAML as JSON."""
    with open(_SEM_PATH, encoding="utf-8") as f:
        sl = yaml.safe_load(f)
    return sl


@app.get("/api/cache")
def get_cache():
    """Return all cached queries."""
    return {"entries": _cache.all_entries(), "total": len(_cache.entries)}


@app.delete("/api/cache")
def clear_cache():
    """Clear the query cache."""
    _cache.clear()
    return {"message": "Cache cleared."}


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest):
    """Accept user feedback; if corrected SQL is provided, update cache."""
    if req.correct or not req.corrected_sql:
        return {"message": "Feedback recorded."}
    # Overwrite cached entry with corrected SQL
    _cache.store(
        question=req.question,
        sql=req.corrected_sql,
        result_preview=[],
        explanation="[User corrected]",
        assumptions="",
    )
    return {"message": "Cache updated with corrected SQL."}


@app.get("/api/sample-questions")
def sample_questions():
    return {"questions": [
        "How many invoices were raised last month?",
        "List all vendors on the watchlist.",
        "Which vendors have overdue invoices greater than INR 1,00,000?",
        "Show me all invoices for the Engineering department.",
        "What is the total outstanding amount across all vendors?",
        "Which product has the highest total invoiced value?",
        "Rank vendors by total invoice value.",
        "For each vendor, show the running total of payments received.",
        "Show each invoice alongside the previous invoice amount for the same vendor.",
        "What was our revenue last quarter?",
        "Show me all unpaid bills.",
        "Who are our top 5 vendors?",
        "Compare this month's invoice volume with last month.",
        "Which department has the highest pending invoice amount?",
        "Show invoices with deviations and their deviation types.",
    ]}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    return {"session_id": session_id, "history": _sessions.get(session_id, [])}


@app.delete("/api/sessions/{session_id}")
def clear_session(session_id: str):
    _sessions.pop(session_id, None)
    return {"message": f"Session {session_id} cleared."}


@app.get("/api/download/cache")
def download_cache():
    data = json.dumps(_cache.all_entries(), indent=2, ensure_ascii=False)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=query_cache.json"},
    )


# ---------------------------------------------------------------------------
# Serve frontend (must be last)
# ---------------------------------------------------------------------------

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)
