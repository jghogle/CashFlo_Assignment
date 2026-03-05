"""
config.py — Loads all settings from config.yaml.

Edit config.yaml (never a .env file) to change API keys, model, SMTP, etc.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

if not _CONFIG_PATH.exists():
    raise FileNotFoundError(
        f"config.yaml not found at {_CONFIG_PATH}. "
        "Copy config.yaml and fill in your Anthropic API key."
    )

def _load() -> Dict[str, Any]:
    """Read config.yaml fresh from disk every time this is called."""
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get(path: str, default: Any = None, cfg: Dict[str, Any] | None = None) -> Any:
    """Dot-path accessor into the YAML tree, e.g. 'llm.api_key'."""
    node = cfg if cfg is not None else _load()
    for k in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
        if node is default:
            return default
    return node


def get_notifications_config() -> Dict[str, Any]:
    """Re-read config.yaml and return the full notifications section.

    Always reads from disk so changes to config.yaml are reflected immediately
    without restarting the server.
    """
    cfg = _load()
    notif = cfg.get("notifications", {}) or {}
    smtp  = notif.get("smtp", {}) or {}
    # 'from' can live at notifications.from (preferred) or notifications.smtp.from
    sender = notif.get("from") or smtp.get("from", "ap-system@company.com")
    return {
        "to":            notif.get("to", []),   # str or list[str]
        "simulate":      bool(notif.get("simulate", True)),
        "smtp_from":     sender,
        "smtp_host":     smtp.get("host", "smtp.gmail.com"),
        "smtp_port":     int(smtp.get("port", 587)),
        "smtp_user":     smtp.get("user", sender),   # default to sender address
        "smtp_password": smtp.get("password", ""),
        "stakeholders":  cfg.get("stakeholders", {}),
    }


# ── Static config (read once at startup — these don't change at runtime) ─────
_cfg_once = _load()

ANTHROPIC_API_KEY: str = _get("llm.api_key", "", _cfg_once)
CLAUDE_MODEL: str      = _get("llm.model", "claude-3-7-sonnet-20250219", _cfg_once)
# When False, always use cached output/extracted_rules.json — no Claude API calls
USE_LLM: bool          = bool(_get("llm.use_llm", False, _cfg_once))

_raw_pdf_path  = _get("policy.pdf_path", "../Sample_AP_Policy_Document.pdf", _cfg_once)
POLICY_PDF_PATH: str = str((Path(__file__).parent / _raw_pdf_path).resolve())

OUTPUT_DIR: Path = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

BASELINE_RULES_PATH: Path = OUTPUT_DIR / "extracted_rules.json"
LLM_RULES_PATH: Path      = OUTPUT_DIR / "llm_extracted_rules.json"

# ── Notification shorthands (re-read live via get_notifications_config()) ────
# These are kept for backward-compat imports but notification.py now calls
# get_notifications_config() at dispatch time so edits to config.yaml
# take effect immediately without any restart.
_n = get_notifications_config()
SMTP_HOST:              str  = _n["smtp_host"]
SMTP_PORT:              int  = _n["smtp_port"]
SMTP_USER:              str  = _n["smtp_user"]
SMTP_PASSWORD:          str  = _n["smtp_password"]
NOTIFICATION_FROM:      str  = _n["smtp_from"]   # notifications.from in config.yaml
NOTIFICATION_TO:        str  = _n["to"]
SIMULATE_NOTIFICATIONS: bool = _n["simulate"]

STAKEHOLDER_EMAILS: Dict[str, str] = _n["stakeholders"] or {
    "AP_CLERK":           "ap-clerk@company.com",
    "AP_MANAGER":         "ap-manager@company.com",
    "DEPARTMENT_HEAD":    "dept-head@company.com",
    "FINANCE_CONTROLLER": "finance-controller@company.com",
    "CFO":                "cfo@company.com",
    "PROCUREMENT":        "procurement@company.com",
    "INTERNAL_AUDIT":     "internal-audit@company.com",
    "COMPLIANCE_TEAM":    "compliance@company.com",
}
