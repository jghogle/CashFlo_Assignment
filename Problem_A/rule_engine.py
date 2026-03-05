"""
rule_engine.py — Lightweight deterministic rule execution engine.

Evaluates every extracted rule against an invoice JSON and returns:
  - A list of RuleResult objects (PASS / FAIL / SKIPPED)
  - A final disposition (AUTO_APPROVED / PENDING_APPROVAL / REJECTED / HELD / FLAGGED)
  - A list of actions fired
  - Computed derived fields for the invoice (deviation %, tax error, etc.)

Condition evaluation supports:
  Operators : gt, lt, gte, lte, eq, neq, is_null, not_null
  Values    : literal, value_field (field reference), expr (safe math formula)
  Compound  : AND, OR, NOT operand trees
  Scopes    : INVOICE (evaluated once) and LINE_ITEM (evaluated per line item)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    rule_id: str
    source_clause: str
    description: str
    triggered: bool          # True = condition matched → action fires
    action: Optional[str]
    reason: Optional[str]
    route_to: Optional[str]
    requires_justification: bool
    notification: Optional[Dict]
    scope: str               # INVOICE | LINE_ITEM
    line_item_id: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class EngineOutput:
    invoice_id: str
    disposition: str                     # Final status
    final_approver: Optional[str]
    fired_actions: List[str]
    results: List[RuleResult]
    derived_fields: Dict[str, Any]
    notifications_pending: List[Dict]
    flags: List[str]
    errors: List[str]


# ---------------------------------------------------------------------------
# Safe expression evaluator
# ---------------------------------------------------------------------------

_SAFE_BUILTINS = {"abs": abs, "round": round, "min": min, "max": max, "math": math}


def _safe_eval(expr: str, context: Dict[str, Any]) -> Any:
    """Evaluate a simple arithmetic expression with field references."""
    try:
        return eval(expr, {"__builtins__": {}}, {**_SAFE_BUILTINS, **context})  # noqa: S307
    except Exception as exc:
        raise ValueError(f"Cannot evaluate expression '{expr}': {exc}") from exc


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

def _compare(actual: Any, op: str, expected: Any) -> bool:
    """Apply a comparison operator between actual and expected values."""
    # Date-aware comparison
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            a = datetime.fromisoformat(actual).date()
            b = datetime.fromisoformat(expected).date()
            actual, expected = a, b
        except ValueError:
            pass

    if op == "gt":
        return actual > expected
    if op == "lt":
        return actual < expected
    if op == "gte":
        return actual >= expected
    if op == "lte":
        return actual <= expected
    if op == "eq":
        return actual == expected
    if op == "neq":
        return actual != expected
    if op == "is_null":
        return actual is None or actual == "" or actual == 0
    if op == "not_null":
        return actual is not None and actual != ""
    raise ValueError(f"Unknown operator: {op}")


def _resolve_value(
    cond: Dict,
    context: Dict[str, Any],
) -> Tuple[Any, Any]:
    """
    Resolve the (actual, expected) pair from a simple condition node.
    Returns (actual_value, expected_value).
    """
    field_name = cond["field"]
    actual = context.get(field_name)

    if "expr" in cond:
        expected = _safe_eval(cond["expr"], context)
    elif "value_field" in cond:
        expected = context.get(cond["value_field"])
    elif "value" in cond:
        expected = cond["value"]
    else:
        # Existence check only (is_null / not_null)
        expected = None

    return actual, expected


def evaluate_condition(cond: Dict, context: Dict[str, Any]) -> bool:
    """
    Recursively evaluate a condition tree.
    Returns True if the condition is satisfied (rule should fire).
    """
    if not cond:
        return False

    # Compound node
    operator = cond.get("operator")
    if operator:
        operands = cond.get("operands", [])
        if operator == "AND":
            return all(evaluate_condition(o, context) for o in operands)
        if operator == "OR":
            return any(evaluate_condition(o, context) for o in operands)
        if operator == "NOT":
            return not evaluate_condition(operands[0], context) if operands else False
        raise ValueError(f"Unknown logical operator: {operator}")

    # Simple leaf node
    op = cond.get("op", "")
    actual, expected = _resolve_value(cond, context)
    return _compare(actual, op, expected)


# ---------------------------------------------------------------------------
# Derived field computation
# ---------------------------------------------------------------------------

def _compute_derived_fields(invoice: Dict) -> Dict[str, Any]:
    """
    Pre-compute derived fields that rules reference (deviation %, tax error, etc.).
    These are added to the evaluation context alongside the invoice fields.
    """
    derived: Dict[str, Any] = {}

    po_amount = invoice.get("po_amount", 0) or 0
    invoice_total = invoice.get("invoice_total", 0) or 0

    # Amount deviation %
    if po_amount:
        raw_dev = (invoice_total - po_amount) / po_amount * 100
        derived["amount_deviation_pct"] = raw_dev
        derived["abs_amount_deviation_pct"] = abs(raw_dev)
    else:
        derived["amount_deviation_pct"] = 0.0
        derived["abs_amount_deviation_pct"] = 0.0

    # Tax calculation error
    taxable = invoice.get("taxable_amount", 0) or 0
    tax = invoice.get("tax_amount", 0) or 0
    grand_total = invoice.get("grand_total", 0) or 0
    derived["abs_tax_calculation_error"] = abs((taxable + tax) - grand_total)

    # PAN embedded in GSTIN: chars index 2–11 (0-based) → PAN is 10 chars
    vendor_gstin = invoice.get("vendor_gstin", "") or ""
    derived["gstin_embedded_pan"] = vendor_gstin[2:12] if len(vendor_gstin) >= 12 else ""

    # Buyer GSTIN state code (first 2 chars)
    buyer_gstin = invoice.get("buyer_gstin", "") or ""
    derived["buyer_gstin_state_code"] = buyer_gstin[:2] if len(buyer_gstin) >= 2 else ""

    # Compliance failure flag
    derived["has_compliance_failure"] = False  # updated after tax rule evaluation

    # Deviation flags (updated as rules fire)
    derived["has_three_way_match_deviation"] = False
    derived["has_unresolved_deviation"] = False
    derived["deviation_age_hours"] = 0

    # Notification tracking
    derived["notification_sent"] = False

    # Duplicate detection shortcut
    derived["existing_invoice_number"] = invoice.get("existing_invoice_number")
    derived["existing_invoice_vendor_gstin"] = invoice.get("existing_invoice_vendor_gstin")

    # All-validations-passed placeholder (set to True, knocked down by rule engine)
    derived["all_validations_passed"] = True

    return derived


def _compute_line_item_derived(item: Dict, invoice: Dict) -> Dict[str, Any]:
    """Compute per-line-item derived fields."""
    d: Dict[str, Any] = {}
    po_rate = item.get("po_unit_rate", 0) or 0
    inv_rate = item.get("invoice_unit_rate", 0) or 0
    if po_rate:
        d["abs_rate_deviation_pct"] = abs((inv_rate - po_rate) / po_rate * 100)
    else:
        d["abs_rate_deviation_pct"] = 0.0
    return d


# ---------------------------------------------------------------------------
# Priority-ordered execution
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = {"REJECTED", "HELD", "AUTO_APPROVED"}

# Maps action → disposition (lower = higher precedence)
_DISPOSITION_PRIORITY = {
    "REJECT": ("REJECTED", 0),
    "HOLD": ("HELD", 1),
    "COMPLIANCE_HOLD": ("HELD", 2),
    "FLAG_DUPLICATE": ("HELD", 3),
    "FLAG_INCOMPLETE": ("FLAGGED", 4),
    "ESCALATE": ("ESCALATED", 5),
    "FLAG_ERROR": ("FLAGGED", 6),
    "FLAG_WARNING": ("FLAGGED", 7),
    "FLAG_AND_ROUTE": ("FLAGGED", 8),
    "ROUTE_FOR_APPROVAL": ("PENDING_APPROVAL", 9),
    "AUTO_APPROVE": ("AUTO_APPROVED", 10),
}


class RuleEngine:
    """Deterministic rule execution engine."""

    def __init__(self, rules: List[Dict]):
        # Sort rules by priority (ascending) so lower numbers run first
        self._rules = sorted(rules, key=lambda r: r.get("priority", 999))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self, invoice: Dict) -> EngineOutput:
        """Execute all rules against the invoice. Returns an EngineOutput."""
        derived = _compute_derived_fields(invoice)
        context = {**invoice, **derived}

        invoice_rules = [r for r in self._rules if r.get("scope", "INVOICE") == "INVOICE"]
        line_item_rules = [r for r in self._rules if r.get("scope") == "LINE_ITEM"]

        results: List[RuleResult] = []
        fired_actions: List[str] = []
        notifications_pending: List[Dict] = []
        flags: List[str] = []
        errors: List[str] = []
        disposition_stack: List[Tuple[int, str, Optional[str]]] = []  # (priority, status, approver)

        # --- Invoice-level rules ---
        for rule in invoice_rules:
            result = self._evaluate_rule(rule, context)
            results.append(result)

            if result.error:
                errors.append(f"{rule['rule_id']}: {result.error}")
                continue

            if result.triggered:
                self._apply_action(
                    result, context, derived,
                    fired_actions, notifications_pending, flags, disposition_stack,
                )
                # Knock down all_validations_passed for non-approve actions
                if result.action not in ("AUTO_APPROVE", "SEND_NOTIFICATION",
                                         "VALIDATE_NOTIFICATION_CONTENT",
                                         "ESCALATE_NOTIFICATION"):
                    context["all_validations_passed"] = False
                    derived["all_validations_passed"] = False

                # Mark compliance failure for tax / GSTIN rules
                if rule.get("category") in ("TAX_COMPLIANCE",) and result.triggered:
                    context["has_compliance_failure"] = True
                    derived["has_compliance_failure"] = True

        # --- Line-item rules ---
        line_items: List[Dict] = invoice.get("line_items", [])
        for item in line_items:
            li_derived = _compute_line_item_derived(item, invoice)
            li_context = {**context, **item, **li_derived}
            for rule in line_item_rules:
                result = self._evaluate_rule(rule, li_context, line_item_id=item.get("line_id"))
                results.append(result)

                if result.error:
                    errors.append(f"{rule['rule_id']}[line {item.get('line_id')}]: {result.error}")
                    continue

                if result.triggered:
                    self._apply_action(
                        result, context, derived,
                        fired_actions, notifications_pending, flags, disposition_stack,
                    )
                    # A line-item deviation = three-way match deviation
                    context["has_three_way_match_deviation"] = True
                    derived["has_three_way_match_deviation"] = True

        # --- Determine final disposition ---
        if disposition_stack:
            disposition_stack.sort(key=lambda x: x[0])
            _, final_status, final_approver = disposition_stack[0]
        else:
            final_status = "AUTO_APPROVED"
            final_approver = None

        return EngineOutput(
            invoice_id=invoice.get("invoice_number", "UNKNOWN"),
            disposition=final_status,
            final_approver=final_approver,
            fired_actions=fired_actions,
            results=results,
            derived_fields=derived,
            notifications_pending=notifications_pending,
            flags=list(set(flags)),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate_rule(
        self,
        rule: Dict,
        context: Dict[str, Any],
        line_item_id: Any = None,
    ) -> RuleResult:
        triggered = False
        error = None
        try:
            triggered = evaluate_condition(rule.get("condition", {}), context)
        except Exception as exc:
            error = str(exc)

        details = rule.get("action_details", {})
        return RuleResult(
            rule_id=rule.get("rule_id", "?"),
            source_clause=rule.get("source_clause", ""),
            description=rule.get("description", ""),
            triggered=triggered,
            action=rule.get("action") if triggered else None,
            reason=details.get("reason") if triggered else None,
            route_to=details.get("route_to") if triggered else None,
            requires_justification=rule.get("requires_justification", False) and triggered,
            notification=rule.get("notification") if triggered else None,
            scope=rule.get("scope", "INVOICE"),
            line_item_id=line_item_id,
            error=error,
        )

    @staticmethod
    def _apply_action(
        result: RuleResult,
        context: Dict,
        derived: Dict,
        fired_actions: List[str],
        notifications_pending: List[Dict],
        flags: List[str],
        disposition_stack: List[Tuple[int, str, Optional[str]]],
    ) -> None:
        action = result.action or ""
        fired_actions.append(f"{result.rule_id} → {action}")

        # Collect flags
        details_flag = None
        # We need rule action_details — retrieve from context (not available directly here)
        # Instead, we use reason as the flag label for simplicity

        # Update disposition stack
        if action in _DISPOSITION_PRIORITY:
            status, priority = _DISPOSITION_PRIORITY[action]
            disposition_stack.append((priority, status, result.route_to))

        # Queue notification
        if result.notification:
            notif = dict(result.notification)
            notif["triggered_by"] = result.rule_id
            notif["invoice_id"] = context.get("invoice_number", "UNKNOWN")
            notif["vendor_name"] = context.get("vendor_name", "UNKNOWN")
            notif["po_number"] = context.get("po_number", "UNKNOWN")
            notif["deviation_type"] = action
            notif["reason"] = result.reason
            notifications_pending.append(notif)

            # Mark three-way match deviation
            if action in ("HOLD", "REJECT", "FLAG_AND_ROUTE", "ESCALATE"):
                derived["has_three_way_match_deviation"] = True
                context["has_three_way_match_deviation"] = True

        # Collect flags
        if "flag" in (result.reason or "").lower() or action.startswith("FLAG"):
            flags.append(result.reason or action)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_engine_report(output: EngineOutput) -> None:
    """Pretty-print the engine execution report."""
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  RULE ENGINE REPORT — Invoice: {output.invoice_id}")
    print(sep)
    print(f"  Final Disposition : {output.disposition}")
    print(f"  Final Approver    : {output.final_approver or '—'}")
    print(f"  Rules Evaluated   : {len(output.results)}")
    print(f"  Rules Triggered   : {sum(1 for r in output.results if r.triggered)}")
    print(f"  Flags Raised      : {len(output.flags)}")
    print(f"  Notifications     : {len(output.notifications_pending)}")
    print(f"  Errors            : {len(output.errors)}")

    if output.flags:
        print(f"\n  FLAGS:")
        for f in output.flags:
            print(f"    • {f}")

    print(f"\n  TRIGGERED RULES:")
    triggered = [r for r in output.results if r.triggered]
    if not triggered:
        print("    (none)")
    for r in triggered:
        scope_tag = f"[line {r.line_item_id}]" if r.line_item_id else ""
        jstr = " [justification required]" if r.requires_justification else ""
        print(f"    ✗ {r.rule_id} ({r.source_clause}) {scope_tag}{jstr}")
        print(f"      → {r.action}  |  {r.reason}")
        if r.route_to:
            print(f"      → Route to: {r.route_to}")

    if output.errors:
        print(f"\n  EVALUATION ERRORS:")
        for e in output.errors:
            print(f"    ! {e}")

    print(f"\n  DERIVED FIELDS (key):")
    key_fields = [
        "amount_deviation_pct", "abs_amount_deviation_pct",
        "abs_tax_calculation_error", "has_compliance_failure",
        "has_three_way_match_deviation", "all_validations_passed",
    ]
    for k in key_fields:
        v = output.derived_fields.get(k, "—")
        if isinstance(v, float):
            v = f"{v:.2f}"
        print(f"    {k}: {v}")

    print(sep + "\n")
