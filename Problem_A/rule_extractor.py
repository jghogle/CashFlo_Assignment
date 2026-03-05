"""
rule_extractor.py — LLM-based rule extraction with baseline fallback.

Strategy:
  1. If anthropic.api_key is set in config.yaml, use Claude to extract rules.
  2. Otherwise, load the pre-extracted baseline rules from output/extracted_rules.json.

Each extracted rule follows the canonical schema:
  rule_id, source_clause, description, category, priority, scope,
  condition, action, action_details, requires_justification, notification,
  confidence, tags
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import (
    ANTHROPIC_API_KEY,
    BASELINE_RULES_PATH,
    CLAUDE_MODEL,
    LLM_RULES_PATH,
    USE_LLM,
)
from parser import ParsedDocument


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""
You are an expert AP (Accounts Payable) automation engineer specialising in rule extraction.
Your task is to read a policy document and extract every business rule as structured JSON.

════════════════════════════════════════
SCHEMA — each rule must have ALL fields:
════════════════════════════════════════
{
  "rule_id":               "<CATEGORY-PREFIX>-<SEQ>",
  "source_clause":         "Section X.Y(z)",
  "description":           "<clear one-sentence description of the rule>",
  "category":              "<BASIC_VALIDATION | PO_MATCHING | GRN_MATCHING | TAX_COMPLIANCE | APPROVAL_MATRIX | DEVIATION_NOTIFICATIONS | QR_VALIDATION>",
  "priority":              <integer — lower number runs first>,
  "scope":                 "<INVOICE | LINE_ITEM>",
  "condition": {
    <nested condition tree — see formats below>
  },
  "action":                "<ACTION_CONSTANT>",
  "action_details":        { "status": "...", "reason": "...", "route_to": "..." },
  "requires_justification": <true | false>,
  "notification":          <null | { "type": "email", "to": [...], "trigger": "...", "within_minutes": N, "include_fields": [...] }>,
  "confidence":            <float 0.0–1.0 — how certain you are about this extraction>,
  "tags":                  ["tag1", "tag2", ...]
}

════════════════════════════════════════
CONDITION TREE FORMAT
════════════════════════════════════════
Simple leaf node:
  { "field": "<invoice_field>", "op": "<gt|lt|gte|lte|eq|neq|is_null|not_null>",
    "value": <literal>            — OR —
    "value_field": "<field_ref>"  — OR —
    "expr": "<arithmetic formula e.g. po_amount * 1.10>" }

Compound node:
  { "operator": "<AND|OR|NOT>", "operands": [ <condition>, <condition>, ... ] }

════════════════════════════════════════
ACTION CONSTANTS (use verbatim)
════════════════════════════════════════
  AUTO_APPROVE, REJECT, HOLD, FLAG_INCOMPLETE, FLAG_WARNING, FLAG_ERROR,
  FLAG_DUPLICATE, ROUTE_FOR_APPROVAL, ESCALATE, COMPLIANCE_HOLD,
  FLAG_AND_ROUTE, SEND_NOTIFICATION, SEND_CRITICAL_NOTIFICATION,
  ESCALATE_NOTIFICATION, VALIDATE_NOTIFICATION_CONTENT

════════════════════════════════════════
EXAMPLE OUTPUT (one rule shown)
════════════════════════════════════════
[
  {
    "rule_id": "AP-TWM-001",
    "source_clause": "Section 2.2(c)",
    "description": "Escalate invoice if amount exceeds PO by >= 10%",
    "category": "PO_MATCHING",
    "priority": 40,
    "scope": "INVOICE",
    "condition": {
      "operator": "AND",
      "operands": [
        { "field": "invoice_total", "op": "gte", "expr": "po_amount * 1.10" },
        { "field": "deviation_pct", "op": "gte", "value": 10 }
      ]
    },
    "action": "ESCALATE",
    "action_details": {
      "status": "ESCALATED",
      "reason": "Invoice amount exceeds PO by 10% or more — mandatory justification required",
      "route_to": "FINANCE_CONTROLLER"
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
]

════════════════════════════════════════
INSTRUCTIONS
════════════════════════════════════════
- Extract EVERY rule in the document — do not skip any clause.
- Each condition must be machine-executable (no vague English phrases as values).
- Use "expr" for formulas that reference other fields (e.g. "po_amount * 0.99").
- Use "value_field" when the comparison is between two invoice fields.
- Use "value" for literal constants (numbers, strings, booleans).
- Set confidence < 0.90 for any rule you are uncertain about.
- Return ONLY a valid JSON array — no prose, no markdown fences, no explanation.
""").strip()

