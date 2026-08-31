"""_pipeline_lock / browser_pipeline_lock.pipeline_lock: prevents overlapping
discover/apply/prepare-sessions/heartbeat/auto-fix runs from executing
concurrently — the root cause of duplicate 'nothing submitted' apply runs and
Playwright browser contention when a legacy launchd job and the current one
both fire at the same scheduled time (or when a manual prepare-sessions, or a
watcher-triggered auto-fix, overlaps a scheduled run)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.main as main_mod
import src.browser_pipeline_lock as lock_mod


def test_second_acquire_is_rejected_while_first_holds_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "PROJECT_ROOT", tmp_path)

    with main_mod._pipeline_lock("apply") as first_acquired:
        assert first_acquired is True
        with main_mod._pipeline_lock("apply") as second_acquired:
            assert second_acquired is False


def test_lock_is_released_after_the_with_block(tmp_path, monkeypatch):
    monkeypatch.setattr(lock_mod, "PROJECT_ROOT", tmp_path)

    with main_mod._pipeline_lock("apply") as acquired:
        assert acquired is True

    with main_mod._pipeline_lock("apply") as acquired_again:
        assert acquired_again is True


def test_different_lock_names_do_not_contend(tmp_path, monkeypatch):
    """Generic property of pipeline_lock itself: distinct names are
    independent locks. main_async no longer exercises this directly (see
    test_discover_apply_prepare_sessions_heartbeat_share_one_lock below) —
    every browser-driving command now passes the same BROWSER_PIPELINE_LOCK
    name — but the primitive still supports separate lock names."""
    monkeypatch.setattr(lock_mod, "PROJECT_ROOT", tmp_path)

    with main_mod._pipeline_lock("some-other-lock") as first_acquired:
        assert first_acquired is True
        with main_mod._pipeline_lock("apply") as second_acquired:
            assert second_acquired is True


@pytest.mark.asyncio
async def test_discover_apply_prepare_sessions_heartbeat_share_one_lock(tmp_path, monkeypatch):
    """discover/apply/prepare-sessions/heartbeat all drive the same Playwright
    browser profile, so a legacy scheduler firing discover while the new one
    fires apply (or a manual prepare-sessions overlapping either) must still
    be blocked — which only happens if they all take the SAME lock name."""
    import types

    monkeypatch.setattr(lock_mod, "PROJECT_ROOT", tmp_path)

    mock_orch = MagicMock()
    mock_orch.discover = AsyncMock()
    mock_orch.apply_approved = AsyncMock()
    mock_orch.prepare_sessions = AsyncMock()
    mock_orch.config = {}

    lock_names_used = []
    real_lock = main_mod._pipeline_lock

    def spy_lock(name):
        lock_names_used.append(name)
        return real_lock(name)

    with patch("src.orchestrator.Orchestrator", return_value=mock_orch), \
         patch.object(main_mod, "_pipeline_lock", side_effect=spy_lock), \
         patch("src.session_watchdog.run_heartbeat", new_callable=AsyncMock, return_value={}):

        await main_mod.main_async(types.SimpleNamespace(
            command="discover", source=None, no_review=True))
        await main_mod.main_async(types.SimpleNamespace(
            command="apply", auto_submit=True, limit=None, job_id=None, source=None, company=None))
        await main_mod.main_async(types.SimpleNamespace(
            command="prepare-sessions", source=None, company=None, limit=None))
        await main_mod.main_async(types.SimpleNamespace(
            command="heartbeat", source=None))

    assert lock_names_used == [main_mod.BROWSER_PIPELINE_LOCK] * 4
    mock_orch.discover.assert_awaited_once()
    mock_orch.apply_approved.assert_awaited_once()
    mock_orch.prepare_sessions.assert_awaited_once()


def test_main_and_commander_use_the_same_underlying_lock(tmp_path, monkeypatch):
    """main.py's CLI wrapper and commander.py's attempt_fix() must contend for
    the literal same lock file, not just lock names that happen to match —
    otherwise a scheduled apply run and a watcher-triggered auto-fix could
    still launch competing Playwright contexts."""
    monkeypatch.setattr(lock_mod, "PROJECT_ROOT", tmp_path)

    with main_mod._pipeline_lock(main_mod.BROWSER_PIPELINE_LOCK) as held_by_main:
        assert held_by_main is True
        with lock_mod.pipeline_lock(lock_mod.BROWSER_PIPELINE_LOCK) as held_by_commander_path:
            assert held_by_commander_path is False
