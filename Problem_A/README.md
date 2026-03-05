# Problem A — AP Policy Document to Deterministic Rule Conversion

## Overview

This system converts the **Cashflo Accounts Payable Policy** (PDF) into a structured, machine-executable set of deterministic rules. It then runs those rules against sample invoices, detects conflicts between rules, and dispatches email notifications for deviations — all accessible through a web UI.

---

## Flow Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                  Problem A — Policy to Deterministic Rules                   │
└──────────────────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────┐
  │  AP Policy PDF          │  Sample_AP_Policy_Document.pdf
  └────────────┬────────────┘
               │
               ▼
  ┌─────────────────────────┐
  │      parser.py          │  • Extract raw text from PDF (pdfplumber)
  │   PDF Text Extraction   │  • Segment into sections, sub-clauses
  │                         │  • Resolve cross-references (e.g. "Refer Sec 2.3(b)")
  └────────────┬────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │              rule_extractor.py                                               │
  │                                                                              │
  │   use_llm: false ──▶ Load output/extracted_rules.json  (instant, free)       │
  │   use_llm: true  ──▶ Claude 3.5 Haiku API call         (re-extracts rules)   │
  │                       System prompt includes schema +                        │
  │                       example JSON rule for guidance                         │
  └────────────────────────┬───────────────────────────────────────────────-─────┘
                           │
                           ▼
  ┌─────────────────────────────────────────────────┐
  │         extracted_rules.json                    │
  │  34 rules across 7 categories                   │
  │  Each rule: rule_id, source_clause, condition,  │
  │  action, priority, confidence, notification     │
  └──────────────┬──────────────────┬───────────────┘
                 │                  │
       ┌─────────▼───────┐  ┌────────▼────────────────┐
       │conflict_        │  │   rule_engine.py        │
       │detector.py      │  │                         │
       │                 │  │  Evaluate all 34 rules  │
       │ • 4 curated     │  │  against invoice JSON   │
       │   conflicts     │  │  • Compute derived      │
       │ • Programmatic  │  │    fields (deviation %) │
       │   overlap/gap   │  │  • Priority ordering    │
       │   detection     │  │  • Return: disposition  │
       └─────────────────┘  │    + triggered rules    │
                            │    + reasons            │
                            └────────┬────────────────┘
                                     │
                                     ▼ (if deviation detected)
                            ┌──────────────────────────┐
                            │   notification.py        │
                            │                          │
                            │  simulate: true          │
                            │    → console + log file  │
                            │  simulate: false         │
                            │    → Gmail SMTP          │
                            │      port 465 (SSL)      │
                            │      or 587 (STARTTLS)   │
                            │  Recipients from         │
                            │  config.yaml (hot-reload)│
                            └────────┬─────────────────┘
                                     │
                                     ▼
  ┌──────────────────────────────────────────────────┐
  │  Web UI  (FastAPI + Tailwind + Alpine.js)        │
  │  ┌──────────┐ ┌───────┐ ┌──────────┐ ┌────────┐  │
  │  │Dashboard │ │ Rules │ │Conflicts │ │ Test   │  │
  │  │          │ │Browse │ │ Severity │ │Invoice │  │
  │  │Rule count│ │Search │ │ Download │ │Simulate│  │
  │  │Conf chart│ │Filter │ │          │ │        │  │
  │  └──────────┘ └───────┘ └──────────┘ └────────┘  │
  │  ┌──────────┐ ┌───────────┐ ┌───────────────────┐│
  │  │Past Runs │ │Notif. Log │ │  Policy Viewer    ││
  │  └──────────┘ └───────────┘ └───────────────────┘│
  └──────────────────────────────────────────────────┘