_CONFLICT_SYSTEM_PROMPT = textwrap.dedent("""
You are an expert in AP policy analysis. Given a list of extracted rules, identify
contradictions, overlaps, or gaps. For each conflict output:
{
  "conflict_id": "AP-CONFLICT-<N>",
  "severity": "<HIGH|MEDIUM|LOW>",
  "title": "<short title>",
  "description": "<detailed explanation of the conflict>",
  "conflicting_rules": ["rule_id_1", "rule_id_2"],
  "example_scenario": "<concrete example>",
  "recommendation": "<suggested resolution>"
}
Return ONLY a valid JSON array — no prose, no markdown fences.
""").strip()


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str, model: str = CLAUDE_MODEL) -> str:
    """Call Anthropic Claude and return the assistant text response."""
    try:
        import anthropic
    except ImportError as exc:
        raise ImportError("anthropic package required: pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=8096,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0,
    )
    return message.content[0].text if message.content else "{}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk_document(doc: ParsedDocument, max_chars: int = 6000) -> List[str]:
    """
    Split a long document into overlapping chunks so each fits in one LLM call.
    Each chunk includes the full section context.
    """
    chunks: List[str] = []
    current_chunk: List[str] = []
    current_len = 0

    for section in doc.sections:
        section_text = f"Section {section.number}: {section.title}\n{section.raw_text}\n"
        if current_len + len(section_text) > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = current_chunk[-1:]
            current_len = len(current_chunk[0]) if current_chunk else 0

        current_chunk.append(section_text)
        current_len += len(section_text)

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks or [doc.full_text[:max_chars]]


def _parse_llm_json(raw: str) -> List[Dict]:
    """
    Parse JSON from Claude's response.
    Claude may return a bare array or wrap it in a key.
    """
    # Strip any accidental markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        for key in ("rules", "extracted_rules", "result", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        return [data] if data else []
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return []


def _assign_confidence(rule: Dict, section_text: str) -> float:
    """Heuristic confidence adjustment based on extraction quality."""
    score = rule.get("confidence", 0.85)
    if not rule.get("condition"):
        score -= 0.15
    if not rule.get("source_clause"):
        score -= 0.10
    if rule.get("description") and len(rule["description"]) > 20:
        score += 0.02
    return round(min(max(score, 0.0), 1.0), 2)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def extract_rules_with_llm(doc: ParsedDocument) -> Dict[str, Any]:
    """
    Use Anthropic Claude to extract rules from the parsed policy document.
    Returns a dict matching the structure of extracted_rules.json.
    """
    print(f"[Claude] Extracting rules using model: {CLAUDE_MODEL}")
    all_rules: List[Dict] = []
    chunks = _chunk_document(doc)

    for i, chunk in enumerate(chunks):
        print(f"[Claude] Processing chunk {i + 1}/{len(chunks)} …")
        user_msg = (
            "Extract ALL business rules from the following AP policy text.\n\n"
            f"Policy text:\n{chunk}"
        )
        try:
            raw = _call_claude(_SYSTEM_PROMPT, user_msg)
            rules = _parse_llm_json(raw)
            for rule in rules:
                rule["confidence"] = _assign_confidence(rule, chunk)
            all_rules.extend(rules)
        except Exception as exc:
            print(f"[Claude] Warning: chunk {i + 1} failed — {exc}")

    # Deduplicate by rule_id
    seen: Dict[str, Dict] = {}
    for rule in all_rules:
        rid = rule.get("rule_id", "UNKNOWN")
        seen[rid] = rule
    deduped = list(seen.values())

    # Conflict detection via Claude
    conflicts: List[Dict] = []
    if deduped:
        print("[Claude] Detecting conflicts …")
        conflict_user = (
            "Here are the extracted rules. Identify any contradictions or overlaps:\n\n"
            + json.dumps(deduped, indent=2)
        )
        try:
            raw_c = _call_claude(_CONFLICT_SYSTEM_PROMPT, conflict_user)
            conflicts = _parse_llm_json(raw_c)
        except Exception as exc:
            print(f"[Claude] Conflict detection failed — {exc}")

    result = {
        "metadata": {
            "policy_document": Path(BASELINE_RULES_PATH).name,
            "extraction_method": "claude_llm",
            "model": CLAUDE_MODEL,
            "total_rules": len(deduped),
        },
        "rules": deduped,
        "conflicts": conflicts,
    }

    LLM_RULES_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[Claude] Saved {len(deduped)} rules to {LLM_RULES_PATH}")
    return result


