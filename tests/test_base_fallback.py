"""
Unit tests for BaseScraper's ProcessSingleton bypass (base.py).

Coverage:
  - _chrome_is_running()              subprocess detection
  - _export_session_json()            atomic write, no-op, failure logging
  - _start_browser() path selection   Chromium fallback vs persistent Chrome
  - _close_browser() guards           sleep, export ordering, both-close in Chromium path
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.sources.base import BaseScraper


# ── Minimal concrete subclass ──────────────────────────────────────────────────
class _Scraper(BaseScraper):
    name = "testsrc"

    async def scrape(self): return []
    async def apply(self, job): return False
    async def prepare_session(self): pass


# ── Helpers ────────────────────────────────────────────────────────────────────
def _make_playwright_mock():
    """Return (mock_ap_cm, mock_pw, mock_browser, mock_ctx, mock_page).

    mock_ap_cm  — what async_playwright() returns; .start() returns mock_pw
    mock_pw     — the playwright API object (pw.chromium.launch / .launch_persistent_context)
    mock_browser — returned by chromium.launch() for the Chromium fallback path
    mock_ctx    — returned by either launch path
    mock_page   — returned by ctx.new_page()
    """
    mock_page = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.pages = []  # empty → new_page() is called
    mock_ctx.new_page = AsyncMock(return_value=mock_page)

    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_ctx)

    mock_pw = AsyncMock()
    mock_pw.chromium = AsyncMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.chromium.launch_persistent_context = AsyncMock(return_value=mock_ctx)

    mock_ap_cm = MagicMock()
    mock_ap_cm.start = AsyncMock(return_value=mock_pw)

    return mock_ap_cm, mock_pw, mock_browser, mock_ctx, mock_page


def _valid_session_json():
    return json.dumps({"cookies": [{"name": "sid", "value": "x"}], "origins": []})


# ── _chrome_is_running ─────────────────────────────────────────────────────────
class TestChromeIsRunning:
    def test_true_when_pgrep_exits_zero(self):
        with patch("src.sources.base.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _Scraper._chrome_is_running() is True
            mock_run.assert_called_once_with(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True, timeout=3,
            )

    def test_false_when_pgrep_exits_nonzero(self):
        with patch("src.sources.base.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert _Scraper._chrome_is_running() is False

    def test_false_when_subprocess_raises(self):
        with patch("src.sources.base.subprocess.run", side_effect=OSError("no pgrep")):
            assert _Scraper._chrome_is_running() is False


# ── _export_session_json ───────────────────────────────────────────────────────
class TestExportSessionJson:
    @pytest.mark.asyncio
    async def test_writes_atomically_via_tmp_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._context.storage_state = AsyncMock(return_value={
            "cookies": [{"name": "sid", "value": "abc"}],
            "origins": [],
        })

        await scraper._export_session_json()

        export = tmp_path / "testsrc_chromium.json"
        assert export.exists()
        data = json.loads(export.read_text())
        assert data["cookies"][0]["name"] == "sid"
        assert not (tmp_path / "testsrc_chromium.tmp").exists()

    @pytest.mark.asyncio
    async def test_noop_when_context_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = None

        await scraper._export_session_json()

        assert not (tmp_path / "testsrc_chromium.json").exists()

    @pytest.mark.asyncio
    async def test_logs_warning_on_storage_state_failure(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._context.storage_state = AsyncMock(side_effect=RuntimeError("context dead"))

        with caplog.at_level(logging.WARNING, logger="src.sources.base"):
            await scraper._export_session_json()

        assert "failed to export" in caplog.text
        assert not (tmp_path / "testsrc_chromium.json").exists()
        assert not (tmp_path / "testsrc_chromium.tmp").exists()

    @pytest.mark.asyncio
    async def test_stale_json_not_left_on_write_failure(self, tmp_path, monkeypatch):
        """A crash mid-write must not leave a corrupt .json behind."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._context.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})

        original_write = Path.write_text

        def fail_on_tmp(self_path, text, *args, **kwargs):
            if self_path.suffix == ".tmp":
                raise OSError("disk full")
            return original_write(self_path, text, *args, **kwargs)

        with patch.object(Path, "write_text", fail_on_tmp):
            await scraper._export_session_json()

        assert not (tmp_path / "testsrc_chromium.json").exists()
        assert not (tmp_path / "testsrc_chromium.tmp").exists()


