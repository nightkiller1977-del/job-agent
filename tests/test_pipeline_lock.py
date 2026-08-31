"""_pipeline_lock: prevents overlapping discover/apply runs from executing
concurrently — the root cause of duplicate 'nothing submitted' apply runs and
Playwright browser contention when a legacy launchd job and the current one
both fire at the same scheduled time."""
from __future__ import annotations

import src.main as main_mod


def test_second_acquire_is_rejected_while_first_holds_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "project_root", tmp_path)

    with main_mod._pipeline_lock("apply") as first_acquired:
        assert first_acquired is True
        with main_mod._pipeline_lock("apply") as second_acquired:
            assert second_acquired is False


def test_lock_is_released_after_the_with_block(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "project_root", tmp_path)

    with main_mod._pipeline_lock("apply") as acquired:
        assert acquired is True

    with main_mod._pipeline_lock("apply") as acquired_again:
        assert acquired_again is True


def test_different_pipeline_names_do_not_contend(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "project_root", tmp_path)

    with main_mod._pipeline_lock("discover") as discover_acquired:
        assert discover_acquired is True
        with main_mod._pipeline_lock("apply") as apply_acquired:
            assert apply_acquired is True
