"""
app.py — FastAPI web server for the AP Rule Pipeline UI.

Usage:
  cd Problem_A
  uvicorn app:app --reload --port 8000

Then open http://localhost:8000 in your browser.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Allow importing local modules
sys.path.insert(0, str(Path(__file__).parent))

from config import OUTPUT_DIR, POLICY_PDF_PATH, SIMULATE_NOTIFICATIONS
from conflict_detector import detect_conflicts
from notification import dispatch_notifications
from parser import parse_pdf
from rule_engine import RuleEngine
from rule_extractor import load_baseline_rules

app = FastAPI(title="Cashflo AP Rule Pipeline", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rules() -> Dict:
    final_path = OUTPUT_DIR / "final_rules_with_conflicts.json"
    if final_path.exists():
        return json.loads(final_path.read_text())
    return load_baseline_rules()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/rules")
def get_rules():
    data = _load_rules()
    rules = data.get("rules", [])
    return {"rules": rules, "total": len(rules)}


@app.get("/api/conflicts")
def get_conflicts():
    data = _load_rules()
    conflicts = data.get("conflicts", [])
    return {"conflicts": conflicts, "total": len(conflicts)}


@app.get("/api/metadata")
def get_metadata():
    data = _load_rules()
    rules = data.get("rules", [])
    by_category: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for r in rules:
        cat = r.get("category", "UNCATEGORISED")
        act = r.get("action", "UNKNOWN")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_action[act] = by_action.get(act, 0) + 1

    low_conf = [r["rule_id"] for r in rules if r.get("confidence", 1.0) < 0.90]
    return {
        "metadata": data.get("metadata", {}),
        "total_rules": len(rules),
        "total_conflicts": len(data.get("conflicts", [])),
        "low_confidence_count": len(low_conf),
        "low_confidence_rules": low_conf,
        "by_category": by_category,
        "by_action": by_action,
    }


@app.post("/api/run")
def run_invoice(invoice: Dict[str, Any]):
    data = _load_rules()
    rules = data.get("rules", [])
    if not rules:
        raise HTTPException(status_code=400, detail="No rules loaded. Run the pipeline first.")

    engine = RuleEngine(rules)
    output = engine.run(invoice)

    triggered = [
        {
            "rule_id": r.rule_id,
            "source_clause": r.source_clause,
            "description": r.description,
            "action": r.action,
            "reason": r.reason,
            "route_to": r.route_to,
            "requires_justification": r.requires_justification,
            "scope": r.scope,
            "line_item_id": r.line_item_id,
        }
        for r in output.results if r.triggered
    ]

    notifications_log = []
    if output.notifications_pending:
        notifications_log = dispatch_notifications(
            output.notifications_pending, invoice, simulate=True
        )

    return {
        "invoice_id": output.invoice_id,
        "disposition": output.disposition,
        "final_approver": output.final_approver,
        "fired_actions": output.fired_actions,
        "triggered_rules": triggered,
        "flags": output.flags,
        "errors": output.errors,
        "derived_fields": {
            k: v for k, v in output.derived_fields.items() if not callable(v)
        },
        "notifications_dispatched": len(output.notifications_pending),
    }


@app.get("/api/download/rules")
def download_rules():
    from fastapi.responses import Response
    data = _load_rules()
    rules = data.get("rules", [])
    pretty = json.dumps(rules, indent=2, ensure_ascii=False)
    return Response(
        content=pretty,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=extracted_rules.json"},
    )


@app.get("/api/download/conflicts")
def download_conflicts():
    from fastapi.responses import Response
    data = _load_rules()
    conflicts = data.get("conflicts", [])
    pretty = json.dumps(conflicts, indent=2, ensure_ascii=False)
    return Response(
        content=pretty,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=conflicts.json"},
    )


@app.get("/api/notifications")
def get_notifications():
    log_path = OUTPUT_DIR / "notifications_log.json"
    if not log_path.exists():
        return {"notifications": [], "total": 0}
    data = json.loads(log_path.read_text())
    return {"notifications": list(reversed(data)), "total": len(data)}


@app.get("/api/engine-results")
def get_engine_results():
    path = OUTPUT_DIR / "engine_results.json"
    if not path.exists():
        return {"results": [], "generated_at": None}
    return json.loads(path.read_text())


@app.get("/api/sample-invoice")
def get_sample_invoice():
    sample_path = Path(__file__).parent / "sample_invoice.json"
    if sample_path.exists():
        return json.loads(sample_path.read_text())
    return {}


@app.get("/api/parse")
def parse_document():
    try:
        doc = parse_pdf(POLICY_PDF_PATH)
        return {
            "title": doc.title,
            "sections": [
                {
                    "number": s.number,
                    "title": s.title,
                    "clauses": [
                        {
                            "number": c.number,
                            "text": c.raw_text,
                            "cross_refs": c.cross_refs,
                        }
                        for c in s.clauses
                    ],
                }
                for s in doc.sections
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Serve frontend (must be last)
# ---------------------------------------------------------------------------

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
