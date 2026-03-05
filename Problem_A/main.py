"""
main.py — AP Policy to Deterministic Rule Conversion Pipeline

Usage:
  python main.py                        # Full pipeline (parse → extract → detect → run → notify)
  python main.py --extract-only         # Parse PDF and extract rules only
  python main.py --run-only             # Load existing rules, run against all sample invoices
  python main.py --invoice invoice.json # Run against a specific invoice JSON
  python main.py --force-baseline       # Use pre-extracted baseline rules (no LLM)
  python main.py --no-notify            # Skip email notification dispatch

Pipeline steps:
  1. Parse    — Extract text from AP policy PDF, segment into sections/clauses.
  2. Extract  — Use LLM (or baseline) to identify and structure rules.
  3. Detect   — Find contradictions and overlaps in extracted rules.
  4. Execute  — Run rules against sample invoice(s) and produce results.
  5. Notify   — Dispatch email notifications for triggered deviations.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config import ANTHROPIC_API_KEY, OUTPUT_DIR, POLICY_PDF_PATH, SIMULATE_NOTIFICATIONS
from conflict_detector import detect_conflicts, print_conflicts
from notification import dispatch_notifications
from parser import parse_pdf, summarize
from rule_engine import RuleEngine, print_engine_report
from rule_extractor import extract_rules, load_baseline_rules, print_rule_summary


# ---------------------------------------------------------------------------
# Sample invoices
# ---------------------------------------------------------------------------

_SAMPLE_INVOICES: List[Dict] = [
    {
        "_scenario": "SCENARIO_1: Over-invoiced (12% above PO) — triggers escalation to Finance Controller",
        "invoice_number": "INV-2026-001",
        "invoice_date": "2026-02-15",
        "processing_date": "2026-02-15",
        "vendor_name": "ABC Supplies Pvt Ltd",
        "vendor_gstin": "29ABCDE1234F1Z5",
        "vendor_pan": "ABCDE1234F",
        "vendor_gstin_in_master": "29ABCDE1234F1Z5",
        "vendor_pan_on_file": "ABCDE1234F",
        "vendor_on_watchlist": False,
        "po_number": "PO-2026-001",
        "po_exists": True,
        "po_amount": 500000,
        "invoice_total": 560000,        # 12% above PO → escalate to Finance Controller
        "taxable_amount": 473729,
        "tax_amount": 85271,
        "grand_total": 560000,
        "supply_type": "intra_state",
        "cgst": 42500,
        "sgst": 42500,
        "igst": 0,
        "place_of_supply_state_code": "29",
        "buyer_gstin": "29XYZAB1234G1Z3",
        "is_handwritten": False,
        "is_goods_based": True,
        "grn_number": "GRN-2026-001",
        "grn_date": "2026-02-12",
        "grn_exists": True,
        "has_qr_code": False,           # > INR 10L would need QR; this is 5.6L so OK
        "qr_invoice_number": None,
        "qr_vendor_gstin": None,
        "digital_signature_present": False,
        "digital_signature_valid": None,
        "existing_invoice_number": None,
        "existing_invoice_vendor_gstin": None,
        "line_items": [
            {
                "line_id": 1,
                "description": "Office Chairs",
                "po_qty": 100,
                "invoice_qty": 112,     # > PO qty → hold
                "po_unit_rate": 5000,
                "invoice_unit_rate": 5000,
                "grn_qty": 100,
            }
        ],
    },
    {
        "_scenario": "SCENARIO_2: Clean invoice — should AUTO_APPROVE",
        "invoice_number": "INV-2026-002",
        "invoice_date": "2026-02-20",
        "processing_date": "2026-02-20",
        "vendor_name": "XYZ Stationery Ltd",
        "vendor_gstin": "27XYZST5678H2Z1",
        "vendor_pan": "XYZST5678H",
        "vendor_gstin_in_master": "27XYZST5678H2Z1",
        "vendor_pan_on_file": "XYZST5678H",
        "vendor_on_watchlist": False,
        "po_number": "PO-2026-002",
        "po_exists": True,
        "po_amount": 80000,
        "invoice_total": 80400,         # 0.5% above — within tolerance → auto-approve
        "taxable_amount": 68136,
        "tax_amount": 12264,
        "grand_total": 80400,
        "supply_type": "intra_state",
        "cgst": 6132,
        "sgst": 6132,
        "igst": 0,
        "place_of_supply_state_code": "27",
        "buyer_gstin": "27BUYCO9876K3Z8",
        "is_handwritten": False,
        "is_goods_based": True,
        "grn_number": "GRN-2026-002",
        "grn_date": "2026-02-19",
        "grn_exists": True,
        "has_qr_code": False,
        "qr_invoice_number": None,
        "qr_vendor_gstin": None,
        "digital_signature_present": False,
        "digital_signature_valid": None,
        "existing_invoice_number": None,
        "existing_invoice_vendor_gstin": None,
        "line_items": [
            {
                "line_id": 1,
                "description": "A4 Paper Reams",
                "po_qty": 200,
                "invoice_qty": 200,
                "po_unit_rate": 400,
                "invoice_unit_rate": 402,   # 0.5% — within 2% rate tolerance
                "grn_qty": 200,
            }
        ],
    },
    {
        "_scenario": "SCENARIO_3: GSTIN mismatch + tax error + inter-state IGST missing",
        "invoice_number": "INV-2026-003",
        "invoice_date": "2026-02-25",
        "processing_date": "2026-02-25",
        "vendor_name": "Rogue Vendor Co",
        "vendor_gstin": "07ROGUE9999Z1Z9",
        "vendor_pan": "ROGUE9999Z",
        "vendor_gstin_in_master": "07ROGUE1111Z1Z9",   # GSTIN mismatch!
        "vendor_pan_on_file": "ROGUE1111Z",
        "vendor_on_watchlist": True,                   # watchlist vendor
        "po_number": "PO-2026-003",
        "po_exists": True,
        "po_amount": 250000,
        "invoice_total": 260000,        # 4% above PO → dept head (but watchlist)
        "taxable_amount": 220339,
        "tax_amount": 40000,            # Should be ~39661; error > INR 1
        "grand_total": 260000,
        "supply_type": "inter_state",
        "cgst": 5000,                   # Should be 0 for inter-state!
        "sgst": 5000,                   # Should be 0 for inter-state!
        "igst": 30000,                  # IGST present but CGST/SGST also non-zero → violation
        "place_of_supply_state_code": "27",
        "buyer_gstin": "27BUYCO9876K3Z8",
        "is_handwritten": False,
        "is_goods_based": True,
        "grn_number": "GRN-2026-003",
        "grn_date": "2026-02-28",      # GRN date AFTER invoice date → flag
        "grn_exists": True,
        "has_qr_code": False,
        "qr_invoice_number": None,
        "qr_vendor_gstin": None,
        "digital_signature_present": False,
        "digital_signature_valid": None,
        "existing_invoice_number": None,
        "existing_invoice_vendor_gstin": None,
        "line_items": [
            {
                "line_id": 1,
                "description": "Industrial Equipment",
                "po_qty": 10,
                "invoice_qty": 12,      # > PO qty → hold
                "po_unit_rate": 25000,
                "invoice_unit_rate": 21667,
                "grn_qty": 11,          # invoice_qty > grn_qty → reject line
            }
        ],
    },
    {
        "_scenario": "SCENARIO_4: High-value invoice (>50L) — requires CFO approval + QR code",
        "invoice_number": "INV-2026-004",
        "invoice_date": "2026-03-01",
        "processing_date": "2026-03-01",
        "vendor_name": "Mega Infrastructure Ltd",
        "vendor_gstin": "06MEGAI1234M1Z2",
        "vendor_pan": "MEGAI1234M",
        "vendor_gstin_in_master": "06MEGAI1234M1Z2",
        "vendor_pan_on_file": "MEGAI1234M",
        "vendor_on_watchlist": False,
        "po_number": "PO-2026-004",
        "po_exists": True,
        "po_amount": 60000000,
        "invoice_total": 60300000,      # 0.5% above — within tolerance
        "taxable_amount": 51101695,
        "tax_amount": 9198305,
        "grand_total": 60300000,
        "supply_type": "inter_state",
        "cgst": 0,
        "sgst": 0,
        "igst": 9198305,
        "place_of_supply_state_code": "06",
        "buyer_gstin": "06BUYCO0001K1Z5",
        "is_handwritten": False,
        "is_goods_based": True,
        "grn_number": "GRN-2026-004",
        "grn_date": "2026-02-28",
        "grn_exists": True,
        "has_qr_code": False,           # MISSING! — >10L requires QR code
        "qr_invoice_number": None,
        "qr_vendor_gstin": None,
        "digital_signature_present": True,
        "digital_signature_valid": False,   # Signature present but invalid
        "existing_invoice_number": None,
        "existing_invoice_vendor_gstin": None,
        "line_items": [
            {
                "line_id": 1,
                "description": "Construction Materials",
                "po_qty": 1000,
                "invoice_qty": 1005,
                "po_unit_rate": 60000,
                "invoice_unit_rate": 60030,  # 0.05% — within 2% rate tolerance
                "grn_qty": 1005,
            }
        ],
    },
]


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_parse(pdf_path: str) -> Any:
    print(f"\n{'─'*60}")
    print("  STEP 1: DOCUMENT PARSING")
    print(f"{'─'*60}")
    print(f"  Parsing: {pdf_path}")
    doc = parse_pdf(pdf_path)
    print(summarize(doc))
    return doc


def step_extract(doc: Any, force_baseline: bool) -> Dict:
    print(f"\n{'─'*60}")
    print("  STEP 2: RULE EXTRACTION")
    _has_key = bool(ANTHROPIC_API_KEY) and not ANTHROPIC_API_KEY.startswith("your-")
    mode = f"Claude ({ANTHROPIC_API_KEY[:8]}…)" if (_has_key and not force_baseline) else "Pre-Extracted Baseline"
    print(f"  Mode: {mode}")
    print(f"{'─'*60}")
    rule_data = extract_rules(doc=doc, force_baseline=force_baseline)
    print_rule_summary(rule_data)
    return rule_data


def step_detect_conflicts(rule_data: Dict) -> Dict:
    print(f"\n{'─'*60}")
    print("  STEP 3: CONFLICT DETECTION")
    print(f"{'─'*60}")
    conflicts = detect_conflicts(rule_data)
    rule_data["conflicts"] = conflicts
    print_conflicts(conflicts)

    # Persist updated rule data with programmatic conflicts
    output_path = OUTPUT_DIR / "final_rules_with_conflicts.json"
    output_path.write_text(json.dumps(rule_data, indent=2), encoding="utf-8")
    print(f"\n  [Output] Rules + conflicts saved → {output_path}")
    return rule_data


def step_run_engine(
    rule_data: Dict,
    invoices: List[Dict],
    notify: bool,
    simulate: bool = True,
) -> List[Dict]:
    print(f"\n{'─'*60}")
    print("  STEP 4: RULE ENGINE EXECUTION")
    print(f"{'─'*60}")

    engine = RuleEngine(rule_data.get("rules", []))
    all_outputs = []

    for invoice in invoices:
        scenario = invoice.pop("_scenario", f"Invoice {invoice.get('invoice_number')}")
        print(f"\n  ▶ {scenario}")

        output = engine.run(invoice)
        print_engine_report(output)

        output_dict = {
            "scenario": scenario,
            "invoice_id": output.invoice_id,
            "disposition": output.disposition,
            "final_approver": output.final_approver,
            "flags": output.flags,
            "fired_actions": output.fired_actions,
            "triggered_rules": [
                {
                    "rule_id": r.rule_id,
                    "source_clause": r.source_clause,
                    "action": r.action,
                    "reason": r.reason,
                    "route_to": r.route_to,
                    "requires_justification": r.requires_justification,
                    "scope": r.scope,
                    "line_item_id": r.line_item_id,
                }
                for r in output.results if r.triggered
            ],
            "errors": output.errors,
            "derived_fields": output.derived_fields,
            "notifications_dispatched": len(output.notifications_pending),
        }
        all_outputs.append(output_dict)

        if notify and output.notifications_pending:
            print(f"\n  STEP 5: NOTIFICATIONS")
            print(f"  {'─'*52}")
            dispatch_notifications(
                output.notifications_pending,
                invoice,
                simulate=simulate,
            )

    # Save engine results
    results_path = OUTPUT_DIR / "engine_results.json"
    results_path.write_text(
        json.dumps(
            {"generated_at": datetime.now().isoformat(), "results": all_outputs},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  [Output] Engine results saved → {results_path}")
    return all_outputs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AP Policy → Deterministic Rule Conversion Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--pdf", default=POLICY_PDF_PATH, help="Path to AP policy PDF")
    p.add_argument("--invoice", help="Path to a custom invoice JSON file")
    p.add_argument("--extract-only", action="store_true", help="Run only parse + extract steps")
    p.add_argument("--run-only", action="store_true", help="Skip parse/extract; load existing rules")
    p.add_argument("--force-baseline", action="store_true", help="Skip LLM; use pre-extracted rules")
    p.add_argument("--no-notify", action="store_true", help="Suppress all notifications")
    # simulate flag reads from config.yaml notifications.simulate by default
    return p


def main() -> None:
    args = build_parser().parse_args()

    print("╔══════════════════════════════════════════════════════╗")
    print("║   Cashflo AP Policy → Deterministic Rule Pipeline    ║")
    print("╚══════════════════════════════════════════════════════╝")

    # --- Load invoices ---
    if args.invoice:
        invoice_path = Path(args.invoice)
        if not invoice_path.exists():
            print(f"[Error] Invoice file not found: {invoice_path}", file=sys.stderr)
            sys.exit(1)
        invoices = [json.loads(invoice_path.read_text())]
    else:
        invoices = [dict(inv) for inv in _SAMPLE_INVOICES]  # shallow copy

    # --- Step 1 + 2: Parse & Extract ---
    if args.run_only:
        print("\n  [run-only] Loading existing rules …")
        rule_data = load_baseline_rules()
        doc = None
    else:
        doc = step_parse(args.pdf)
        rule_data = step_extract(doc, force_baseline=args.force_baseline or not OPENAI_API_KEY)

    # --- Step 3: Conflict detection ---
    if not args.extract_only:
        rule_data = step_detect_conflicts(rule_data)

    # --- Step 4 + 5: Run engine + notifications ---
    if not args.extract_only:
        step_run_engine(
            rule_data=rule_data,
            invoices=invoices,
            notify=not args.no_notify,
            simulate=SIMULATE_NOTIFICATIONS,
        )

    print("\n✓ Pipeline complete.\n")
    print(f"  Output files in: {OUTPUT_DIR}")
    print(f"    extracted_rules.json           — baseline pre-extracted rules")
    print(f"    final_rules_with_conflicts.json — rules + conflict analysis")
    print(f"    engine_results.json            — rule engine execution results")
    print(f"    notifications_log.json         — notification dispatch log")


if __name__ == "__main__":
    main()
