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

        from src.reauth import ReauthManager
        mgr = ReauthManager(config={})

        with patch("src.reauth._get_source_map", return_value={"jobright": MagicMock()}):
            result = await mgr._reauth_automated("jobright")

        assert result is False

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
             patch("time.monotonic", side_effect=[0, 30]):
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
             patch("time.monotonic", side_effect=[0, 0, 999999]):
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
             patch("time.monotonic", side_effect=[0, 999999]):
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
             patch("time.monotonic", side_effect=[0, 999999]):
            await mgr._reauth_human("usajobs", "2FA", timeout_minutes=1)

        data = json.loads((tmp_path / "status.json").read_text())
        events = data.get("reauth_events", [])
        outcomes = [e["outcome"] for e in events if e["source"] == "usajobs"]
        assert "waiting" in outcomes
        assert "timeout" in outcomes