```

---

## File Structure

```
Problem_A/
├── app.py                   ← FastAPI web server (UI entry point)
├── main.py                  ← Full pipeline orchestrator (CLI entry point)
├── parser.py                ← PDF text extraction & section/clause segmentation
├── rule_extractor.py        ← Claude LLM rule extraction + cached baseline fallback
├── conflict_detector.py     ← Programmatic conflict & overlap detection
├── rule_engine.py           ← Deterministic rule execution engine
├── notification.py          ← Email notification dispatch (Section 6 compliance)
├── config.py                ← YAML config loader (reads config.yaml)
├── config.yaml              ← All settings: API key, SMTP, notifications, LLM toggle
├── sample_invoice.json      ← Sample invoice for testing
├── requirements.txt
├── frontend/
│   ├── index.html           ← Single-page web UI (Tailwind + Alpine.js + Chart.js)
│   └── logo.png             ← Cashflo brand logo
└── output/
    ├── extracted_rules.json             ← Pre-extracted baseline (34 rules, 4 conflicts)
    ├── final_rules_with_conflicts.json  ← Rules + programmatic conflict analysis
    ├── engine_results.json              ← Rule engine execution results per invoice
    └── notifications_log.json           ← Notification dispatch log
```

---

## Quick Start

### 1. Install dependencies
```bash
cd Problem_A
pip install -r requirements.txt
```

### 2. Configure `config.yaml`

All settings live in `config.yaml` — no `.env` file needed.

```yaml
llm:
  api_key: "sk-ant-your-key-here"
  model: "claude-3-5-haiku-20241022"
  use_llm: false          # false = use cached rules (free, instant)
                          # true  = call Claude API to re-extract rules

policy:
  pdf_path: "../Sample_AP_Policy_Document.pdf"

notifications:
  simulate: true          # true = print to console; false = send real email
  from: "sender@gmail.com"
  to:
    - "recipient1@gmail.com"
    - "recipient2@gmail.com"
  smtp:
    host: "smtp.gmail.com"
    port: 465             # 465 = SSL (recommended), 587 = STARTTLS
    user: "sender@gmail.com"
    password: "xxxx xxxx xxxx xxxx"   # Gmail App Password
```

> **Tip:** Get a Gmail App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Use port 465 (SSL) — it is more reliable than 587 through most networks.

### 3. Launch the web UI
```bash
cd Problem_A
uvicorn app:app --reload --port 8000
```
Open **http://localhost:8000** in your browser.

### 4. Run the CLI pipeline (baseline mode — no API key needed)
```bash
python main.py --force-baseline
```

### 5. Run with Claude LLM extraction
```bash
# Set use_llm: true in config.yaml first
python main.py
```

### 6. Run against a custom invoice
```bash
python main.py --invoice sample_invoice.json --force-baseline
```

### 7. Extract rules only (skip engine run)
```bash
python main.py --extract-only --force-baseline
```

---

## Web UI Tabs

| Tab | Description |
|-----|-------------|
| **Dashboard** | Rule counts, conflict counts, low-confidence flags, category chart |
| **Rules** | Browse, search, and filter all 34 extracted rules. Download as JSON |
| **Conflicts** | View all detected conflicts with severity, example scenarios, and recommendations. Download as JSON |
| **Test Invoice** | Submit any invoice JSON and see disposition, triggered rules, and notifications |
| **Past Runs** | History of all engine executions |
| **Notifications** | Log of all email notifications dispatched |
| **Policy Viewer** | Browse the parsed policy document section by section |

---

## LLM vs Cached Rules

Controlled by a single flag in `config.yaml`:

```yaml
llm:
  use_llm: false   # always use output/extracted_rules.json — zero API cost
  use_llm: true    # call Claude to re-extract rules from the policy PDF