# ---------------------------------------------------------------------------
# Baseline (pre-extracted) loader
# ---------------------------------------------------------------------------

def load_baseline_rules() -> Dict[str, Any]:
    """Load the hand-curated baseline rules from output/extracted_rules.json."""
    if not BASELINE_RULES_PATH.exists():
        raise FileNotFoundError(
            f"Baseline rules not found at {BASELINE_RULES_PATH}. "
            "Set a valid api_key in config.yaml to run LLM extraction, or "
            "ensure output/extracted_rules.json exists."
        )
    data = json.loads(BASELINE_RULES_PATH.read_text(encoding="utf-8"))
    print(f"[Baseline] Loaded {len(data.get('rules', []))} rules from {BASELINE_RULES_PATH}")
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_rules(
    doc: Optional[ParsedDocument] = None,
    force_baseline: bool = False,
) -> Dict[str, Any]:
    """
    Main entry point.

    Decision order:
      1. If llm.use_llm is false in config.yaml  → always use cached baseline (no API call).
      2. If api_key is missing / placeholder      → use cached baseline.
      3. Otherwise                                → call Claude.
    """
    if not USE_LLM or force_baseline:
        if not USE_LLM:
            print("[Extractor] llm.use_llm=false in config.yaml — using cached baseline rules (no API call).")
        return load_baseline_rules()

    key = ANTHROPIC_API_KEY
    if not key or key.startswith("your-"):
        print("[Extractor] No Anthropic API key in config.yaml — using baseline rules.")
        return load_baseline_rules()

    if doc is None:
        raise ValueError("ParsedDocument required for LLM extraction")
    return extract_rules_with_llm(doc)


def print_rule_summary(rule_data: Dict[str, Any]) -> None:
    """Pretty-print a summary of extracted rules to stdout."""
    rules = rule_data.get("rules", [])
    conflicts = rule_data.get("conflicts", [])
    print(f"\n{'='*60}")
    print(f"  EXTRACTED RULES SUMMARY")
    print(f"{'='*60}")
    print(f"  Total rules    : {len(rules)}")
    print(f"  Total conflicts: {len(conflicts)}")
    print()

    by_cat: Dict[str, List] = {}
    for r in rules:
        cat = r.get("category", "UNCATEGORISED")
        by_cat.setdefault(cat, []).append(r)

    for cat, cat_rules in sorted(by_cat.items()):
        print(f"  [{cat}] — {len(cat_rules)} rules")
        for r in cat_rules:
            conf = r.get("confidence", 0)
            flag = "⚠" if conf < 0.90 else " "
            print(f"    {flag} {r.get('rule_id','?'):15s}  {r.get('source_clause','?'):18s}  conf={conf:.2f}")

    if conflicts:
        print(f"\n  CONFLICTS DETECTED:")
        for c in conflicts:
            sev = c.get("severity", "?")
            print(f"    [{sev}] {c.get('conflict_id','?')}: {c.get('title','')}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    from config import POLICY_PDF_PATH
    from parser import parse_pdf

    parsed = parse_pdf(POLICY_PDF_PATH)
    data = extract_rules(parsed)
    print_rule_summary(data)
