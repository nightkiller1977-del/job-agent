"""
Unit tests for self-healing auth components.

No browser, no network — all Playwright interactions are mocked.

Coverage:
  - AuthFailedError construction and attributes
  - BaseScraper._safe_evaluate: returns default on non-fatal errors,
    re-raises on browser-death signals
  - BaseScraper._safe_goto: returns False on failure, re-raises on crash
  - notifier.record_reauth_event: appends correctly to agent_status.json
  - ReauthManager.handle: routes source names to correct strategy
  - ReauthManager._reauth_automated: success/failure/missing-credentials paths
  - ReauthManager._reauth_human: session-file-updated path, timeout path
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.sources.base import AuthFailedError, BaseScraper
from src.notifier import record_reauth_event


# ── AuthFailedError ───────────────────────────────────────────────────────────

class TestAuthFailedError:
    def test_message_with_detail(self):
        exc = AuthFailedError("linkedin", "Login timed out")
        assert "linkedin" in str(exc)
        assert "Login timed out" in str(exc)

    def test_message_without_detail(self):
        exc = AuthFailedError("jobright")
        assert "jobright" in str(exc)

    def test_source_attribute(self):
        exc = AuthFailedError("indeed", "session expired")
        assert exc.source == "indeed"

    def test_detail_attribute(self):
        exc = AuthFailedError("usajobs", "2FA required")
        assert exc.detail == "2FA required"

    def test_detail_defaults_empty(self):
        exc = AuthFailedError("linkedin")
        assert exc.detail == ""

    def test_is_exception(self):
        exc = AuthFailedError("linkedin")
        assert isinstance(exc, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(AuthFailedError) as exc_info:
            raise AuthFailedError("jobright", "redirect to /login")
        assert exc_info.value.source == "jobright"


# ── _safe_evaluate ─────────────────────────────────────────────────────────────

class _ConcreteScraper(BaseScraper):
    name = "test"
    async def scrape(self): return []
    async def apply(self, job, auto_submit=False): return False


class TestSafeEvaluate:
    def _scraper(self):
        return _ConcreteScraper(config={})

    @pytest.mark.asyncio
    async def test_returns_value_on_success(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value=42)
        result = await scraper._safe_evaluate(page, "1+1", default=0)
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_default_on_non_fatal_error(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("some selector error"))
        result = await scraper._safe_evaluate(page, "bad_script()", default="fallback")
        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_reraises_on_page_closed(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )
        with pytest.raises(Exception, match="closed"):
            await scraper._safe_evaluate(page, "anything", default=None)

    @pytest.mark.asyncio
    async def test_reraises_on_context_detached(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("Execution context was detached"))
        with pytest.raises(Exception, match="detached"):
            await scraper._safe_evaluate(page, "anything", default=None)

    @pytest.mark.asyncio
    async def test_reraises_on_browser_crashed(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("Target page crashed"))
        with pytest.raises(Exception, match="crashed"):
            await scraper._safe_evaluate(page, "anything", default=None)

    @pytest.mark.asyncio
    async def test_default_none_when_not_specified(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("non-fatal"))
        result = await scraper._safe_evaluate(page, "script")
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_args_to_evaluate(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="ok")
        await scraper._safe_evaluate(page, "script", "arg1", default=None)
        page.evaluate.assert_called_once_with("script", "arg1")

    @pytest.mark.asyncio
    async def test_no_args_calls_evaluate_without_args(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value="ok")
        await scraper._safe_evaluate(page, "script")
        page.evaluate.assert_called_once_with("script")


# ── _safe_goto ────────────────────────────────────────────────────────────────

class TestSafeGoto:
    def _scraper(self):
        return _ConcreteScraper(config={})

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.goto = AsyncMock(return_value=None)
        result = await scraper._safe_goto(page, "https://example.com")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_navigation_error(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("net::ERR_ABORTED"))
        result = await scraper._safe_goto(page, "https://example.com")
        assert result is False

    @pytest.mark.asyncio
    async def test_reraises_on_browser_closed(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=Exception("Target page, context or browser has been closed"))
        with pytest.raises(Exception, match="closed"):
            await scraper._safe_goto(page, "https://example.com")

    @pytest.mark.asyncio
    async def test_passes_timeout_and_wait_until(self):
        scraper = self._scraper()
        page = AsyncMock()
        page.goto = AsyncMock(return_value=None)
        await scraper._safe_goto(page, "https://x.com", timeout=15000, wait_until="networkidle")
        page.goto.assert_called_once_with(
            "https://x.com", wait_until="networkidle", timeout=15000
        )


# ── record_reauth_event ────────────────────────────────────────────────────────

class TestRecordReauthEvent:
    def test_appends_event_to_status_file(self, tmp_path, monkeypatch):
        status_file = tmp_path / "agent_status.json"
        monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)

        record_reauth_event("linkedin", "automated", "success", "session refreshed")

        data = json.loads(status_file.read_text())
        assert "reauth_events" in data
        assert len(data["reauth_events"]) == 1
        ev = data["reauth_events"][0]
        assert ev["source"] == "linkedin"
        assert ev["mode"] == "automated"
        assert ev["outcome"] == "success"
        assert ev["detail"] == "session refreshed"
        assert "ts" in ev

    def test_appends_multiple_events(self, tmp_path, monkeypatch):
        status_file = tmp_path / "agent_status.json"
        monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)

        record_reauth_event("jobright", "automated", "success")
        record_reauth_event("linkedin", "human_notified", "waiting")
        record_reauth_event("usajobs", "human", "timeout")

        data = json.loads(status_file.read_text())
        assert len(data["reauth_events"]) == 3
        assert data["reauth_events"][0]["source"] == "jobright"
        assert data["reauth_events"][2]["source"] == "usajobs"

    def test_caps_at_100_events(self, tmp_path, monkeypatch):
        status_file = tmp_path / "agent_status.json"
        monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)

        for i in range(105):
            record_reauth_event("jobright", "automated", "success", str(i))

        data = json.loads(status_file.read_text())
        assert len(data["reauth_events"]) == 100

    def test_empty_detail_defaults_to_empty_string(self, tmp_path, monkeypatch):
        status_file = tmp_path / "agent_status.json"
        monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)

        record_reauth_event("indeed", "automated", "failed")

        data = json.loads(status_file.read_text())
        assert data["reauth_events"][0]["detail"] == ""


# ── ReauthManager routing ──────────────────────────────────────────────────────

class TestReauthManagerRouting:
    @pytest.mark.asyncio
    async def test_automated_sources_route_to_automated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        for source in ("jobright", "indeed", "linkedin"):
            with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=True) as mock_auto, \
                 patch.object(mgr, "_reauth_human", new_callable=AsyncMock, return_value=True) as mock_human:
                result = await mgr.handle(source, "test")
                mock_auto.assert_called_once_with(source)
                mock_human.assert_not_called()
                assert result is True

    @pytest.mark.asyncio
    async def test_usajobs_routes_to_human(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock) as mock_auto, \
             patch.object(mgr, "_reauth_human", new_callable=AsyncMock, return_value=False) as mock_human:
            result = await mgr.handle("usajobs", "2FA required")
            mock_human.assert_called_once()
            mock_auto.assert_not_called()
            assert result is False

    @pytest.mark.asyncio
    async def test_unknown_source_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        result = await mgr.handle("unknown_source", "detail")
        assert result is False

    @pytest.mark.asyncio
    async def test_context_discover_uses_long_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        os.environ["REAUTH_TIMEOUT_MINUTES"] = "30"
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        captured = {}
        async def fake_human(source, detail, timeout_minutes):
            captured["timeout"] = timeout_minutes
            return False

        with patch.object(mgr, "_reauth_human", side_effect=fake_human):
            await mgr.handle("usajobs", "detail", context="discover")
        assert captured["timeout"] == 30

    @pytest.mark.asyncio
    async def test_context_apply_uses_short_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        os.environ["REAUTH_TIMEOUT_APPLY_MINUTES"] = "10"
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        captured = {}
        async def fake_human(source, detail, timeout_minutes):
            captured["timeout"] = timeout_minutes
            return False

        with patch.object(mgr, "_reauth_human", side_effect=fake_human):
            await mgr.handle("usajobs", "detail", context="apply")
        assert captured["timeout"] == 10


# ── ReauthManager._reauth_automated ───────────────────────────────────────────

class TestReauthAutomated:
    @pytest.mark.asyncio
    async def test_success_returns_true_and_saves_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@test.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "password")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        mock_scraper = AsyncMock()
        mock_scraper._auto_login = AsyncMock(return_value=True)
        mock_scraper._export_session_json = AsyncMock()
        mock_scraper._close_browser = AsyncMock()

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock(return_value=mock_scraper)}), \
             patch.object(mock_scraper, "_start_browser", new_callable=AsyncMock, return_value=AsyncMock()):
            result = await mgr._reauth_automated("jobright")

        assert result is True
        mock_scraper._export_session_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_login_failure_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@test.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "password")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        mock_scraper = AsyncMock()
        mock_scraper._auto_login = AsyncMock(return_value=False)
        mock_scraper._export_session_json = AsyncMock()
        mock_scraper._close_browser = AsyncMock()

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock(return_value=mock_scraper)}), \
             patch.object(mock_scraper, "_start_browser", new_callable=AsyncMock, return_value=AsyncMock()):
            result = await mgr._reauth_automated("jobright")

        assert result is False
        mock_scraper._export_session_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_credentials_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.delenv("JOBRIGHT_EMAIL", raising=False)
        monkeypatch.delenv("JOBRIGHT_PASSWORD", raising=False)
        # Deterministic regardless of what the host's central secret store has
        # (env is still checked first inside resolve_secret; see the
        # resolve_secret test below for the case where the store fills it in).
        monkeypatch.setattr("src.secret_store.resolve_secret", lambda name: None)

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock()}):
            result = await mgr._reauth_automated("jobright")

        assert result is False

    @pytest.mark.asyncio
    async def test_automated_reauth_falls_back_to_central_secret_store(self, tmp_path, monkeypatch):
        """Credentials must resolve through secret_store.resolve_secret (env →
        aicc-secrets CLI → sops-decrypted or plaintext central store), not bare
        os.environ.get — otherwise a scheduled run whose shell env doesn't carry
        JOBRIGHT_EMAIL/PASSWORD reports 'missing credentials' even when the
        central store has them (ACES: PR review — reauth previously bypassed
        the central store entirely)."""
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.delenv("JOBRIGHT_EMAIL", raising=False)
        monkeypatch.delenv("JOBRIGHT_PASSWORD", raising=False)

        resolved = {"JOBRIGHT_EMAIL": "store@example.com", "JOBRIGHT_PASSWORD": "store-secret"}
        monkeypatch.setattr("src.secret_store.resolve_secret", lambda name: resolved.get(name))

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        mock_scraper = AsyncMock()
        mock_scraper._auto_login = AsyncMock(return_value=True)
        mock_scraper._export_session_json = AsyncMock()
        mock_scraper._close_browser = AsyncMock()
        mock_page = AsyncMock()

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock(return_value=mock_scraper)}), \
             patch.object(mock_scraper, "_start_browser", new_callable=AsyncMock, return_value=mock_page):
            result = await mgr._reauth_automated("jobright")

        assert result is True
        mock_scraper._auto_login.assert_awaited_once_with(mock_page, "store@example.com", "store-secret")

    @pytest.mark.asyncio
    async def test_browser_always_closed_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@test.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "password")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        mock_scraper = AsyncMock()
        mock_scraper._auto_login = AsyncMock(side_effect=Exception("playwright crash"))
        mock_scraper._close_browser = AsyncMock()

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock(return_value=mock_scraper)}), \
             patch.object(mock_scraper, "_start_browser", new_callable=AsyncMock, return_value=AsyncMock()):
            result = await mgr._reauth_automated("jobright")

        assert result is False
        mock_scraper._close_browser.assert_called_once_with(save_session=False)

    @pytest.mark.asyncio
    async def test_records_success_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setenv("LINKEDIN_EMAIL", "test@test.com")
        monkeypatch.setenv("LINKEDIN_PASSWORD", "password")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        mock_scraper = AsyncMock()
        mock_scraper._auto_login = AsyncMock(return_value=True)
        mock_scraper._export_session_json = AsyncMock()
        mock_scraper._close_browser = AsyncMock()

        with patch("src.reauth._get_source_map", return_value={"linkedin": MagicMock(return_value=mock_scraper)}), \
             patch.object(mock_scraper, "_start_browser", new_callable=AsyncMock, return_value=AsyncMock()):
            await mgr._reauth_automated("linkedin")

        data = json.loads((tmp_path / "status.json").read_text())
        events = data.get("reauth_events", [])
        assert any(e["source"] == "linkedin" and e["outcome"] == "success" for e in events)


# ── ReauthManager._reauth_human ────────────────────────────────────────────────

class TestReauthHuman:
    @pytest.mark.asyncio
    async def test_returns_true_when_session_file_updated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)
        monkeypatch.setattr("src.reauth._send_imessage", MagicMock())

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        mgr.notify_phone = "+13055551234"

        session_file = tmp_path / "usajobs_chromium.json"
        # Write with an old mtime so _reauth_human captures it as baseline
        session_file.write_text("{}")
        import os
        old_time = 1000.0
        os.utime(session_file, (old_time, old_time))

        # On the first asyncio.sleep, bump the file's mtime to simulate user login
        async def sleep_and_update(_):
            os.utime(session_file, (old_time + 100, old_time + 100))

        with patch("asyncio.sleep", side_effect=sleep_and_update), \
             patch("time.monotonic", side_effect=[0, 30]), \
             patch("src.reauth._is_interactive", return_value=True):
            result = await mgr._reauth_human("usajobs", "2FA required", timeout_minutes=1)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)
        monkeypatch.setattr("src.reauth._send_imessage", MagicMock())

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        # File exists but is never updated
        session_file = tmp_path / "usajobs_chromium.json"
        session_file.write_text("{}")

        poll_count = 0
        original_sleep = asyncio.sleep

        async def fast_sleep(_):
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                # Exhaust the deadline by monkeypatching time.monotonic
                pass

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.monotonic", side_effect=[0, 0, 999999]), \
             patch("src.reauth._is_interactive", return_value=True):
            result = await mgr._reauth_human("usajobs", "2FA required", timeout_minutes=1)

        assert result is False

    @pytest.mark.asyncio
    async def test_sends_imessage_with_source_and_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        mgr.notify_phone = "+13055551234"

        sent_messages = []
        def capture_imessage(phone, message):
            sent_messages.append((phone, message))

        monkeypatch.setattr("src.reauth._send_imessage", capture_imessage)

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.monotonic", side_effect=[0, 999999]), \
             patch("src.reauth._is_interactive", return_value=True):
            await mgr._reauth_human("usajobs", "2FA required", timeout_minutes=1)

        assert len(sent_messages) == 1
        phone, msg = sent_messages[0]
        assert phone == "+13055551234"
        assert "usajobs" in msg.lower() or "USAJOBS" in msg
        assert "prepare-sessions" in msg

    @pytest.mark.asyncio
    async def test_records_waiting_then_timeout_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)
        monkeypatch.setattr("src.reauth._send_imessage", MagicMock())

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.monotonic", side_effect=[0, 999999]), \
             patch("src.reauth._is_interactive", return_value=True):
            await mgr._reauth_human("usajobs", "2FA", timeout_minutes=1)

        data = json.loads((tmp_path / "status.json").read_text())
        events = data.get("reauth_events", [])
        outcomes = [e["outcome"] for e in events if e["source"] == "usajobs"]
        assert "waiting" in outcomes
        assert "timeout" in outcomes

    @pytest.mark.asyncio
    async def test_noninteractive_returns_false_immediately_without_polling(self, tmp_path, monkeypatch):
        """Background runs must NOT block waiting for a human. _reauth_human should
        notify and return False at once — never entering the poll loop — so the
        orchestrator can move on to other sources instead of stalling for minutes."""
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)

        sent = []
        monkeypatch.setattr("src.reauth._send_imessage", lambda phone, msg: sent.append((phone, msg)))

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        mgr.notify_phone = "+13055551234"

        (tmp_path / "usajobs_chromium.json").write_text("{}")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("src.reauth._is_interactive", return_value=False):
            result = await mgr._reauth_human("usajobs", "2FA required", timeout_minutes=30)

        assert result is False
        mock_sleep.assert_not_called()          # never polled — returned immediately
        assert len(sent) == 1                   # still notified the user
        assert "prepare-sessions" in sent[0][1]

        data = json.loads((tmp_path / "status.json").read_text())
        outcomes = [e["outcome"] for e in data.get("reauth_events", []) if e["source"] == "usajobs"]
        assert "skipped_noninteractive" in outcomes
        assert "timeout" not in outcomes

    @pytest.mark.asyncio
    async def test_noninteractive_warning_does_not_include_phone_number(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("src.reauth.SESSIONS_DIR", tmp_path)
        monkeypatch.setattr("src.reauth._send_imessage", lambda phone, msg: None)
        from src import notifier
        notifier._last_notification_times.clear()

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        mgr.notify_phone = "+13055551234"

        with patch("src.reauth._is_interactive", return_value=False), \
             patch("src.notifier._send_telegram"), \
             patch("src.session_watchdog._stage_prepare_sessions", return_value=True), \
             patch("src.notifier._desktop_notify") as mock_desktop:
            result = await mgr._reauth_human("usajobs", "2FA required", timeout_minutes=30)

        assert result is False
        assert mock_desktop.call_count == 1
        title, message = mock_desktop.call_args.args[:2]
        assert title == "usajobs session needs refresh"
        assert "Tap to open Terminal and refresh" in message
        assert "+13055551234" not in title
        assert "+13055551234" not in message
        data = json.loads((tmp_path / "status.json").read_text())
        detail = data["alerts"][-1]["detail"]
        assert "+13055551234" not in detail
        assert "[phone]" not in detail
        assert "Auth refresh instructions were sent" in detail

    def test_missing_imessage_phone_records_without_desktop_popup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src import reauth

        desktop = []
        monkeypatch.setattr("src.notifier._desktop_notify", lambda *args, **kwargs: desktop.append(args))
        monkeypatch.setattr("src.notifier._send_telegram", lambda *args, **kwargs: None)

        reauth._send_imessage("", "prepare-sessions")

        assert desktop == []
        data = json.loads((tmp_path / "status.json").read_text())
        assert data["alerts"][-1]["title"] == "iMessage not configured"


# ── _write_regression_test & _notify_correction ────────────────────────────────

class TestSelfHealingNotifications:
    def test_write_regression_test_creates_file_with_valid_python(self, tmp_path):
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        test_file = tmp_path / "test_reauth_regressions.py"
        mgr.regression_test_path = test_file

        mgr._write_regression_test("jobright", "automated", "redirect to /login")

        assert test_file.exists()
        content = test_file.read_text()
        assert "def test_regression_jobright_" in content
        assert "redirect to /login" in content
        assert "AUTOMATED_SOURCES" in content

    def test_write_regression_test_appends_on_subsequent_calls(self, tmp_path):
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        test_file = tmp_path / "test_reauth_regressions.py"
        test_file.write_text('"""existing header"""\nimport pytest\n')
        mgr.regression_test_path = test_file

        mgr._write_regression_test("indeed", "automated", "cookie missing")

        content = test_file.read_text()
        assert "existing header" in content
        assert "def test_regression_indeed_" in content

    def test_notify_correction_sends_success_notification_with_correct_fields(self):
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch("src.reauth.notify_success") as mock_notify:
            mgr._notify_correction("linkedin", "automated", "li_at cookie expired")

        mock_notify.assert_called_once()
        title, detail = mock_notify.call_args[0]
        assert "LINKEDIN" in title
        assert "self-healed" in title
        assert "automated" in detail
        assert "li_at cookie expired" in detail

    def test_notify_correction_human_mode_message(self):
        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch("src.reauth.notify_success") as mock_notify:
            mgr._notify_correction("usajobs", "human", "2FA completed")

        title, detail = mock_notify.call_args[0]
        assert "USAJOBS" in title
        assert "human" in detail
        assert "Session refreshed" in detail


# ── ACES-72: Retry Cap, Backoff, and URL Verification ───────────────────────────

class TestReauthRemediation:
    def test_is_logged_in_url(self):
        from src.reauth import _is_logged_in_url
        assert _is_logged_in_url("jobright", "https://jobright.ai/jobs") is True
        assert _is_logged_in_url("linkedin", "https://www.linkedin.com/feed/") is True
        assert _is_logged_in_url("jobright", "https://jobright.ai/login") is False
        assert _is_logged_in_url("linkedin", "https://www.linkedin.com/checkpoint/challenge") is False
        assert _is_logged_in_url("indeed", "https://secure.indeed.com/account/signin") is False

    @pytest.mark.asyncio
    async def test_retry_cap_limits_attempts(self, tmp_path, monkeypatch):
        # Set up a mock status file with 3 consecutive failures
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.notifier import record_reauth_event
        record_reauth_event("jobright", "automated", "failed", "1")
        record_reauth_event("jobright", "automated", "failed", "2")
        record_reauth_event("jobright", "automated", "failed", "3")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        result = await mgr._reauth_automated("jobright")
        assert result is False

    @pytest.mark.asyncio
    async def test_backoff_delays_attempts(self, tmp_path, monkeypatch):
        # Set up a mock status file with 1 failure
        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.notifier import record_reauth_event
        record_reauth_event("jobright", "automated", "failed", "1")

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        # The backoff for 1 failure is 240 seconds, so it should bail out early (return False)
        # without running _get_source_map
        with patch("src.reauth._get_source_map") as mock_get_map:
            result = await mgr._reauth_automated("jobright")
            assert result is False
            mock_get_map.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_cap_is_half_open_after_cooldown(self, tmp_path, monkeypatch):
        """At/above the cap, the source must not be locked out forever: once
        CIRCUIT_OPEN_COOLDOWN_SECONDS has elapsed since the last failure it gets
        one half-open retry instead of a permanent block (ACES: reauth circuit
        breaker never recovered on its own, even after credentials were fixed)."""
        import json as _json
        from datetime import datetime, timedelta

        monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
        from src.notifier import record_reauth_event
        from src.reauth import CIRCUIT_OPEN_COOLDOWN_SECONDS

        for i in range(3):
            record_reauth_event("jobright", "automated", "failed", str(i))

        status_file = tmp_path / "status.json"
        status = _json.loads(status_file.read_text())
        old_ts = (datetime.utcnow() - timedelta(seconds=CIRCUIT_OPEN_COOLDOWN_SECONDS + 60)).isoformat()
        status["reauth_events"][-1]["ts"] = old_ts
        status_file.write_text(_json.dumps(status))

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})
        with patch("src.reauth._get_source_map") as mock_get_map:
            mock_get_map.return_value = {}
            await mgr._reauth_automated("jobright")
            mock_get_map.assert_called_once()
