"""
ReauthManager — recover from expired source sessions automatically.

Automated path (jobright, indeed, linkedin, usajobs):
    Spins up a fresh Playwright context, calls the source's existing
    _auto_login() (for usajobs that includes TOTP / backup-code / emailed-code
    2FA), verifies the result, saves the refreshed session, returns True/False.

Human-assisted fallback (usajobs):
    Only after the automated path has failed. Sends an iMessage to NOTIFY_PHONE
    with the exact terminal command to run, then polls the session file mtime
    until the user completes the interactive login (or the timeout expires).
    Non-interactive runs notify and return immediately — they never block.

    Until ACES-283 usajobs was human-ONLY: every expiry became a notification
    nobody could act on at 23:00 and the source stayed dead for weeks. Automated
    first, human last is the rule now.

On every successful correction:
    1. A regression test is appended to tests/test_reauth_regressions.py
       documenting the exact failure pattern so it can be caught in CI.
    2. A success notification is sent through the shared notifier, which routes to
       Telegram when configured.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
import logging

from datetime import datetime, timezone
from pathlib import Path
from .sources.base import SESSIONS_DIR
from .notifier import notify_error, notify_info, notify_success, notify_warning, record_reauth_event

_log = logging.getLogger(__name__)

# Every source with an _auto_login() gets the automated attempt first.
AUTOMATED_SOURCES = {"jobright", "indeed", "linkedin", "usajobs"}
# Sources that additionally fall back to the human-assisted path when the automated
# attempt fails (2FA method unavailable, CAPTCHA, ...). Mirrored by
# blocker_classifier._HUMAN_FALLBACK_SOURCES — keep both in sync.
HUMAN_SOURCES     = {"usajobs"}

# Circuit breaker for _reauth_automated: below this many consecutive failures we
# use exponential backoff; at/above it we fall back to a long fixed cooldown and
# then allow a half-open retry, rather than blocking the source forever. A manual
# `prepare-sessions` success also clears the streak immediately — see
# orchestrator.prepare_sessions().
CIRCUIT_CAP_FAILURES = 3
CIRCUIT_OPEN_COOLDOWN_SECONDS = 4 * 60 * 60


def _is_interactive() -> bool:
    """True when attached to a terminal, i.e. a human is present to complete an
    interactive re-login. launchd/cron set stdin to /dev/null → not a TTY.
    Factored out so it can be patched in tests."""
    return bool(sys.stdin and sys.stdin.isatty())

# Map source name → scraper class (populated lazily to avoid circular imports)
def _get_source_map():
    from .sources.jobright import JobrightScraper
    from .sources.indeed import IndeedScraper
    from .sources.linkedin import LinkedInScraper
    from .sources.usajobs import USAJobsScraper
    return {
        "jobright": JobrightScraper,
        "indeed":   IndeedScraper,
        "linkedin": LinkedInScraper,
        "usajobs":  USAJobsScraper,
    }


class ReauthManager:
    def __init__(self, config: dict):
        self.config = config
        self.notify_phone = os.environ.get("NOTIFY_PHONE", "")
        self.timeout_discover = int(os.environ.get("REAUTH_TIMEOUT_MINUTES", "30"))
        self.timeout_apply    = int(os.environ.get("REAUTH_TIMEOUT_APPLY_MINUTES", "10"))
        # Where a self-heal appends an auto-generated regression test. OFF by default:
        # production code mutating a tracked test file on every successful reauth
        # grew tests/test_reauth_regressions.py to 35 identical USAJobs tests and
        # leaked runtime artifacts into unrelated PRs (ACES-287). Opt in with
        # JOBAGENT_WRITE_REGRESSION_TESTS=1; tests point this at a tmp file directly.
        self.regression_test_path: Path | None = (
            Path(__file__).parent.parent / "tests" / "test_reauth_regressions.py"
            if os.environ.get("JOBAGENT_WRITE_REGRESSION_TESTS", "").strip().lower() in ("1", "true", "yes", "on")
            else None
        )

    async def handle(self, source: str, detail: str = "", context: str = "discover") -> bool:
        """Attempt session recovery for *source*.

        context="discover" uses the longer poll timeout (REAUTH_TIMEOUT_MINUTES).
        context="apply"    uses the shorter one (REAUTH_TIMEOUT_APPLY_MINUTES) so
                           a blocked source doesn't hold up the remaining apply queue.

        Returns True if the session was refreshed and the caller should retry.
        """
        _log.info("reauth.start source=%s context=%s", source, context)
        has_human_fallback = source in HUMAN_SOURCES
        if source in AUTOMATED_SOURCES:
            # When a human fallback follows, the automated attempt must not fire its
            # own phone escalation — _reauth_human sends the (single) notification.
            kwargs = {"escalate": False} if has_human_fallback else {}
            refreshed = await self._reauth_automated(source, **kwargs)
            if refreshed or not has_human_fallback:
                return refreshed
            _log.info("reauth.automated_failed_falling_back_to_human source=%s", source)
        if has_human_fallback:
            timeout = self.timeout_discover if context == "discover" else self.timeout_apply
            return await self._reauth_human(source, detail, timeout)
        _log.warning("reauth.unknown_source source=%s", source)
        return False

    # ------------------------------------------------------------------
    # Automated path
    # ------------------------------------------------------------------

    async def _reauth_automated(self, source: str, *, escalate: bool = True) -> bool:
        """Stored-credential login for *source*.

        escalate=False suppresses this path's own failure notifications / phone
        deep-link (used when handle() will fall back to _reauth_human, which sends
        the single human-facing notification). Breaker events are recorded either way.
        """
        # Cap retries and implement exponential backoff
        from .notifier import get_status
        status = get_status()
        events = status.get("reauth_events", [])

        consecutive_failures = 0
        last_failure_ts = None
        for event in reversed(events):
            if event.get("source") == source:
                if event.get("outcome") == "failed" and event.get("mode") == "automated":
                    consecutive_failures += 1
                    if not last_failure_ts:
                        try:
                            last_failure_ts = datetime.fromisoformat(event.get("ts").replace("Z", "+00:00"))
                        except Exception:
                            pass
                elif event.get("outcome") == "success":
                    break

        if consecutive_failures > 0 and last_failure_ts:
            if last_failure_ts.tzinfo is not None:
                last_failure_ts = last_failure_ts.replace(tzinfo=None)
            now = datetime.utcnow()
            if consecutive_failures >= CIRCUIT_CAP_FAILURES:
                backoff_seconds = CIRCUIT_OPEN_COOLDOWN_SECONDS
            else:
                backoff_seconds = (2 ** consecutive_failures) * 120
            time_since_failure = (now - last_failure_ts).total_seconds()
            if time_since_failure < backoff_seconds:
                remaining = int(backoff_seconds - time_since_failure)
                _log.info("reauth.backoff source=%s consecutive_failures=%d remaining=%ds", source, consecutive_failures, remaining)
                return False
            if consecutive_failures >= CIRCUIT_CAP_FAILURES:
                _log.info("reauth.half_open_retry source=%s consecutive_failures=%d", source, consecutive_failures)

        source_map = _get_source_map()
        scraper_cls = source_map.get(source)
        if not scraper_cls:
            notify_error(f"{source} reauth failed", "No automated reauth path for this source")
            return False

        from .secret_store import resolve_secret
        email    = resolve_secret(f"{source.upper()}_EMAIL") or ""
        password = resolve_secret(f"{source.upper()}_PASSWORD") or ""
        if not email or not password:
            record_reauth_event(source, "automated", "failed", "missing credentials")
            _log.warning("reauth.failed source=%s mode=automated reason=missing_credentials", source)
            if escalate:
                notify_error(
                    f"{source} reauth failed",
                    f"Add {source.upper()}_EMAIL and {source.upper()}_PASSWORD to .env",
                )
            return False

        notify_info(f"{source} reauth", "Attempting automated session refresh…")
        scraper = scraper_cls(self.config)
        page = None
        try:
            page = await scraper._start_browser()
            success = await scraper._auto_login(page, email, password)
            if success:
                # Post-login verification to eliminate false-positives
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    await asyncio.sleep(2)
                    cur_url = page.url
                except Exception as verify_exc:
                    record_reauth_event(source, "automated", "failed", f"Failed post-login url verification: {verify_exc}")
                    _log.error("reauth.verify_error source=%s error=%s", source, verify_exc)
                    return False

                if not _is_logged_in_url(source, cur_url):
                    record_reauth_event(source, "automated", "failed", f"auto_login returned True but URL is {cur_url}")
                    _log.warning("reauth.false_positive source=%s url=%s", source, cur_url)
                    if escalate:
                        notify_warning(
                            f"{source} automated reauth false-positive",
                            f"Login returned True but was redirected to login page: {cur_url}",
                        )
                    return False
                # Second, stronger check where the scraper offers one: a DOM probe for
                # logged-in state. The URL heuristic alone passes a redirect to a home
                # page that never shows a login wall (Jobright), and a false "success"
                # both exports a stale session and resets the circuit breaker (ACES-72).
                if not await _dom_says_logged_in(scraper, page):
                    record_reauth_event(
                        source, "automated", "failed",
                        f"auto_login returned True but the page shows no logged-in state ({cur_url})",
                    )
                    _log.warning("reauth.false_positive_dom source=%s url=%s", source, cur_url)
                    if escalate:
                        notify_warning(
                            f"{source} automated reauth false-positive",
                            f"Login returned True but the page shows no logged-in state: {cur_url}",
                        )
                    return False
                await scraper._export_session_json()
                record_reauth_event(source, "automated", "success")
                _log.info("reauth.success source=%s mode=automated", source)
                notify_info(f"{source} reauth", "Session refreshed automatically — retrying")
                # After a successful Jobright reauth, re-enable Orion resume tailoring
                # so the next job in the queue gets a fresh Orion attempt rather than
                # being skipped for the remainder of the session.
                if source == "jobright":
                    try:
                        from .sources.jobright import JobrightScraper
                        JobrightScraper.reset_orion_availability()
                        _log.info("reauth.orion_reset source=jobright")
                    except Exception as _reset_exc:
                        _log.warning("reauth.orion_reset_failed: %s", _reset_exc)
                self._write_regression_test(source, "automated", "_auto_login returned True after session expiry")
                self._notify_correction(source, "automated", "_auto_login returned True after session expiry")
                return True
            else:
                record_reauth_event(source, "automated", "failed", "_auto_login returned False")
                _log.warning("reauth.failed source=%s mode=automated reason=login_returned_false", source)
                if escalate:
                    notify_warning(
                        f"{source} automated reauth failed",
                        "Login returned False — may need human assist or CAPTCHA",
                    )
                    # Escalate: send a one-tap deep-link so user can fix from phone
                    try:
                        from .session_watchdog import _send_deep_link_notification
                        _send_deep_link_notification(
                            source,
                            f"[Job Agent] {source.capitalize()} automated login failed (CAPTCHA/2FA). Tap to open Terminal and fix:",
                        )
                    except Exception:
                        pass
                return False
        except Exception as exc:
            record_reauth_event(source, "automated", "failed", str(exc)[:300])
            _log.error("reauth.error source=%s mode=automated error=%s", source, exc)
            if escalate:
                notify_error(f"{source} automated reauth error", str(exc)[:200])
                try:
                    from .session_watchdog import _send_deep_link_notification
                    _send_deep_link_notification(
                        source,
                        f"[Job Agent] {source.capitalize()} reauth error: {str(exc)[:120]}. Tap to fix:",
                    )
                except Exception:
                    pass
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

        interactive = _is_interactive()
        retry_line = (
            f"Agent will auto-retry within {timeout_minutes} min."
            if interactive
            else "Agent is running in the background — it won't wait now. Refresh the "
                 "session when you can and it'll be used on the next run."
        )
        msg = (
            f"Job agent: {source.upper()} session expired.\n"
            f"{detail}\n\n"
            f"Tap the link below to open Terminal automatically:\n"
            f"jobagent://prepare-sessions?source={source}\n\n"
            f"Or run manually:\n"
            f"  cd ~/Dev/Projects/job-agent\n"
            f"  python src/main.py prepare-sessions --source {source}\n\n"
            f"Log in / complete 2FA in the browser, then close it.\n"
            f"{retry_line}"
        )
        _send_imessage(self.notify_phone, msg)
        # Also send via Telegram for the clickable deep-link
        try:
            from .session_watchdog import _send_deep_link_notification
            _send_deep_link_notification(
                source,
                f"[Job Agent] {source.capitalize()} session expired. Tap to open Terminal and refresh:",
            )
        except Exception:
            pass
        _log.info(
            "reauth.human_notified source=%s timeout_min=%d phone_set=%s interactive=%s",
            source, timeout_minutes, bool(self.notify_phone), interactive,
        )
        record_reauth_event(source, "human_notified", "waiting", detail)

        # Non-interactive (launchd/cron/background): we cannot block for a human to
        # complete an interactive login. Blocking would freeze the run up to
        # `timeout_minutes` PER blocked source — several sources could stall the run
        # for close to an hour until it's killed, applying to nothing. So notify and
        # skip immediately; the orchestrator moves on to other sources, and the
        # refreshed session is picked up on the next scheduled run.
        if not interactive:
            record_reauth_event(
                source, "human", "skipped_noninteractive",
                "Notified user; not waiting because there is no TTY (background run)",
            )
            _log.warning(
                "reauth.skipped_noninteractive source=%s — notified, not blocking", source,
            )
            notify_warning(
                f"{source} session expired — action needed",
                "Auth refresh instructions were sent. Skipping this source in the background "
                "run; it will retry once you refresh the session.",
                desktop=False,
            )
            return False

        notify_warning(
            f"{source} session expired — action needed",
            f"Auth refresh instructions were sent. Waiting up to {timeout_minutes} min for refresh.",
            desktop=False,
        )

        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(30)
            if session_file.exists() and session_file.stat().st_mtime > baseline_mtime:
                record_reauth_event(source, "human", "session_refreshed")
                _log.info("reauth.success source=%s mode=human", source)
                notify_info(f"{source} session refreshed", "Session file updated — retrying source")
                self._write_regression_test(source, "human", detail)
                self._notify_correction(source, "human", detail)
                return True

        record_reauth_event(source, "human", "timeout", f"No refresh after {timeout_minutes} min")
        _log.warning("reauth.timeout source=%s mode=human timeout_min=%d", source, timeout_minutes)
        notify_error(
            f"{source} reauth timed out",
            f"Session was not refreshed within {timeout_minutes} minutes. Skipping source this run.",
        )
        return False

    # ------------------------------------------------------------------
    # Intelligent self-healing: regression test + iMessage notification
    # ------------------------------------------------------------------

    def _write_regression_test(self, source: str, mode: str, detail: str) -> None:
        """Append an auto-generated regression test to self.regression_test_path
        (no-op when that is None — the default; see __init__)."""
        if not self.regression_test_path:
            _log.debug("ReauthManager: regression-test writing disabled; skipping for %s", source)
            return
        ts = datetime.now(timezone.utc)
        ts_slug = ts.strftime("%Y%m%d_%H%M%S")
        ts_human = ts.strftime("%Y-%m-%d %H:%M:%S UTC")

        strategy = "_reauth_automated" if mode == "automated" else "_reauth_human"
        other    = "_reauth_human" if mode == "automated" else "_reauth_automated"
        set_name = "AUTOMATED_SOURCES" if mode == "automated" else "HUMAN_SOURCES"

        safe_detail = re.sub(r"[^\w\s.,:;'()/-]", "", detail)[:120]

        # Both strategies are patched: a human-fallback source runs the automated path
        # first, and an unpatched strategy would launch a real browser from a unit test.
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

    # Verify ReauthManager reaches the strategy that healed this (the other one is
    # patched to fail so nothing real runs and the fallback order is exercised)
    mgr = ReauthManager(config={{}})
    with patch.object(mgr, "{strategy}", new_callable=AsyncMock, return_value=True) as mock, \\
         patch.object(mgr, "{other}", new_callable=AsyncMock, return_value=False):
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
        """Notify through the shared notifier after a successful self-heal."""
        ts_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        notify_success(
            f"Job Agent self-healed: {source.upper()}",
            (
            f"What failed: {detail}\n"
            f"How fixed: {mode} reauth\n"
            f"When: {ts_human}\n"
            f"Status: Session refreshed — source will be retried"
            ),
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _send_imessage(phone: str, message: str) -> None:
    if not phone:
        notify_warning(
            "iMessage not configured",
            "Set NOTIFY_PHONE in .env to receive auth alerts via iMessage",
            desktop=False,
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


async def _dom_says_logged_in(scraper, page) -> bool:
    """Post-login DOM probe via the scraper's own ``_is_logged_in(page)`` when it has
    one (USAJobs does). Scrapers without a probe — or a probe that raises — pass, so
    this only ever *adds* a rejection on top of the URL heuristic; it never blesses a
    session the URL check already refused."""
    probe = getattr(scraper, "_is_logged_in", None)
    # asyncio's check (not inspect's) so both real `async def` methods and AsyncMock
    # test doubles count as probes; a non-awaitable attribute is not a probe at all.
    if probe is None or not asyncio.iscoroutinefunction(probe):
        return True
    try:
        return bool(await probe(page))
    except Exception as exc:  # noqa: BLE001 — a broken probe must not fail a good login
        _log.warning("reauth.dom_probe_error error=%s", exc)
        return True


def _is_logged_in_url(source: str, url: str) -> bool:
    """True if the URL doesn't look like a login/signin/challenge redirect."""
    if not isinstance(url, str):
        return True
    url_lower = url.lower()
    bad_patterns = ["/login", "/signin", "/signup", "challenge", "checkpoint", "accounts.google"]
    return not any(p in url_lower for p in bad_patterns)
