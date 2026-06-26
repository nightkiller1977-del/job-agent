"""
ReauthManager — recover from expired source sessions automatically.

Automated path (jobright, indeed, linkedin):
    Spins up a fresh Playwright context, calls the source's existing
    _auto_login(), saves the refreshed session, returns True/False.

Human-assisted path (usajobs):
    Sends an iMessage to NOTIFY_PHONE with the exact terminal command
    to run, then polls the session file mtime until the user completes
    the interactive login (or the timeout expires).

On every successful correction:
    1. A regression test is appended to tests/test_reauth_regressions.py
       documenting the exact failure pattern so it can be caught in CI.
    2. An iMessage is sent to NOTIFY_PHONE summarising what was fixed.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
import logging

from datetime import datetime, timezone
from pathlib import Path
from .sources.base import SESSIONS_DIR
from .notifier import notify_error, notify_info, notify_warning, record_reauth_event, _macos_notify

_log = logging.getLogger(__name__)

AUTOMATED_SOURCES = {"jobright", "indeed", "linkedin"}
HUMAN_SOURCES     = {"usajobs"}

# Map source name → scraper class (populated lazily to avoid circular imports)
def _get_source_map():
    from .sources.jobright import JobrightScraper
    from .sources.indeed import IndeedScraper
    from .sources.linkedin import LinkedInScraper
    return {
        "jobright": JobrightScraper,
        "indeed":   IndeedScraper,
        "linkedin": LinkedInScraper,
    }


class ReauthManager:
    def __init__(self, config: dict):
        self.config = config
        self.notify_phone = os.environ.get("NOTIFY_PHONE", "")
        self.timeout_discover = int(os.environ.get("REAUTH_TIMEOUT_MINUTES", "30"))
        self.timeout_apply    = int(os.environ.get("REAUTH_TIMEOUT_APPLY_MINUTES", "10"))
        self.regression_test_path: Path = (
            Path(__file__).parent.parent / "tests" / "test_reauth_regressions.py"
        )

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
                self._write_regression_test(source, "automated", "_auto_login returned True after session expiry")
                self._notify_correction(source, "automated", "_auto_login returned True after session expiry")
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
                self._write_regression_test(source, "human", detail)
                self._notify_correction(source, "human", detail)
                return True

        record_reauth_event(source, "human", "timeout", f"No refresh after {timeout_minutes} min")
        notify_error(
            f"{source} reauth timed out",
            f"Session was not refreshed within {timeout_minutes} minutes. Skipping source this run.",
        )
        return False

    # ------------------------------------------------------------------
    # Intelligent self-healing: regression test + iMessage notification
    # ------------------------------------------------------------------

    def _write_regression_test(self, source: str, mode: str, detail: str) -> None:
        """Append an auto-generated regression test to tests/test_reauth_regressions.py."""
        ts = datetime.now(timezone.utc)
        ts_slug = ts.strftime("%Y%m%d_%H%M%S")
        ts_human = ts.strftime("%Y-%m-%d %H:%M:%S UTC")

        strategy = "_reauth_automated" if mode == "automated" else "_reauth_human"
        set_name = "AUTOMATED_SOURCES" if mode == "automated" else "HUMAN_SOURCES"

        safe_detail = re.sub(r"[^\w\s.,:;'()/-]", "", detail)[:120]

        test_body = f'''
@pytest.mark.asyncio
async def test_regression_{source}_{ts_slug}():
    """Auto-generated regression: {source} — {safe_detail} — corrected {ts_human}"""
    from src.sources.base import AuthFailedError
    from src.reauth import ReauthManager, AUTOMATED_SOURCES, HUMAN_SOURCES
    from unittest.mock import patch, AsyncMock

    # Verify source routing hasn't regressed
    assert "{source}" in {set_name}

    # Verify AuthFailedError carries correct attributes for this scenario
    exc = AuthFailedError("{source}", "{safe_detail}")
    assert exc.source == "{source}"
    assert "{safe_detail}" in str(exc)

    # Verify ReauthManager routes to the correct strategy
    mgr = ReauthManager(config={{}})
    with patch.object(mgr, "{strategy}", new_callable=AsyncMock, return_value=True) as mock:
        result = await mgr.handle("{source}", "{safe_detail}")
        mock.assert_called_once()
    assert result is True
'''

        regression_path = self.regression_test_path

        if not regression_path.exists():
            header = (
                '"""Auto-generated regression tests — written by ReauthManager on each successful self-heal."""\n'
                "import pytest\n"
            )
            regression_path.write_text(header)

        with regression_path.open("a") as fh:
            fh.write(test_body)

        _log.info("ReauthManager: regression test written for %s (%s)", source, ts_slug)

    def _notify_correction(self, source: str, mode: str, detail: str) -> None:
        """Send an iMessage summarising the successful self-heal."""
        ts_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            f"✅ Job Agent self-healed: {source.upper()}\n"
            f"What failed: {detail}\n"
            f"How fixed: {mode} reauth\n"
            f"When: {ts_human}\n"
            f"Status: Session refreshed — source will be retried"
        )
        _send_imessage(self.notify_phone, msg)


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
