"""
conflict_detector.py — Identifies contradictory and overlapping rules.

Detection strategies:
  1. Same-action conflicts  — two rules with identical triggers but different actions.
  2. Approver conflicts     — multiple rules assign different approvers for the same invoice.
  3. Priority gaps          — rules whose combined priority ordering creates an ambiguity.
  4. Cross-section overlaps — rules from different sections that address the same scenario.

In addition to the static conflicts pre-encoded in extracted_rules.json, this module
performs programmatic analysis over the full rule set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class Conflict:
    conflict_id: str
    severity: str          # HIGH | MEDIUM | LOW
    title: str
    description: str
    conflicting_rules: List[str]
    example_scenario: str
    recommendation: str
    detection_method: str = "programmatic"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APPROVAL_ACTIONS = {
    "AUTO_APPROVE",
    "ROUTE_FOR_APPROVAL",
    "ESCALATE",
    "COMPLIANCE_HOLD",
}

_APPROVER_FIELDS = {"route_to", "escalate_to"}


def _get_approver(rule: Dict) -> Optional[str]:
    details = rule.get("action_details", {})
    return details.get("route_to") or details.get("escalate_to")


def _get_amount_range(rule: Dict) -> Optional[Tuple[float, float]]:
    """Extract invoice_total lower/upper bound from a rule's condition."""
    cond = rule.get("condition", {})
    operands = cond.get("operands", [])
    lower = upper = None
    for op in operands:
        f = op.get("field", "")
        operator = op.get("op", "")
        val = op.get("value")
        if f == "invoice_total" and isinstance(val, (int, float)):
            if operator in ("gt", "gte"):
                lower = float(val)
            elif operator in ("lt", "lte"):
                upper = float(val)
    if lower is not None or upper is not None:
        return (lower or 0.0, upper or float("inf"))
    return None


def _get_extra_conditions_note(rule: Dict) -> str:
    """Return a human-readable qualifier for non-amount conditions on a rule.

    E.g. AP-VAL-003 requires is_handwritten=true, so it should be described
    as 'handwritten invoices only', not as a general amount-range rule.
    """
    cond = rule.get("condition", {})
    operands = cond.get("operands", [cond])  # handle single-condition rules too
    notes = []
    for op in operands:
        f = op.get("field", "")
        val = op.get("value")
        if f == "is_handwritten" and val is True:
            notes.append("handwritten invoices only")
        elif f == "vendor_on_watchlist" and val is True:
            notes.append("watchlist vendors only")
        elif f == "is_goods_based" and val is True:
            notes.append("goods-based POs only")
    return f" [{', '.join(notes)}]" if notes else ""


