"""
ReauthManager — recover from expired source sessions automatically.

Automated path (jobright, indeed):
    Spins up a fresh Playwright context, calls the source's existing
    _auto_login(), saves the refreshed session, returns True/False.

Human-assisted path (linkedin, usajobs):
    Sends an iMessage to NOTIFY_PHONE with the exact terminal command
    to run, then polls the session file mtime until the user completes
    the interactive login (or the timeout expires).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import logging

from pathlib import Path
from .sources.base import SESSIONS_DIR
from .notifier import notify_error, notify_info, notify_warning, record_reauth_event, _macos_notify

_log = logging.getLogger(__name__)

AUTOMATED_SOURCES = {"jobright", "indeed"}
HUMAN_SOURCES     = {"linkedin", "usajobs"}

# Map source name → scraper class (populated lazily to avoid circular imports)
def _get_source_map():
    from .sources.jobright import JobrightScraper
    from .sources.indeed import IndeedScraper
    return {
        "jobright": JobrightScraper,
        "indeed":   IndeedScraper,
    }


class ReauthManager:
    def __init__(self, config: dict):
        self.config = config
        self.notify_phone = os.environ.get("NOTIFY_PHONE", "")
        self.timeout_discover = int(os.environ.get("REAUTH_TIMEOUT_MINUTES", "30"))
        self.timeout_apply    = int(os.environ.get("REAUTH_TIMEOUT_APPLY_MINUTES", "10"))

    async def handle(self, source: str, detail: str = "", context: str = "discover") -> bool:
        """Attempt session recovery for *source*.

        context="discover" uses the longer poll timeout (REAUTH_TIMEOUT_MINUTES).
        context="apply"    uses the shorter one (REAUTH_TIMEOUT_APPLY_MINUTES) so
                           a blocked source doesn't hold up the remaining apply queue.

        Returns True if the session was refreshed and the caller should retry.
        """
        if source in AUTOMATED_SOURCES:
            return await self._reauth_automated(source)
        elif source in HUMAN_SOURCES:
            timeout = self.timeout_discover if context == "discover" else self.timeout_apply
            return await self._reauth_human(source, detail, timeout)
        _log.warning("ReauthManager: unknown source '%s' — cannot reauth", source)
        return False

    # ------------------------------------------------------------------
    # Automated path
    # ------------------------------------------------------------------

    async def _reauth_automated(self, source: str) -> bool:
        source_map = _get_source_map()
        scraper_cls = source_map.get(source)
        if not scraper_cls:
            notify_error(f"{source} reauth failed", "No automated reauth path for this source")
            return False

        email    = os.environ.get(f"{source.upper()}_EMAIL", "")
        password = os.environ.get(f"{source.upper()}_PASSWORD", "")
        if not email or not password:
            notify_error(
                f"{source} reauth failed",
                f"Add {source.upper()}_EMAIL and {source.upper()}_PASSWORD to .env",
            )
            record_reauth_event(source, "automated", "failed", "missing credentials")
            return False

        notify_info(f"{source} reauth", "Attempting automated session refresh…")
        scraper = scraper_cls(self.config)
        page = None
        try:
            page = await scraper._start_browser()
            success = await scraper._auto_login(page, email, password)
            if success:
                await scraper._export_session_json()
                record_reauth_event(source, "automated", "success")
                notify_info(f"{source} reauth", "Session refreshed automatically — retrying")
                return True
            else:
                record_reauth_event(source, "automated", "failed", "_auto_login returned False")
                notify_warning(
                    f"{source} automated reauth failed",
                    "Login returned False — may need human assist or CAPTCHA",
                )
                return False
        except Exception as exc:
            record_reauth_event(source, "automated", "failed", str(exc)[:300])
            notify_error(f"{source} automated reauth error", str(exc)[:200])
            return False
        finally:
            try:
                await scraper._close_browser(save_session=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Human-assisted path
    # ------------------------------------------------------------------

    async def _reauth_human(self, source: str, detail: str, timeout_minutes: int) -> bool:
        session_file = SESSIONS_DIR / f"{source}_chromium.json"
        baseline_mtime = session_file.stat().st_mtime if session_file.exists() else 0

        msg = (
            f"Job agent: {source.upper()} session expired.\n"
            f"{detail}\n\n"
            f"To fix — open Terminal and run:\n"
            f"  cd ~/Dev/Projects/job-agent\n"
            f"  python src/main.py prepare-sessions --source {source}\n\n"
            f"Log in / complete 2FA in the browser, then close it.\n"
            f"Agent will auto-retry within {timeout_minutes} min."
        )
        _send_imessage(self.notify_phone, msg)
        record_reauth_event(source, "human_notified", "waiting", detail)
        notify_warning(
            f"{source} session expired — action needed",
            f"iMessage sent to {self.notify_phone or 'N/A'}. Waiting up to {timeout_minutes} min for refresh.",
        )

        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(30)
            if session_file.exists() and session_file.stat().st_mtime > baseline_mtime:
                record_reauth_event(source, "human", "session_refreshed")
                notify_info(f"{source} session refreshed", "Session file updated — retrying source")
                return True

        record_reauth_event(source, "human", "timeout", f"No refresh after {timeout_minutes} min")
        notify_error(
            f"{source} reauth timed out",
            f"Session was not refreshed within {timeout_minutes} minutes. Skipping source this run.",
        )
        return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _send_imessage(phone: str, message: str) -> None:
    # Always fire a macOS notification as a fallback regardless of iMessage success
    _macos_notify("Job Agent — Auth Required", message[:200], subtitle="Action Needed")

    if not phone:
        notify_warning(
            "iMessage not configured",
            "Set NOTIFY_PHONE in .env to receive auth alerts via iMessage",
        )
        return

    # Escape for AppleScript string literal
    safe_msg = message.replace('"', '\\"').replace("\n", "\\n")
    script = (
        f'tell application "Messages"\n'
        f'    set t to first service whose service type = iMessage\n'
        f'    set b to buddy "{phone}" of t\n'
        f'    send "{safe_msg}" to b\n'
        f'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as exc:
        _log.warning("iMessage send failed: %s", exc)