# ── _start_browser path selection ─────────────────────────────────────────────
class TestStartBrowserPathSelection:
    @pytest.mark.asyncio
    async def test_background_valid_export_uses_chromium_launch(self, tmp_path, monkeypatch):
        """Background + valid JSON export → chromium.launch(), NOT launch_persistent_context."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        (tmp_path / "testsrc_chromium.json").write_text(_valid_session_json())

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, mock_page = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"):
            mock_stdin.isatty.return_value = False  # background

            page = await scraper._start_browser()

        mock_pw.chromium.launch.assert_called_once()
        mock_pw.chromium.launch_persistent_context.assert_not_called()
        assert page is mock_page

    @pytest.mark.asyncio
    async def test_background_no_export_uses_persistent_chrome(self, tmp_path, monkeypatch, caplog):
        """Background + no export → launch_persistent_context + warning."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        # No export file

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, mock_page = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"), \
             patch.object(scraper, "_chrome_is_running", return_value=False), \
             caplog.at_level(logging.WARNING, logger="src.sources.base"):
            mock_stdin.isatty.return_value = False

            page = await scraper._start_browser()

        mock_pw.chromium.launch.assert_not_called()
        mock_pw.chromium.launch_persistent_context.assert_called_once()
        assert "prepare-sessions" in caplog.text
        assert page is mock_page

    @pytest.mark.asyncio
    async def test_interactive_uses_persistent_chrome_even_with_valid_export(self, tmp_path, monkeypatch):
        """Interactive (TTY present) → always persistent Chrome, never Chromium fallback."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        (tmp_path / "testsrc_chromium.json").write_text(_valid_session_json())

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, mock_page = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"):
            mock_stdin.isatty.return_value = True  # interactive TTY

            page = await scraper._start_browser()

        mock_pw.chromium.launch.assert_not_called()
        mock_pw.chromium.launch_persistent_context.assert_called_once()
        assert page is mock_page

    @pytest.mark.asyncio
    async def test_corrupt_export_deleted_falls_back_to_chrome(self, tmp_path, monkeypatch, caplog):
        """Background + corrupt JSON → export deleted, Chrome fallback, warning logged."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        export = tmp_path / "testsrc_chromium.json"
        export.write_text("{{not: valid: json}}")

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, _ = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"), \
             patch.object(scraper, "_chrome_is_running", return_value=False), \
             caplog.at_level(logging.WARNING, logger="src.sources.base"):
            mock_stdin.isatty.return_value = False

            await scraper._start_browser()

        assert not export.exists()
        mock_pw.chromium.launch.assert_not_called()
        mock_pw.chromium.launch_persistent_context.assert_called_once()
        assert "corrupt" in caplog.text

    @pytest.mark.asyncio
    async def test_background_no_export_chrome_running_warns_processsingleton(
        self, tmp_path, monkeypatch, caplog
    ):
        """Warning includes ProcessSingleton hint when Chrome is running + no export."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, _ = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"), \
             patch.object(scraper, "_chrome_is_running", return_value=True), \
             caplog.at_level(logging.WARNING, logger="src.sources.base"):
            mock_stdin.isatty.return_value = False

            await scraper._start_browser()

        assert "ProcessSingleton" in caplog.text

    @pytest.mark.asyncio
    async def test_persistent_context_always_passes_channel_chrome(self, tmp_path, monkeypatch):
        """Persistent context path always passes channel='chrome'."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, _ = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"), \
             patch.object(scraper, "_chrome_is_running", return_value=False):
            mock_stdin.isatty.return_value = False  # background, no export

            await scraper._start_browser()

        kwargs = mock_pw.chromium.launch_persistent_context.call_args.kwargs
        assert kwargs.get("channel") == "chrome"

    @pytest.mark.asyncio
    async def test_chromium_fallback_does_not_pass_channel(self, tmp_path, monkeypatch):
        """Chromium fallback must NOT pass channel='chrome' (that would cause ProcessSingleton)."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        (tmp_path / "testsrc_chromium.json").write_text(_valid_session_json())

        scraper = _Scraper(config={})
        mock_ap, mock_pw, _, _, _ = _make_playwright_mock()

        with patch("src.sources.base.async_playwright", return_value=mock_ap), \
             patch("sys.stdin") as mock_stdin, \
             patch.object(scraper, "_clear_profile_locks"):
            mock_stdin.isatty.return_value = False

            await scraper._start_browser()

        launch_kwargs = mock_pw.chromium.launch.call_args.kwargs
        assert "channel" not in launch_kwargs


# ── _close_browser ─────────────────────────────────────────────────────────────
class TestCloseBrowser:
    @pytest.mark.asyncio
    async def test_export_called_before_context_close(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        order = []

        scraper._context = AsyncMock()
        scraper._context.close = AsyncMock(side_effect=lambda: order.append("close"))
        scraper._playwright = AsyncMock()

        async def record_export():
            order.append("export")

        with patch.object(scraper, "_export_session_json", new=AsyncMock(side_effect=record_export)), \
             patch("asyncio.sleep"):
            await scraper._close_browser(save_session=True)

        assert order == ["export", "close"]

    @pytest.mark.asyncio
    async def test_no_export_when_save_session_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._playwright = AsyncMock()

        mock_export = AsyncMock()
        with patch.object(scraper, "_export_session_json", new=mock_export), \
             patch("asyncio.sleep"):
            await scraper._close_browser(save_session=False)

        mock_export.assert_not_called()

    @pytest.mark.asyncio
    async def test_sleep_skipped_when_nothing_was_opened(self, monkeypatch):
        """2s sleep must be skipped when _start_browser never ran."""
        scraper = _Scraper(config={})
        assert scraper._context is None and scraper._browser is None

        sleep_calls = []
        with patch("asyncio.sleep", side_effect=lambda t: sleep_calls.append(t)), \
             patch.object(scraper, "_export_session_json", new=AsyncMock()):
            await scraper._close_browser(save_session=False)

        assert sleep_calls == []

    @pytest.mark.asyncio
    async def test_sleep_called_when_context_was_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._playwright = AsyncMock()

        sleep_calls = []
        with patch("asyncio.sleep", side_effect=lambda t: sleep_calls.append(t)), \
             patch.object(scraper, "_export_session_json", new=AsyncMock()):
            await scraper._close_browser(save_session=False)

        assert sleep_calls == [2]

    @pytest.mark.asyncio
    async def test_both_context_and_browser_closed_in_chromium_path(self, tmp_path, monkeypatch):
        """Chromium fallback path: context.close() AND browser.close() both called."""
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        closed = []

        mock_ctx = AsyncMock()
        mock_ctx.close = AsyncMock(side_effect=lambda: closed.append("context"))
        mock_browser = AsyncMock()
        mock_browser.close = AsyncMock(side_effect=lambda: closed.append("browser"))

        scraper._context = mock_ctx
        scraper._browser = mock_browser
        scraper._playwright = AsyncMock()

        with patch("asyncio.sleep"), \
             patch.object(scraper, "_export_session_json", new=AsyncMock()):
            await scraper._close_browser(save_session=False)

        assert "context" in closed
        assert "browser" in closed

    @pytest.mark.asyncio
    async def test_all_handles_reset_to_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.base.SESSIONS_DIR", tmp_path)
        scraper = _Scraper(config={})
        scraper._context = AsyncMock()
        scraper._browser = AsyncMock()
        scraper._playwright = AsyncMock()
        scraper._page = AsyncMock()

        with patch("asyncio.sleep"), \
             patch.object(scraper, "_export_session_json", new=AsyncMock()):
            await scraper._close_browser(save_session=False)

        assert scraper._context is None
        assert scraper._browser is None
        assert scraper._playwright is None
        assert scraper._page is None
