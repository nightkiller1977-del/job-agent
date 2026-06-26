"""
Agent notification system.

Writes structured alerts to state/agent_status.json and fires macOS
notifications so Desktop Commander / the user sees failures immediately
without watching logs.

Usage:
    from src.notifier import notify_error, notify_success, notify_warning
    notify_error("CVS Health login failed", "Add COMPANY_PASSWORD to .env")
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

STATUS_FILE = Path(__file__).parent.parent / "state" / "agent_status.json"


def _load_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {"alerts": [], "last_run": None, "applied": 0, "failed": 0}


def _save_status(data: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def _macos_notify(title: str, message: str, subtitle: str = "Job Agent") -> None:
    """Fire a native macOS notification via osascript."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" '
            f'subtitle "{subtitle}"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Never let notification failures crash the agent


def _add_alert(level: str, title: str, detail: str) -> None:
    status = _load_status()
    status.setdefault("alerts", [])
    status["last_run"] = datetime.utcnow().isoformat()

    # Keep only the last 50 alerts
    status["alerts"] = status["alerts"][-49:] + [{
        "level": level,        # "error" | "warning" | "success" | "info"
        "title": title,
        "detail": detail,
        "ts": datetime.utcnow().isoformat(),
    }]
    _save_status(status)


# ── Public API ────────────────────────────────────────────────────────────────

def notify_error(title: str, detail: str = "") -> None:
    """Signal a hard failure — missing credentials, crash, etc."""
    _add_alert("error", title, detail)
    _macos_notify(f"🔴 {title}", detail or title, subtitle="Job Agent ERROR")


def notify_warning(title: str, detail: str = "") -> None:
    """Signal something degraded but not fatal — login retry, skipped job."""
    _add_alert("warning", title, detail)
    _macos_notify(f"🟡 {title}", detail or title, subtitle="Job Agent WARNING")


def notify_success(title: str, detail: str = "") -> None:
    """Signal a successful application submission."""
    _add_alert("success", title, detail)
    _macos_notify(f"✅ {title}", detail or title, subtitle="Job Agent")


def notify_info(title: str, detail: str = "") -> None:
    """Informational — run started, jobs discovered, etc."""
    _add_alert("info", title, detail)
    # Don't fire a macOS popup for info — just write to status file


def record_run_stats(applied: int, failed: int, skipped: int) -> None:
    """Update the high-level counters after an apply run."""
    status = _load_status()
    status["last_run"] = datetime.utcnow().isoformat()
    status["applied"] = status.get("applied", 0) + applied
    status["failed"] = status.get("failed", 0) + failed
    status["skipped"] = status.get("skipped", 0) + skipped
    _save_status(status)


def record_reauth_event(source: str, mode: str, outcome: str, detail: str = "") -> None:
    """Append a reauth attempt to state/agent_status.json under 'reauth_events'."""
    status = _load_status()
    status.setdefault("reauth_events", [])
    status["reauth_events"] = status["reauth_events"][-99:] + [{
        "source": source,
        "mode": mode,       # "automated" | "human_notified" | "human"
        "outcome": outcome, # "success" | "failed" | "waiting" | "session_refreshed" | "timeout"
        "detail": detail,
        "ts": datetime.utcnow().isoformat(),
    }]
    _save_status(status)


def get_status() -> dict:
    """Return the current status dict — used by the dashboard / Desktop Commander."""
    return _load_status()
