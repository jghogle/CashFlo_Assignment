"""
notification.py — Email notification module (Section 6 compliance).

By default, notifications are simulated (printed to console + saved to
output/notifications_log.json). If SMTP credentials are provided in .env,
real emails are sent via smtplib.

Notification schema (Section 6.2):
  Invoice Number, Vendor Name, PO Number, Deviation Type,
  Deviation Details (expected vs actual), Recommended Action.
"""

from __future__ import annotations

import json
import smtplib
import textwrap
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import OUTPUT_DIR, get_notifications_config


# ---------------------------------------------------------------------------
# Email template builder
# ---------------------------------------------------------------------------

_DEVIATION_LABELS = {
    "REJECT":                   "Invoice Rejected",
    "HOLD":                     "Invoice Held",
    "ESCALATE":                 "Invoice Escalated",
    "FLAG_WARNING":             "Warning Flag Raised",
    "FLAG_ERROR":               "Error Flag Raised",
    "FLAG_AND_ROUTE":           "Flagged and Routed",
    "COMPLIANCE_HOLD":          "Compliance Hold",
    "FLAG_INCOMPLETE":          "Incomplete Invoice",
    "FLAG_DUPLICATE":           "Potential Duplicate",
    "ROUTE_FOR_APPROVAL":       "Routed for Approval",
    "SEND_CRITICAL_NOTIFICATION": "Critical Deviation",
    "ESCALATE_NOTIFICATION":    "Escalation — SLA Breach",
}

_RECOMMENDED_ACTIONS = {
    "REJECT":               "Please review and correct the invoice before resubmission.",
    "HOLD":                 "Resolve the outstanding issue to release the invoice for processing.",
    "ESCALATE":             "Provide a mandatory justification note for the Finance Controller to review.",
    "FLAG_WARNING":         "Verify the flagged data and update the invoice or master records as needed.",
    "FLAG_ERROR":           "Correct the error on the invoice and resubmit for processing.",
    "FLAG_AND_ROUTE":       "Procurement team to verify and confirm or reject the deviation.",
    "COMPLIANCE_HOLD":      "Contact the Compliance team to resolve the PAN/GSTIN mismatch.",
    "ROUTE_FOR_APPROVAL":   "Please approve or reject the invoice in the AP system within 48 hours.",
    "SEND_CRITICAL_NOTIFICATION": "Immediate review required by Finance Controller and Internal Audit.",
    "ESCALATE_NOTIFICATION": "SLA breached. Escalated to next-level approver for immediate action.",
}


def _resolve_recipients(to_list: List[str], cfg: Dict[str, Any]) -> List[str]:
    """
    Re-reads notifications.to and stakeholders from config.yaml at call time
    so edits to config.yaml are reflected immediately (no restart needed).

    notifications.to can be:
      - a single email string  → sends to that address
      - a list of email strings → sends to all of them
      - empty / placeholder    → falls back to per-rule stakeholder roles
    """
    raw = cfg.get("to", "")

    # Normalise to a list
    if isinstance(raw, list):
        configured = [e for e in raw if e and not str(e).startswith("your-")]
    elif isinstance(raw, str) and raw and not raw.startswith("your-"):
        configured = [raw]
    else:
        configured = []

    if configured:
        return configured

    # Fall back to per-rule stakeholder role mapping
    stakeholders = cfg.get("stakeholders", {})
    return [
        stakeholders.get(role.upper(), f"{role.lower()}@company.com")
        for role in to_list
    ]


