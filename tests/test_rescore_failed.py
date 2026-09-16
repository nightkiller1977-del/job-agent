"""Regression tests for the `rescore` recovery path (#129).

Discovery cannot recover a job whose evaluation failed: already_seen() skips
every known job_id, so a transient SCORING_FAILED row would stay unscored
forever. These tests cover the selector, the flag cleanup, the orchestrator
handler, and CLI wiring.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.scorer import SCORING_FAILED_ACTION, SCORING_FAILED_FLAG
from src.state_manager import StateManager


@pytest.fixture
def state() -> StateManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield StateManager(db_path=str(Path(tmpdir) / "jobs.db"))


def _failed_job(state: StateManager, job_id: str) -> None:
    state.upsert_job(
        {
            "job_id": job_id,
            "source": "linkedin",
            "title": f"Role {job_id}",
            "company": "Acme",
            "status": "discovered",
            "score": None,
            "score_reason": "Scoring error: transient",
            "flags": SCORING_FAILED_FLAG,
        }
    )
    # upsert_job is insert-only; set the failure state explicitly the way the
    # scorer/hydrate persist paths do.
    state.update_score(job_id, None, "Scoring error: transient", SCORING_FAILED_FLAG)


# ---------------------------------------------------------------------------
# StateManager.get_scoring_failed_jobs
# ---------------------------------------------------------------------------

def test_get_scoring_failed_jobs_selects_only_failed_discovered_rows(state):
    _failed_job(state, "failed-1")
    state.upsert_job(
        {
            "job_id": "scored-1",
            "source": "linkedin",
            "title": "Scored",
            "status": "discovered",
            "score": 88,
            "flags": "",
        }
    )
    state.upsert_job(
        {
            "job_id": "approved-failed",
            "source": "linkedin",
            "title": "Approved but flagged",
            "status": "approved",
            "score": None,
            "flags": SCORING_FAILED_FLAG,
        }
    )

    jobs = state.get_scoring_failed_jobs()

    assert [j["job_id"] for j in jobs] == ["failed-1"]


def test_get_scoring_failed_jobs_respects_limit(state):
    for i in range(3):
        _failed_job(state, f"failed-{i}")

    assert len(state.get_scoring_failed_jobs()) == 3
    assert len(state.get_scoring_failed_jobs(limit=2)) == 2
    assert state.get_scoring_failed_jobs(limit=0) == []


def test_get_scoring_failed_jobs_ignores_rows_with_a_real_score(state):
    """A row that has a usable score must never be re-scored, even if flagged."""
    state.upsert_job(
        {
            "job_id": "mixed",
            "source": "linkedin",
            "title": "Mixed",
            "status": "discovered",
        }
    )
    state.update_score("mixed", 72, "OK", SCORING_FAILED_FLAG)

    assert state.get_scoring_failed_jobs() == []


def test_get_scoring_failed_jobs_matches_the_flag_inside_a_flag_set(state):
    """`flags` is a comma-joined set, and clearing preserves unrelated tokens, so
    a row such as `IC_ROLE,SCORING_FAILED` must still be selectable."""
    state.upsert_job(
        {
            "job_id": "compound",
            "source": "linkedin",
            "title": "Compound",
            "status": "discovered",
            "score": None,
            "flags": f"IC_ROLE,{SCORING_FAILED_FLAG}",
        }
    )

    assert [j["job_id"] for j in state.get_scoring_failed_jobs()] == ["compound"]


def test_selector_matches_the_shape_batch_score_persists_on_failure(state):
    """The selector must select exactly what a failed discovery run persists.

    Discovery stores the scored job dict via upsert_job; a failed evaluation
    carries score=None and the SCORING_FAILED flag. This locks the scorer →
    state contract that makes the recovery path reachable.
    """
    failed_job = {
        "job_id": "discovery-failed",
        "source": "linkedin",
        "title": "Staff Engineer",
        "company": "Acme",
        "status": "discovered",
        "score": None,
        "score_reason": "Scoring failed: model timeout",
        "flags": SCORING_FAILED_FLAG,
        "recommended_action": SCORING_FAILED_ACTION,
    }

    assert state.upsert_job(failed_job) is True

    assert [j["job_id"] for j in state.get_scoring_failed_jobs()] == ["discovery-failed"]


# ---------------------------------------------------------------------------
# StateManager.clear_scoring_failed_flag
# ---------------------------------------------------------------------------

def test_clear_scoring_failed_flag_removes_only_that_token(state):
    state.upsert_job({"job_id": "multi", "source": "linkedin", "title": "Role", "status": "discovered"})
    state.update_score("multi", None, "err", "IC_ROLE,SCORING_FAILED")

    state.clear_scoring_failed_flag("multi")

    assert state.get_job("multi")["flags"] == "IC_ROLE"


def test_clear_scoring_failed_flag_is_a_noop_for_other_rows(state):
    state.upsert_job({"job_id": "clean", "source": "linkedin", "title": "Role", "status": "discovered"})
    state.update_score("clean", 80, "ok", "IC_ROLE")

    state.clear_scoring_failed_flag("clean")

    assert state.get_job("clean")["flags"] == "IC_ROLE"


def test_clear_scoring_failed_flag_leaves_empty_as_empty(state):
    state.upsert_job({"job_id": "solo", "source": "linkedin", "title": "Role", "status": "discovered"})
    state.update_score("solo", None, "err", SCORING_FAILED_FLAG)

    state.clear_scoring_failed_flag("solo")

    assert state.get_job("solo")["flags"] == ""


# ---------------------------------------------------------------------------
# Orchestrator.rescore_failed
# ---------------------------------------------------------------------------

async def _set_success(jobs):
    for job in jobs:
        job["score"] = 90
        job["score_reason"] = "Fit"
        job["flags"] = ""
        job["recommended_action"] = "apply"
        job["status"] = "approved"
    return jobs


def _orchestrator_with_state(state: StateManager) -> Orchestrator:
    orc = Orchestrator.__new__(Orchestrator)
    orc.state = state
    orc.config = {}
    orc.scorer = MagicMock()
    return orc


@pytest.mark.asyncio
async def test_rescore_failed_is_a_noop_without_failed_rows(state):
    orc = _orchestrator_with_state(state)
    orc._score_jobs_with_progress = AsyncMock(side_effect=AssertionError("must not score"))

    result = await orc.rescore_failed()

    assert result == {"matched": 0, "rescored": 0, "still_failed": 0, "triaged": 0}
    orc._score_jobs_with_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_rescore_failed_dry_run_does_not_call_the_scorer(state):
    _failed_job(state, "failed-1")
    orc = _orchestrator_with_state(state)
    orc._score_jobs_with_progress = AsyncMock(side_effect=AssertionError("must not score"))

    result = await orc.rescore_failed(dry_run=True)

    assert result["matched"] == 1
    assert state.get_job("failed-1")["flags"] == SCORING_FAILED_FLAG


@pytest.mark.asyncio
async def test_rescore_failed_persists_a_successful_re_score(state):
    _failed_job(state, "failed-1")
    orc = _orchestrator_with_state(state)

    async def _rescore(jobs):
        for job in jobs:
            job["score"] = 91
            job["score_reason"] = "Strong fit"
            job["flags"] = ""
            job["recommended_action"] = "apply"
            job["status"] = "approved"
        return jobs

    orc._score_jobs_with_progress = _rescore

    result = await orc.rescore_failed()

    assert result == {"matched": 1, "rescored": 1, "still_failed": 0, "triaged": 1}
    row = state.get_job("failed-1")
    assert row["score"] == 91
    assert row["score_reason"] == "Strong fit"
    assert row["flags"] == ""
    assert row["status"] == "approved"
    # Recovery is complete: the row is no longer selectable as failed.
    assert state.get_scoring_failed_jobs() == []


@pytest.mark.asyncio
async def test_rescore_failed_persists_the_flags_from_the_new_verdict(state):
    """The verdict's own flags must survive: passing "" would wipe them and make
    the targeted failure-token clear a no-op."""
    _failed_job(state, "failed-1")
    orc = _orchestrator_with_state(state)

    async def _rescore(jobs):
        for job in jobs:
            job["score"] = 77
            job["score_reason"] = "Cleared role"
            job["flags"] = "CLEARED_ROLE"
            job["recommended_action"] = "apply"
            job["status"] = "approved"
        return jobs

    orc._score_jobs_with_progress = _rescore

    result = await orc.rescore_failed()

    assert result["triaged"] == 1
    row = state.get_job("failed-1")
    assert row["score"] == 77
    assert row["flags"] == "CLEARED_ROLE"
    assert SCORING_FAILED_FLAG not in (row["flags"] or "")


@pytest.mark.asyncio
async def test_rescore_failed_keeps_still_failing_rows_selectable(state):
    _failed_job(state, "failed-1")
    orc = _orchestrator_with_state(state)

    async def _still_fails(jobs):
        for job in jobs:
            job["score"] = None
            job["score_reason"] = "No model available"
            job["flags"] = SCORING_FAILED_FLAG
            job["recommended_action"] = SCORING_FAILED_ACTION
            job["status"] = "discovered"
        return jobs

    orc._score_jobs_with_progress = _still_fails

    result = await orc.rescore_failed()

    assert result == {"matched": 1, "rescored": 0, "still_failed": 1, "triaged": 0}
    assert state.get_job("failed-1")["flags"] == SCORING_FAILED_FLAG
    assert len(state.get_scoring_failed_jobs()) == 1


@pytest.mark.asyncio
async def test_rescore_failed_marks_a_null_score_as_still_failed(state):
    """Defensive: a bare None score must not be counted as a successful re-score."""
    _failed_job(state, "failed-1")
    orc = _orchestrator_with_state(state)

    async def _null_score(jobs):
        for job in jobs:
            job["score"] = None
            job["flags"] = ""
            job["recommended_action"] = "apply"
            job["status"] = "approved"
        return jobs

    orc._score_jobs_with_progress = _null_score

    result = await orc.rescore_failed()

    assert result["still_failed"] == 1
    assert result["triaged"] == 0
    assert state.get_job("failed-1")["flags"] == SCORING_FAILED_FLAG


@pytest.mark.asyncio
async def test_rescore_failed_does_not_touch_successfully_scored_rows(state):
    _failed_job(state, "failed-1")
    state.upsert_job(
        {
            "job_id": "scored-1",
            "source": "linkedin",
            "title": "Scored",
            "status": "approved",
            "score": 88,
        }
    )
    state.update_score("scored-1", 88, "Good fit", "")
    orc = _orchestrator_with_state(state)
    orc._score_jobs_with_progress = _set_success

    await orc.rescore_failed()

    untouched = state.get_job("scored-1")
    assert untouched["score"] == 88
    assert untouched["score_reason"] == "Good fit"


# ---------------------------------------------------------------------------
# Minimum-apply-score policy (fail closed on an unscored job)
# ---------------------------------------------------------------------------

def test_meets_min_apply_score_holds_unscored_and_invalid_values():
    from src.orchestrator import meets_min_apply_score

    assert meets_min_apply_score(50, 50) is True
    assert meets_min_apply_score(60, 50) is True
    assert meets_min_apply_score(49, 50) is False
    # No evaluation happened (SCORING_FAILED) → cannot be shown to clear policy.
    assert meets_min_apply_score(None, 50) is False
    assert meets_min_apply_score("90", 50) is False
    assert meets_min_apply_score(True, 50) is False


def _job(job_id, status="approved", source="jobright", **overrides):
    job = {
        "job_id": job_id,
        "source": source,
        "title": "Director of Engineering",
        "company": "Acme",
        "url": "https://jobright.ai/jobs/info/abc123",
        "status": status,
        "score": 90,
    }
    job.update(overrides)
    return job


def _make_orchestrator(tmp_path, config_extra=None):
    config = {"state_db_path": str(tmp_path / "jobs.db")}
    if config_extra:
        config.update(config_extra)
    orc = Orchestrator.__new__(Orchestrator)
    orc.config = config
    orc.state = StateManager(config["state_db_path"])
    orc.scorer = MagicMock()
    return orc


@pytest.mark.asyncio
async def test_unscored_approved_job_is_held_out_of_the_apply_pool(tmp_path, caplog):
    """A SCORING_FAILED row approved by a reviewer has no score to compare, so
    it must not slip past min_apply_score into an employer submission."""
    orc = _make_orchestrator(tmp_path, {"search_settings": {"min_apply_score": 50}})
    orc.state.upsert_job(_job("unscored", score=None, flags=SCORING_FAILED_FLAG))
    scraper = MagicMock()
    scraper.apply = AsyncMock()
    scraper_cls = MagicMock(return_value=scraper)

    with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
         patch.object(Orchestrator, "_pull_approved_from_cloud", new_callable=AsyncMock), \
         patch.object(Orchestrator, "_push_status_to_cloud", new_callable=AsyncMock), \
         patch.object(Orchestrator, "expiry_sweep", new_callable=AsyncMock), \
         caplog.at_level("WARNING", logger="src.orchestrator"):
        await orc.apply_approved(auto_submit=False)

    # Held as approved (not skipped, since it was never evaluated), explicitly
    # logged as held, and never handed to a scraper.
    assert orc.state.get_job("unscored")["status"] == "approved"
    assert any("apply.hold_unscored" in rec.getMessage() for rec in caplog.records)
    scraper.apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_score_approved_job_is_still_skipped(tmp_path):
    orc = _make_orchestrator(tmp_path, {"search_settings": {"min_apply_score": 50}})
    orc.state.upsert_job(_job("low", score=10))

    with patch.object(Orchestrator, "_pull_approved_from_cloud", new_callable=AsyncMock), \
         patch.object(Orchestrator, "_push_status_to_cloud", new_callable=AsyncMock), \
         patch.object(Orchestrator, "expiry_sweep", new_callable=AsyncMock):
        await orc.apply_approved(auto_submit=False)

    assert orc.state.get_job("low")["status"] == "skipped"


def test_queue_scope_excludes_unscored_approved_jobs(monkeypatch):
    """The `apply` preflight must not validate a job the run will hold back."""
    from src import main as main_mod

    jobs = [_job("unscored", score=None)]

    class _FakeState:
        def __init__(self, *a, **k):
            pass

        def get_approved_unapplied(self):
            return jobs

    import src.state_manager as state_mod
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **kw: _FakeState())

    sources, has_jobs = main_mod._apply_queue_scope(
        config={"search_settings": {"min_apply_score": 50}}
    )

    assert sources == []
    assert has_jobs is False


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_rescore_subcommand_is_registered():
    from src.main import build_parser

    args = build_parser().parse_args(["rescore", "--limit", "5", "--dry-run"])

    assert args.command == "rescore"
    assert args.limit == 5
    assert args.dry_run is True


@pytest.mark.asyncio
async def test_rescore_command_dispatches_to_orchestrator():
    from src import main as main_module

    captured: dict = {}

    class _FakeOrchestrator:
        def __init__(self, *a, **k):
            pass

        async def rescore_failed(self, **kwargs):
            captured.update(kwargs)

    args = argparse.Namespace(command="rescore", limit=3, dry_run=True)

    with patch("src.orchestrator.Orchestrator", _FakeOrchestrator):
        rc = await main_module.main_async(args)

    assert rc == 0
    assert captured == {"limit": 3, "dry_run": True}
