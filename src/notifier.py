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
import re
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


import time

_last_notification_times: dict[str, float] = {}


def _sanitize_notification_text(value: str) -> str:
    text = str(value or "")
    home = str(Path.home())
    if home and home != "/":
        terminal_delimiters = ".,;:!?)]}'\"`>"
        protected_tokens: list[str] = []

        def protect_token(match: re.Match) -> str:
            protected_tokens.append(match.group(0))
            return f"__JOB_AGENT_HOME_TOKEN_{len(protected_tokens) - 1}__"

        spaced_sibling = rf"{re.escape(home)}(?:\s+[^\s{re.escape(terminal_delimiters)}/]+)+/[^\s{re.escape(terminal_delimiters)}]*"
        text = re.sub(spaced_sibling, protect_token, text)

        def quoted_token_end(start: int) -> int | None:
            token_start = max(text.rfind(" ", 0, start), text.rfind("\n", 0, start), text.rfind("\t", 0, start)) + 1
            prefix = text[token_start:start]
            opener = prefix[-1:] if prefix else ""
            if opener not in "'\"`":
                for wrapper in ("PosixPath(", "Path("):
                    wrapped = prefix.rfind(wrapper)
                    if wrapped >= 0 and len(prefix) > wrapped + len(wrapper):
                        candidate = prefix[wrapped + len(wrapper)]
                        if candidate in "'\"`":
                            opener = candidate
                            break
            if opener not in "'\"`":
                return None
            closing = text.find(opener, start)
            return closing if closing >= 0 else None

        def is_spaced_sibling_path(start: int, end: int) -> bool:
            token_end = quoted_token_end(start)
            if token_end is None:
                token_end = len(text)
                for pos in range(end + 1, len(text)):
                    if text[pos] in terminal_delimiters:
                        token_end = pos
                        break
            tail = text[end:token_end]
            slash = tail.find("/")
            return slash > 0 and not tail[slash - 1].isspace()

        def redact_home(match: re.Match) -> str:
            idx = match.end()
            if idx >= len(text):
                return "~"
            nxt = text[idx]
            if nxt == "/":
                return "~"
            if nxt.isspace():
                if is_spaced_sibling_path(match.start(), idx):
                    return match.group(0)
                return "~"
            if nxt in terminal_delimiters and (
                idx + 1 >= len(text)
                or text[idx + 1].isspace()
                or text[idx + 1] in terminal_delimiters
            ):
                return "~"
            return match.group(0)

        text = re.sub(rf"(?<![\w/.-]){re.escape(home)}", redact_home, text)
        for index, token in enumerate(protected_tokens):
            text = text.replace(f"__JOB_AGENT_HOME_TOKEN_{index}__", token)
    text = re.sub(
        r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)",
        "[phone]",
        text,
    )
    return text

def _load_telegram_config() -> tuple[str, str]:
    """Load Telegram bot token and chat ID.

    Resolution order (first non-empty wins):
      1. Process env vars  — set by the shell or job-agent's own .env
      2. AI Commander userData .env  — ~/Library/Application Support/ai-command-center/.env
         This is the same file telegramApprovalProvider.js reads via main.js loadEnvFile().
      3. AI Commander settings-v3.json telegram section  — legacy fallback

    Returns (token, chat_id) or ("", "") if not configured.
    """
    # 1 + 2. Process env, then the central AI Commander store (CLI → sops → .env),
    # via the shared resolver so there is only one reader of that file — see
    # src/secret_store.py and SECRETS.md.
    try:
        from .secret_store import resolve_secret
        token = resolve_secret("TELEGRAM_BOT_TOKEN") or ""
        chat_id = resolve_secret("TELEGRAM_CHAT_ID") or ""
    except Exception:
        token = chat_id = ""
    if token and chat_id:
        return token, chat_id

    # 3. settings-v3.json legacy fallback (in case UI ever writes there)
    try:
        from .secret_store import _commander_dir
        settings_path = _commander_dir() / "settings-v3.json"
    except Exception:
        settings_path = Path.home() / ".config" / "ai-command-center" / "settings-v3.json"
    if settings_path.exists():
        try:
            tg = json.loads(settings_path.read_text()).get("telegram", {})
            token = token or tg.get("botToken", "")
            chat_id = chat_id or tg.get("chatId", "")
        except Exception:
            pass

    return token, chat_id


def _send_telegram(message: str) -> None:
    """Send a Telegram message. Silently does nothing if not configured."""
    try:
        message = _sanitize_notification_text(message)
        token, chat_id = _load_telegram_config()
        if not token or not chat_id:
            return

        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass

def _desktop_notify(title: str, message: str, subtitle: str = "Job Agent") -> None:
    """Fire a native desktop notification with rate limiting (macOS and Linux)."""
    import sys
    title = _sanitize_notification_text(title)
    message = _sanitize_notification_text(message)
    subtitle = _sanitize_notification_text(subtitle)
    now = time.time()
    cache_key = f"{title}:{message}"
    last_time = _last_notification_times.get(cache_key, 0)
    if now - last_time < 900:
        return
    _last_notification_times[cache_key] = now

    try:
        if sys.platform == "darwin":
            script = (
                f'display notification "{message}" '
                f'with title "{title}" '
                f'subtitle "{subtitle}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        else:
            subprocess.run(
                ["notify-send", f"{subtitle}: {title}", message],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        pass  # Never let notification failures crash the agent


def _add_alert(level: str, title: str, detail: str) -> None:
    title = _sanitize_notification_text(title)
    detail = _sanitize_notification_text(detail)
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
    title = _sanitize_notification_text(title)
    detail = _sanitize_notification_text(detail)
    _add_alert("error", title, detail)

    # Send to Telegram (rate limited by caching key)
    now = time.time()
    cache_key = f"tg:error:{title}:{detail}"
    last_time = _last_notification_times.get(cache_key, 0)
    if now - last_time >= 900:
        _last_notification_times[cache_key] = now
        _send_telegram(f"🚨 [Job Agent ERROR] {title}\nDetail: {detail}")

    _desktop_notify(f"🔴 {title}", detail or title, subtitle="Job Agent ERROR")


def notify_warning(title: str, detail: str = "", *, desktop: bool = True) -> None:
    """Signal something degraded but not fatal — login retry, skipped job."""
    title = _sanitize_notification_text(title)
    detail = _sanitize_notification_text(detail)
    _add_alert("warning", title, detail)

    # Send to Telegram (rate limited by caching key)
    now = time.time()
    cache_key = f"tg:warn:{title}:{detail}"
    last_time = _last_notification_times.get(cache_key, 0)
    if now - last_time >= 900:
        _last_notification_times[cache_key] = now
        _send_telegram(f"⚠️ [Job Agent WARNING] {title}\nDetail: {detail}")

    if desktop:
        _desktop_notify(f"🟡 {title}", detail or title, subtitle="Job Agent WARNING")


def notify_success(title: str, detail: str = "") -> None:
    """Signal a successful application submission."""
    title = _sanitize_notification_text(title)
    detail = _sanitize_notification_text(detail)
    _add_alert("success", title, detail)
    _send_telegram(f"✅ [Job Agent SUCCESS] {title}\nDetail: {detail}")
    _desktop_notify(f"✅ {title}", detail or title, subtitle="Job Agent")


def notify_info(title: str, detail: str = "") -> None:
    """Informational — run started, jobs discovered, etc."""
    title = _sanitize_notification_text(title)
    detail = _sanitize_notification_text(detail)
    _add_alert("info", title, detail)
    # Don't fire a macOS popup or Telegram for info — just write to status file


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
    detail = _sanitize_notification_text(detail)
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