def _build_email_body(notification: Dict[str, Any], invoice: Dict[str, Any]) -> str:
    """Compose a plain-text email body compliant with Section 6.2."""
    action = notification.get("deviation_type", "UNKNOWN")
    dev_label = _DEVIATION_LABELS.get(action, action.replace("_", " ").title())
    rec_action = _RECOMMENDED_ACTIONS.get(action, "Please review and take appropriate action.")

    # Derive expected vs actual for key deviations
    po_amount = invoice.get("po_amount", "N/A")
    inv_total = invoice.get("invoice_total", "N/A")
    deviation_details = notification.get("reason", "See AP system for details.")

    body = textwrap.dedent(f"""
        ══════════════════════════════════════════════════════
         ACCOUNTS PAYABLE SYSTEM — DEVIATION NOTIFICATION
        ══════════════════════════════════════════════════════

        Invoice Number   : {invoice.get('invoice_number', 'N/A')}
        Vendor Name      : {invoice.get('vendor_name', 'N/A')}
        PO Number        : {invoice.get('po_number', 'N/A')}
        Invoice Amount   : INR {inv_total:,} (if numeric) or {inv_total}
        PO Amount        : INR {po_amount:,} (if numeric) or {po_amount}

        ──────────────────────────────────────────────────────
        Deviation Type   : {dev_label}
        Deviation Details: {deviation_details}

        Recommended Action:
          {rec_action}
        ──────────────────────────────────────────────────────

        Triggered Rule   : {notification.get('triggered_by', 'N/A')}
        Detected At      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        Notification SLA : Within {notification.get('within_minutes', 15)} minute(s)

        Please log into the AP system to review and act on this invoice.

        ══════════════════════════════════════════════════════
        This is an automated message from the Cashflo AP System.
        Do not reply to this email.
    """).strip()

    return body


def _build_html_body(notification: Dict[str, Any], invoice: Dict[str, Any]) -> str:
    """Build an HTML email body for rich-text email clients."""
    action = notification.get("deviation_type", "UNKNOWN")
    dev_label = _DEVIATION_LABELS.get(action, action.replace("_", " ").title())
    rec_action = _RECOMMENDED_ACTIONS.get(action, "Please review and take appropriate action.")

    color = "#c0392b" if "REJECT" in action or "CRITICAL" in action else (
        "#e67e22" if "HOLD" in action or "ESCALATE" in action else "#2980b9"
    )

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:650px;margin:auto;">
  <div style="background:{color};color:#fff;padding:16px 24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0">AP Deviation Notification</h2>
    <p style="margin:4px 0 0">{dev_label}</p>
  </div>
  <div style="border:1px solid #ddd;border-top:none;padding:24px;border-radius:0 0 6px 6px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:6px 0;color:#555;width:160px"><strong>Invoice Number</strong></td>
          <td>{invoice.get('invoice_number','N/A')}</td></tr>
      <tr><td style="padding:6px 0;color:#555"><strong>Vendor Name</strong></td>
          <td>{invoice.get('vendor_name','N/A')}</td></tr>
      <tr><td style="padding:6px 0;color:#555"><strong>PO Number</strong></td>
          <td>{invoice.get('po_number','N/A')}</td></tr>
      <tr><td style="padding:6px 0;color:#555"><strong>Invoice Amount</strong></td>
          <td>INR {invoice.get('invoice_total','N/A'):,}</td></tr>
      <tr><td style="padding:6px 0;color:#555"><strong>PO Amount</strong></td>
          <td>INR {invoice.get('po_amount','N/A'):,}</td></tr>
    </table>
    <hr style="margin:16px 0;border:none;border-top:1px solid #eee;">
    <p><strong>Deviation Details:</strong><br>{notification.get('reason','N/A')}</p>
    <p><strong>Recommended Action:</strong><br>{rec_action}</p>
    <hr style="margin:16px 0;border:none;border-top:1px solid #eee;">
    <p style="font-size:12px;color:#888;">
      Rule: {notification.get('triggered_by','N/A')} &nbsp;|&nbsp;
      Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
      SLA: {notification.get('within_minutes',15)} min
    </p>
    <p style="font-size:11px;color:#aaa;">
      Automated message from the Cashflo AP System. Do not reply.
    </p>
  </div>
