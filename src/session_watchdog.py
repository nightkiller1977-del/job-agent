"""
Session Watchdog — proactive session health monitoring and automated recovery.

Three layers of protection:
  1. Health check: inspect cookie age before each apply run and classify each
     source as healthy / stale / expired / missing.
  2. Heartbeat: silently visit each source in the background to extend cookie
     lifetime without a full login — run nightly via scheduler.
  3. Deep-link notification: delegates to notifier.py which reads Telegram
     credentials from AI Commander's settings-v3.json automatically.
     (platform-specific: ~/Library/Application Support/ai-command-center on macOS,
      ~/.config/ai-command-center on Linux — resolved via secret_store._commander_dir())
     No duplicate credential configuration needed — configure Telegram once
     in AI Commander and job-agent picks it up automatically.

macOS URL handler (register once):
  bash scripts/install-jobagent-url-handler.sh
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

_log = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSIONS_DIR = Path(__file__).parent.parent / "state" / "sessions"

# How old a session export JSON can be before it's considered stale (warn)
_STALE_HOURS = 20

# How old before it's treated as expired (block)
_EXPIRED_HOURS = 48

# Sources that support background heartbeat visits
_HEARTBEAT_SOURCES = {"linkedin", "indeed", "jobright"}

# Sources that need a visible browser for the user to complete login
_HUMAN_SOURCES = {"linkedin", "usajobs"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SessionHealth:
    source: str
    status: str          # healthy | stale | expired | missing
    age_hours: float
    session_path: Path
    detail: str = ""


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_session_health(sources: list[str] | None = None) -> list[SessionHealth]:
    """Inspect cookie file ages and return health status for each source."""
    all_sources = sources or ["linkedin", "indeed", "jobright", "usajobs"]
    results: list[SessionHealth] = []

    for src in all_sources:
        # Prefer the chromium export (used by background runs)
        paths = [
            SESSIONS_DIR / f"{src}_chromium.json",
            SESSIONS_DIR / f"{src}.json",
        ]
        found: Optional[Path] = None
        for p in paths:
            if p.exists():
                found = p
                break

        if found is None:
            results.append(SessionHealth(
                source=src, status="missing",
                age_hours=float("inf"),
                session_path=SESSIONS_DIR / f"{src}_chromium.json",
                detail="No session file — run prepare-sessions once to seed cookies.",
            ))
            continue

        age_sec = time.time() - found.stat().st_mtime
        age_hours = age_sec / 3600

        # Also peek inside for LinkedIn expiry timestamps if available
        cookie_expiry_hours = _parse_linkedin_expiry(found) if src == "linkedin" else None

        is_expired = (age_hours >= _EXPIRED_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 0)
        is_stale = (age_hours >= _STALE_HOURS) or (cookie_expiry_hours is not None and cookie_expiry_hours <= 4)

        if is_expired:
            status = "expired"
            if cookie_expiry_hours is not None and cookie_expiry_hours <= 0:
                detail = f"LinkedIn cookies expired {abs(cookie_expiry_hours):.1f}h ago."
            else:
                detail = f"Session is {age_hours:.0f}h old — cookies very likely invalid."
        elif is_stale:
            status = "stale"
            if cookie_expiry_hours is not None and cookie_expiry_hours <= 4:
                detail = f"LinkedIn cookies expire in {cookie_expiry_hours:.1f}h — heartbeat recommended."
            else:
                detail = f"Session is {age_hours:.0f}h old — heartbeat recommended."
        else:
            status = "healthy"
            detail = f"Session is {age_hours:.1f}h old — looks good."

        results.append(SessionHealth(
            source=src, status=status,
            age_hours=age_hours,
            session_path=found,
            detail=detail,
        ))

    return results


def _parse_linkedin_expiry(session_path: Path) -> Optional[float]:
    """Extract the earliest LinkedIn cookie expiry from the session JSON.

    Returns hours until expiry (can be negative if already expired),
    or None if parsing fails.
    """
    try:
        data = json.loads(session_path.read_text())
        cookies = data.get("cookies", [])
        li_cookies = [c for c in cookies if "linkedin" in c.get("domain", "")]
        if not li_cookies:
            return None
        now = time.time()
        expiries = [c["expires"] for c in li_cookies if c.get("expires", -1) > 0]
        if not expiries:
            return None
        earliest = min(expiries)
        return (earliest - now) / 3600  # hours until expiry (negative = already expired)
    except Exception:
        return None


def print_health_table(results: list[SessionHealth]) -> None:
    table = Table(title="Session Health", show_lines=True)
    table.add_column("Source", style="bold")
    table.add_column("Status")
    table.add_column("Age")
    table.add_column("Detail")

    STATUS_COLOR = {
        "healthy": "green",
        "stale": "yellow",
        "expired": "red",
        "missing": "red",
    }
    for r in results:
        color = STATUS_COLOR.get(r.status, "white")
        age_str = f"{r.age_hours:.1f}h" if r.age_hours < float("inf") else "—"
        table.add_row(r.source, f"[{color}]{r.status}[/{color}]", age_str, r.detail)

    console.print(table)


# ---------------------------------------------------------------------------
# Heartbeat — silently extend session lifetime
# ---------------------------------------------------------------------------

async def run_heartbeat(sources: list[str] | None = None, config: dict | None = None) -> dict[str, bool]:
    """Visit each source in the background to keep cookies alive.

    Only runs for sources whose session is healthy or stale (not missing/expired).
    Returns {source: refreshed_ok}.
    """
    cfg = config or {}
    targets = sources or list(_HEARTBEAT_SOURCES)
    results: dict[str, bool] = {}

    health = {h.source: h for h in check_session_health(targets)}

    for src in targets:
        h = health.get(src)
        if h and h.status == "missing":
            _log.info("heartbeat.skip source=%s reason=missing", src)
            results[src] = False
            continue
        if h and h.status == "expired":
            _log.info("heartbeat.skip source=%s reason=expired", src)
            results[src] = False
            _send_deep_link_notification(
                src,
                f"{src.capitalize()} session expired ({h.age_hours:.0f}h old). Tap to refresh:",
            )
            continue

        try:
            ok = await _heartbeat_source(src, cfg)
            results[src] = ok
            if ok:
                _log.info("heartbeat.success source=%s", src)
            else:
                _log.warning("heartbeat.failed source=%s", src)
                _send_deep_link_notification(
                    src,
                    f"{src.capitalize()} heartbeat failed. Session may need manual refresh:",
                )
        except Exception as exc:
            _log.error("heartbeat.error source=%s error=%s", src, exc)
            results[src] = False

    return results


async def _heartbeat_source(source: str, config: dict) -> bool:
    """Silently load the source's homepage to refresh cookies."""
    HEARTBEAT_URLS = {
        "linkedin": "https://www.linkedin.com/feed/",
        "indeed":   "https://www.indeed.com/",
        "jobright": "https://jobright.ai/",
    }
    url = HEARTBEAT_URLS.get(source)
    if not url:
        return False

    try:
        from .sources.base import SESSIONS_DIR
        from playwright.async_api import async_playwright

        session_file = SESSIONS_DIR / f"{source}_chromium.json"
        if not session_file.exists():
            return False

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(
                storage_state=str(session_file),
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # Export refreshed cookies back to session file
            state = await ctx.storage_state()
            tmp = session_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(session_file)

            await browser.close()
        return True
    except Exception as exc:
        _log.warning("heartbeat._heartbeat_source source=%s error=%s", source, exc)
        return False


# ---------------------------------------------------------------------------
# Deep-link notification
# ---------------------------------------------------------------------------

def _send_deep_link_notification(source: str, message: str) -> None:
    """Send a Telegram message with a one-tap repair deep-link.

    Delegates entirely to notifier._send_telegram() which reads credentials
    from AI Commander's settings-v3.json — no duplicate config needed.
    Configure Telegram once in AI Commander; job-agent picks it up here.

    Deep-link format: jobagent://prepare-sessions?source=<source>
    Handled by scripts/install-jobagent-url-handler.sh (register once).
    """
    deep_link = f"jobagent://prepare-sessions?source={source}"
    full_msg = f"{message}\n\n{deep_link}"

    try:
        from .notifier import _send_telegram, _macos_notify, _last_notification_times
        import time
        now = time.time()
        cache_key = f"tg:deep_link:{source}"
        last_time = _last_notification_times.get(cache_key, 0)
        # Rate limit identical deep link Telegram alerts to once every 12 hours (43200 seconds)
        if now - last_time < 43200:
            return
        _last_notification_times[cache_key] = now

        _send_telegram(full_msg)
        _macos_notify(f"{source} session needs refresh", message)
    except Exception as exc:
        _log.warning("session_watchdog.notify_failed source=%s error=%s", source, exc)
        console.print(f"[yellow]Session alert ({source}):[/yellow] {message}\n{deep_link}")


# ---------------------------------------------------------------------------
# Preflight gate used by orchestrator.apply_approved
# ---------------------------------------------------------------------------

def preflight_session_check(sources: list[str]) -> dict[str, SessionHealth]:
    """Called before apply_approved — returns health map.

    Expired/missing sources emit deep-link notifications immediately so the
    user can act while other jobs are being processed.
    """
    health_map = {h.source: h for h in check_session_health(sources)}
    for src, h in health_map.items():
        if h.status in {"expired", "missing"}:
            _send_deep_link_notification(
                src,
                f"[Job Agent] {src.capitalize()} session {h.status} — apply will skip {src} jobs. Tap to fix:",
            )
    return health_map