```

With `use_llm: false` (the default), the system reads from the pre-extracted `output/extracted_rules.json` — instant, free, and deterministic. Set it to `true` only when you want to re-extract rules from a new or updated policy document.

---

## Pipeline Steps

| Step | Module | Description |
|------|--------|-------------|
| 1. Parse | `parser.py` | Extracts text from PDF; identifies sections, sub-clauses, and cross-references |
| 2. Extract | `rule_extractor.py` | Claude (Anthropic) or cached baseline identifies every IF/THEN/UNLESS rule |
| 3. Detect | `conflict_detector.py` | Finds contradictory/overlapping rules and coverage gaps |
| 4. Execute | `rule_engine.py` | Evaluates all rules against invoice JSON; returns disposition + reasons |
| 5. Notify | `notification.py` | Dispatches Section 6-compliant HTML emails via SMTP (port 465/587) |

---

## Rule Schema

Every extracted rule follows this canonical structure:

```json
{
  "rule_id": "AP-PO-004",
  "source_clause": "Section 2.2(c)",
  "description": "If Invoice Total exceeds PO Amount by 10% or more, escalate to Finance Controller with mandatory justification.",
  "category": "PO_MATCHING",
  "priority": 40,
  "scope": "INVOICE",
  "condition": {
    "field": "invoice_total",
    "op": "gte",
    "expr": "po_amount * 1.10"
  },
  "action": "ESCALATE",
  "action_details": {
    "status": "ESCALATED",
    "route_to": "FINANCE_CONTROLLER",
    "reason": "Invoice amount exceeds PO by 10% or more — mandatory justification required"
  },
  "requires_justification": true,
  "notification": {
    "type": "email",
    "to": ["finance_controller", "internal_audit"],
    "trigger": "ON_ESCALATE",
    "within_minutes": 15,
    "include_fields": ["invoice_number", "vendor_name", "po_number", "deviation_type", "deviation_details", "recommended_action"]
  },
  "confidence": 0.99,
  "tags": ["amount_matching", "escalation", "finance_controller", "critical_deviation"]
}
```

### Condition Operators

| Operator | Meaning |
|----------|---------|
| `gt` / `lt` | Greater / less than |
| `gte` / `lte` | Greater / less than or equal |
| `eq` / `neq` | Equal / not equal |
| `is_null` | Field is null, empty, or zero |
| `not_null` | Field has a non-null value |

### Condition Value Types

| Key | Example | Description |
|-----|---------|-------------|
| `value` | `10` | Literal constant |
| `value_field` | `"processing_date"` | Reference to another invoice field |
| `expr` | `"po_amount * 1.10"` | Safe arithmetic formula |

### Compound Conditions

```json
{
  "operator": "AND",
  "operands": [
    { "field": "invoice_total", "op": "gt", "expr": "po_amount * 1.01" },
    { "field": "invoice_total", "op": "lt", "expr": "po_amount * 1.10" }
  ]
}
```

---

## Extracted Rules Summary (34 rules across 7 categories)

| Category | Rules | Key Logic |
|----------|-------|-----------|
| `BASIC_VALIDATION` | 4 | Mandatory fields, future dates, handwritten invoices, duplicate detection |
| `PO_MATCHING` | 7 | ±1% auto-approve, 1–10% → Dept Head, ≥10% → Finance Controller, under-invoicing flag, qty/rate checks |
| `GRN_MATCHING` | 3 | GRN existence, invoice qty vs GRN qty, GRN post-dating |
| `TAX_COMPLIANCE` | 6 | GSTIN/PAN validation, tax calculation, intra/inter-state tax rules, place of supply |
| `APPROVAL_MATRIX` | 5 | Auto-approve ≤1L, Dept Head 1L–10L, Finance Controller 10L–50L, CFO >50L, watchlist exception |
| `DEVIATION_NOTIFICATIONS` | 4 | 15-min SLA, 48-hr escalation, critical deviation immediate notification |
| `QR_VALIDATION` | 3 | QR code requirement >10L, QR data validation, digital signature |

---

## Detected Conflicts

The system identifies **4 conflicts** between rules (3 static + 1 structural):

| ID | Severity | Issue |
|----|----------|-------|
| `AP-CONFLICT-001` | HIGH | Section 2.2(c) routes ≥10% deviation to Finance Controller, but Section 5.2 routes 1L–10L invoices to Dept Head. Conflict when invoice is 1L–10L AND deviation ≥10%. |
| `AP-CONFLICT-002` | HIGH | Section 2.2(c) routes to Finance Controller for ≥10% deviation; Section 5.4 routes >50L to CFO. Conflict for high-value invoices with large deviations. |
| `AP-CONFLICT-003` | MEDIUM | Section 2.2(b) routes 1–10% deviation to Dept Head; Section 5.3 routes 10L–50L to Finance Controller. Conflict for mid-range invoices with minor deviations. |
| `AP-CONFLICT-004` | HIGH | AP-PO-002 auto-approves any invoice within ±1% of PO amount with no upper bound. For invoices above INR 1L, this conflicts with the approval matrix (Dept Head / Finance Controller / CFO). |

**Resolution strategy**: Rules are ordered by `priority` (lower number = runs first). Deviation-based and exception-based routing should take precedence over amount-based routing. AP-PO-002 should be capped at INR 1L to align with the approval matrix.

Additional programmatic conflicts are detected at runtime by `conflict_detector.py` for overlapping amount ranges and coverage gaps.

---

## Email Notifications

Notifications are configured entirely in `config.yaml`:

- **`simulate: true`** — prints email content to console and logs to `output/notifications_log.json`. No SMTP needed.
- **`simulate: false`** — sends real emails via Gmail SMTP.
- **`from`** — the Gmail address sending the emails.
- **`to`** — one address (string) or multiple (list) — all recipients get every notification.
- **`smtp.port: 465`** — SSL (recommended). Change to `587` for STARTTLS if needed.
- **`smtp.password`** — must be a [Gmail App Password](https://myaccount.google.com/apppasswords), not your login password.

All notifications include: Invoice Number, Vendor Name, PO Number, Deviation Type, Deviation Details, and Recommended Action (Section 6.2 compliance).

---

## Sample Invoice Scenarios

The pipeline runs against 4 built-in scenarios:

| Scenario | Amount | Key Deviations | Expected Disposition |
|----------|--------|----------------|----------------------|
| 1 — Over-invoiced | INR 5.6L | 12% above PO, Qty > PO qty | ESCALATED → Finance Controller |
| 2 — Clean invoice | INR 0.8L | 0.5% above PO (within tolerance) | AUTO_APPROVED |
| 3 — Multi-failure | INR 2.6L | GSTIN mismatch, inter-state tax error, GRN post-dated, watchlist vendor | REJECTED |
| 4 — High-value | INR 6Cr | Missing QR code, invalid digital signature | HELD |

---

## Invoice JSON Schema

The rule engine expects an invoice with these fields:

```json
{
  "invoice_number": "INV-2026-001",
  "invoice_date": "YYYY-MM-DD",
  "processing_date": "YYYY-MM-DD",
  "vendor_name": "...",
  "vendor_gstin": "29ABCDE1234F1Z5",
  "vendor_gstin_in_master": "29ABCDE1234F1Z5",
  "vendor_pan_on_file": "ABCDE1234F",
  "vendor_on_watchlist": false,
  "po_number": "PO-2026-001",
  "po_exists": true,
  "po_amount": 500000,
  "invoice_total": 560000,
  "taxable_amount": 473729,
  "tax_amount": 85271,
  "grand_total": 560000,
  "supply_type": "intra_state",
  "cgst": 42500,
  "sgst": 42500,
  "igst": 0,
  "place_of_supply_state_code": "29",
  "buyer_gstin": "29XYZAB1234G1Z3",
  "is_handwritten": false,
  "is_goods_based": true,
  "grn_number": "GRN-2026-001",
  "grn_date": "YYYY-MM-DD",
  "grn_exists": true,
  "has_qr_code": false,
  "digital_signature_present": false,
  "digital_signature_valid": null,
  "existing_invoice_number": null,
  "line_items": [
    {
      "line_id": 1,
      "po_qty": 100,
      "invoice_qty": 112,
      "po_unit_rate": 5000,
      "invoice_unit_rate": 5000,
      "grn_qty": 100
    }
  ]
}
```

---

## Bonus Features Implemented

| Feature | Status | Details |
|---------|--------|---------|
| Web UI | ✅ | `app.py` + `frontend/index.html` — 7-tab SPA with Tailwind CSS, Alpine.js, Chart.js |
| Rule Execution Engine | ✅ | `rule_engine.py` — evaluates all 34 rules, computes derived fields, returns disposition + reasons |
| Email Notifications | ✅ | `notification.py` — Section 6-compliant HTML/plain-text emails, port 465 SSL, multi-recipient support |
| Conflict Detection | ✅ | `conflict_detector.py` — 4 curated conflicts + programmatic overlap & coverage gap analysis |
| Confidence Scoring | ✅ | Each rule has a `confidence` field (0–1); low-confidence rules flagged for human review |
| Traceability | ✅ | Every rule has `source_clause` mapping back to the exact policy section |
| LLM Extraction | ✅ | `rule_extractor.py` — Claude (Anthropic) with structured JSON prompting; toggle via `use_llm` in config |
| Downloadable Output | ✅ | Rules and conflicts downloadable as JSON directly from the web UI |
| Config-driven | ✅ | All settings in `config.yaml` — no `.env` file; SMTP and recipients hot-reload without restart |
| Multi-Document Support | 🔲 | `parser.py` and `rule_extractor.py` are document-agnostic — pass any PDF path in `config.yaml` |
| Visual Rule Graph | 🔲 | Not implemented (extension opportunity) |

---

## Traceability Map

| Rule ID | Policy Section | Description |
|---------|---------------|-------------|
| AP-VAL-001 | Section 1.1 | Mandatory fields |
| AP-VAL-002 | Section 1.2 | Future-dated invoice rejection |
| AP-VAL-003 | Section 1.3 | Handwritten invoice >50K |
| AP-VAL-004 | Section 1.4 | Duplicate detection |
| AP-PO-001 | Section 2.1 | PO reference validation |
| AP-PO-002 | Section 2.2(a) | Auto-approve ±1% |
| AP-PO-003 | Section 2.2(b) | Dept Head at 1–10% |
| AP-PO-004 | Section 2.2(c) | Finance Controller at ≥10% |
| AP-PO-005 | Section 2.2(d) | Under-invoicing flag |
| AP-PO-006 | Section 2.3(b) | Invoice Qty > PO Qty |
| AP-PO-007 | Section 2.3(c) | Unit rate >2% mismatch |
| AP-GRN-001 | Section 3.1 | GRN required |
| AP-GRN-002 | Section 3.2(b) | Invoice Qty > GRN Qty |
| AP-GRN-003 | Section 3.3 | GRN post-dated |
| AP-TAX-001 | Section 4.1 | GSTIN validation |
| AP-TAX-002 | Section 4.2 | PAN-GSTIN cross-check |
| AP-TAX-003 | Section 4.3(a) | Tax calculation verification |
| AP-TAX-004 | Section 4.3(b) | Intra-state CGST=SGST, no IGST |
| AP-TAX-005 | Section 4.3(c) | Inter-state IGST only |
| AP-TAX-006 | Section 4.4 | Place of supply mismatch |
| AP-APR-001 | Section 5.1 | Auto-approve ≤1L |
| AP-APR-002 | Section 5.2 | Dept Head 1L–10L |
| AP-APR-003 | Section 5.3 | Finance Controller 10L–50L |
| AP-APR-004 | Section 5.4 | CFO >50L |
| AP-APR-005 | Section 5.5 | Watchlist vendor exception |
| AP-NOT-001 | Section 6.1 | 15-minute deviation notification |
| AP-NOT-002 | Section 6.2 | Notification content requirements |
| AP-NOT-003 | Section 6.3 | 48-hour escalation |
| AP-NOT-004 | Section 6.4 | Critical deviation immediate email |
| AP-QR-001 | Section 7.1 | QR code required >10L |
| AP-QR-002 | Section 7.2 | QR code data validation |
| AP-QR-003 | Section 7.3 | Digital signature validation |