</body></html>
"""
    return html


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _send_via_smtp(
    recipients: List[str],
    subject: str,
    plain_body: str,
    html_body: str,
    cfg: Dict[str, Any],
) -> bool:
    """Send email via SMTP using live config values.

    Port 465 → SSL (smtplib.SMTP_SSL).
    Port 587 or anything else → STARTTLS (smtplib.SMTP).
    Falls back to simulation and prints a clear error if sending fails.
    """
    import smtplib, ssl as _ssl
    sender = cfg["smtp_from"]
    port   = int(cfg["smtp_port"])

    msg = MIMEMultipart("alternative")
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = _ssl.create_default_context()
        if port == 465:
            # SSL from the start
            with smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=15, context=ctx) as server:
                server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.sendmail(sender, recipients, msg.as_string())
        else:
            # STARTTLS (port 587)
            with smtplib.SMTP(cfg["smtp_host"], port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.sendmail(sender, recipients, msg.as_string())
        print(f"  [SMTP] Email sent to {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"  [SMTP] Failed to send email: {exc}")
        print("  [SMTP] Falling back to simulation.")
        return False


def _simulate_send(
    recipients: List[str],
    subject: str,
    plain_body: str,
    notification: Dict,
) -> None:
    """Simulate email delivery by printing to console."""
    print(f"\n  {'─'*52}")
    print(f"  📧 SIMULATED EMAIL")
    print(f"  To     : {', '.join(recipients)}")
    print(f"  Subject: {subject}")
    print(f"  {'─'*52}")
    for line in plain_body.splitlines()[:20]:
        print(f"  {line}")
    print(f"  … (full body logged to notifications_log.json)")
    print(f"  {'─'*52}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dispatch_notifications(
    notifications: List[Dict],
    invoice: Dict[str, Any],
    simulate: bool = True,
) -> List[Dict]:
    """
    Dispatch all pending notifications for an invoice.

    Reads config.yaml fresh on every call — edit notifications.to or
    smtp settings in config.yaml and they take effect immediately,
    no server restart required.

    Args:
        notifications : list of notification dicts from RuleEngine output.
        invoice       : original invoice dict (for context fields).
        simulate      : if True, print to console instead of sending real email.

    Returns:
        List of notification log entries.
    """
    # ── Re-read config.yaml fresh every dispatch ──────────────────────────────
    cfg = get_notifications_config()
    simulate = cfg["simulate"]   # honour config.yaml, override arg default

    log: List[Dict] = []

    for notif in notifications:
        to_roles   = notif.get("to", [])
        recipients = _resolve_recipients(to_roles, cfg)
        action     = notif.get("deviation_type", "DEVIATION")
        dev_label  = _DEVIATION_LABELS.get(action, action.replace("_", " ").title())
        subject = (
            f"[AP Alert] {dev_label} — Invoice {invoice.get('invoice_number', 'N/A')} "
            f"| Vendor: {invoice.get('vendor_name', 'N/A')}"
        )
        plain_body = _build_email_body(notif, invoice)
        html_body  = _build_html_body(notif, invoice)

        sent = False
        if not simulate and cfg["smtp_user"] and cfg["smtp_password"]:
            sent = _send_via_smtp(recipients, subject, plain_body, html_body, cfg)
        else:
            _simulate_send(recipients, subject, plain_body, notif)
            sent = True

        log_entry = {
            "timestamp":    datetime.now().isoformat(),
            "triggered_by": notif.get("triggered_by"),
            "invoice_id":   invoice.get("invoice_number"),
            "recipients":   recipients,
            "subject":      subject,
            "delivered":    sent,
            "simulated":    simulate,
            "body_preview": plain_body[:300],
        }
        log.append(log_entry)

    # Persist log
    log_path = OUTPUT_DIR / "notifications_log.json"
    existing: List[Dict] = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text())
        except json.JSONDecodeError:
            existing = []
    existing.extend(log)
    log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    if log:
        print(f"\n  [Notifications] {len(log)} notification(s) dispatched. Log → {log_path}")

    return log