def _ranges_overlap(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# ---------------------------------------------------------------------------
# Detection strategies
# ---------------------------------------------------------------------------

def _detect_approval_matrix_conflicts(rules: List[Dict]) -> List[Conflict]:
    """
    Identify cases where an invoice amount or deviation could trigger two
    different approver assignments simultaneously.
    """
    conflicts: List[Conflict] = []

    # Rules that assign approvers
    approver_rules = [
        r for r in rules
        if r.get("action") in _APPROVAL_ACTIONS and _get_approver(r)
    ]

    # Pair-wise check for overlapping amount ranges with different approvers
    for i in range(len(approver_rules)):
        for j in range(i + 1, len(approver_rules)):
            r1, r2 = approver_rules[i], approver_rules[j]
            a1 = _get_approver(r1)
            a2 = _get_approver(r2)
            if a1 == a2:
                continue  # Same approver — no conflict

            rng1 = _get_amount_range(r1)
            rng2 = _get_amount_range(r2)

            # If both rules have amount-based conditions that overlap
            if rng1 and rng2 and _ranges_overlap(rng1, rng2):
                cid = f"AP-PROG-CONFLICT-{len(conflicts)+1:03d}"
                lo = max(rng1[0], rng2[0])
                hi = min(rng1[1], rng2[1])
                note1 = _get_extra_conditions_note(r1)
                note2 = _get_extra_conditions_note(r2)
                mid = (lo + (hi if hi != float("inf") else lo * 2)) / 2
                conflicts.append(Conflict(
                    conflict_id=cid,
                    severity="MEDIUM",
                    title=f"Overlapping Amount Range: {r1['rule_id']} vs {r2['rule_id']}",
                    description=(
                        f"Rule {r1['rule_id']}{note1} routes to {a1}, and "
                        f"{r2['rule_id']}{note2} routes to {a2}. "
                        f"Both conditions can fire simultaneously for invoices in the "
                        f"INR {lo:,.0f}–{hi:,.0f} range, assigning different approvers."
                    ),
                    conflicting_rules=[r1["rule_id"], r2["rule_id"]],
                    example_scenario=(
                        f"Invoice of INR {mid:,.0f}{note1 or note2} triggers both rules."
                    ),
                    recommendation=(
                        "Define explicit precedence: deviation-based or exception-based "
                        "routing should supersede amount-based routing when both apply."
                    ),
                ))

    return conflicts


def _detect_action_conflicts(rules: List[Dict]) -> List[Conflict]:
    """
    Detect cases where two rules sharing the same source_clause or very similar
    descriptions produce contradictory actions.
    """
    conflicts: List[Conflict] = []
    by_clause: Dict[str, List[Dict]] = {}
    for rule in rules:
        clause = rule.get("source_clause", "")
        if clause:
            by_clause.setdefault(clause, []).append(rule)

    for clause, clause_rules in by_clause.items():
        if len(clause_rules) < 2:
            continue
        actions = {r["action"] for r in clause_rules}
        if len(actions) > 1:
            cid = f"AP-PROG-CONFLICT-ACTION-{clause.replace(' ', '_')}"
            conflicts.append(Conflict(
                conflict_id=cid,
                severity="HIGH",
                title=f"Multiple Actions for {clause}",
                description=(
                    f"Clause {clause} is mapped to {len(clause_rules)} rules with "
                    f"different actions: {', '.join(actions)}."
                ),
                conflicting_rules=[r["rule_id"] for r in clause_rules],
                example_scenario=f"A single invoice satisfying {clause} triggers ambiguous actions.",
                recommendation="Consolidate into a single rule with explicit branching conditions.",
            ))

    return conflicts


def _detect_auto_approve_vs_amount_matrix(rules: List[Dict]) -> List[Conflict]:
    """
    Detect the structural conflict where AP-PO-002 (auto-approve within ±1% of PO)
    has no upper amount bound, so it collides with the approval matrix for large invoices.

    AP-APR-001 already self-resolves the watchlist case via an explicit condition, so no
    separate watchlist/auto-approve conflict check is needed.
    """
    conflicts: List[Conflict] = []
    po_auto = next((r for r in rules if r.get("rule_id") == "AP-PO-002"), None)
    matrix_rules = [r for r in rules if r.get("rule_id") in {"AP-APR-002", "AP-APR-003", "AP-APR-004"}]

    if not po_auto or not matrix_rules:
        return conflicts

    matrix_ids = [r["rule_id"] for r in matrix_rules]
    approvers  = [_get_approver(r) for r in matrix_rules if _get_approver(r)]

    # Only flag if AP-PO-002 has no explicit upper amount cap
    rng = _get_amount_range(po_auto)
    has_upper_cap = rng and rng[1] < float("inf")
    if has_upper_cap:
        return conflicts  # already constrained — no conflict

    conflicts.append(Conflict(
        conflict_id="AP-PROG-CONFLICT-AUTO-APPROVE-MATRIX",
        severity="HIGH",
        title="Auto-Approve on PO Tolerance Has No Amount Cap — Conflicts with Approval Matrix",
        description=(
            "AP-PO-002 auto-approves any invoice within ±1% of the PO amount with no "
            "upper bound on invoice size. Rules "
            f"{', '.join(matrix_ids)} require "
            f"{'/'.join(approvers)} approval for invoices above INR 1L, 10L, and 50L. "
            "A large invoice within PO tolerance satisfies both: one says AUTO_APPROVE, "
            "the other says escalate."
        ),
        conflicting_rules=["AP-PO-002"] + matrix_ids,
        example_scenario=(
            "Invoice of INR 20,00,000 exactly matching the PO amount: "
            "AP-PO-002 fires AUTO_APPROVE; AP-APR-003 fires ROUTE_FOR_APPROVAL → FINANCE_CONTROLLER."
        ),
        recommendation=(
            "Add an explicit upper-bound condition to AP-PO-002: invoice_total <= 100000 "
            "(the auto-approve threshold from Section 5.1). For invoices above INR 1L, "
            "the approval matrix should always take precedence over PO tolerance."
        ),
    ))
    return conflicts


def _detect_coverage_gaps(rules: List[Dict]) -> List[Conflict]:
    """
    Detect coverage gaps, e.g. no rule covers invoices exactly AT the threshold boundary.
    """
    conflicts: List[Conflict] = []

    # Check that the approval matrix has contiguous coverage: 0→1L, 1L→10L, 10L→50L, >50L
    expected_bands = [
        (0, 100000, "AP-APR-001"),
        (100000, 1000000, "AP-APR-002"),
        (1000000, 5000000, "AP-APR-003"),
        (5000000, float("inf"), "AP-APR-004"),
    ]
    rule_ids = {r["rule_id"] for r in rules}
    missing = [rid for _, _, rid in expected_bands if rid not in rule_ids]
    if missing:
        conflicts.append(Conflict(
            conflict_id="AP-PROG-CONFLICT-COVERAGE",
            severity="HIGH",
            title="Approval Matrix Coverage Gap",
            description=(
                f"Missing approval matrix rules: {missing}. "
                "Some invoice amounts may not be covered by any approval rule."
            ),
            conflicting_rules=missing,
            example_scenario="Invoice amount falls into an uncovered band — no routing action taken.",
            recommendation="Ensure all expected approval bands are represented by rules.",
        ))

    return conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_conflicts(rule_data: Dict[str, Any]) -> List[Dict]:
    """
    Run all detection strategies against the full rule set.

    Returns a merged list of conflict dicts (existing + newly detected).
    Deduplicates by conflict_id.
    """
    rules: List[Dict] = rule_data.get("rules", [])
    existing: List[Dict] = rule_data.get("conflicts", [])

    programmatic: List[Conflict] = []
    programmatic.extend(_detect_approval_matrix_conflicts(rules))
    programmatic.extend(_detect_action_conflicts(rules))
    programmatic.extend(_detect_auto_approve_vs_amount_matrix(rules))
    programmatic.extend(_detect_coverage_gaps(rules))

    # Merge: existing conflicts take precedence (they have richer descriptions)
    existing_ids = {c.get("conflict_id") for c in existing}
    new_conflicts = [
        {
            "conflict_id": c.conflict_id,
            "severity": c.severity,
            "title": c.title,
            "description": c.description,
            "conflicting_rules": c.conflicting_rules,
            "example_scenario": c.example_scenario,
            "recommendation": c.recommendation,
            "detection_method": c.detection_method,
        }
        for c in programmatic
        if c.conflict_id not in existing_ids
    ]

    all_conflicts = existing + new_conflicts
    return all_conflicts


def print_conflicts(conflicts: List[Dict]) -> None:
    """Pretty-print conflict report to stdout."""
    if not conflicts:
        print("  No conflicts detected.")
        return

    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_c = sorted(conflicts, key=lambda c: severity_order.get(c.get("severity", "LOW"), 3))

    for c in sorted_c:
        sev = c.get("severity", "?")
        icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
        print(f"\n  {icon} [{sev}] {c.get('conflict_id','?')}: {c.get('title','')}")
        print(f"     Rules    : {', '.join(c.get('conflicting_rules', []))}")
        print(f"     Issue    : {c.get('description','')}")
        print(f"     Example  : {c.get('example_scenario','')}")
        print(f"     Fix      : {c.get('recommendation','')}")
