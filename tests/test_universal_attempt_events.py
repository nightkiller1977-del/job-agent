"""ACES-399 — universal outer-boundary attempt events (orchestrator.py).

Every apply attempt — registry-adapter path or a legacy scraper with no live
Page at all — must get an attempt_id and a final status/outcome record from
the outer apply loop in Orchestrator.apply_approved(). These tests exercise a
purely legacy path (a MagicMock scraper standing in for e.g. USAJobsScraper)
with no adapters/session.py involved at all, proving the universal wrapper
covers paths that never touch the registry.

Pattern mirrors tests/test_apply_ats_nonfatal.py (Orchestrator.__new__ +
stubbed state/cloud/gate helpers, real SOURCE_MAP patched to a fake scraper).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.events import RunLog, read_run


def _make_orch(tmp_path, jobs):
    orch = Orchestrator.__new__(Orchestrator)
    resume_pdf = tmp_path / "resume.pdf"
    resume_pdf.write_text("real resume")
    orch.config = {"local_resume_path": str(resume_pdf)}

    state = MagicMock()
    state.get_approved_unapplied.return_value = jobs
    orch.state = state

    orch._pull_approved_from_cloud = AsyncMock()
    orch._push_apply_attempt_to_cloud = AsyncMock()
    orch._push_status_to_cloud = AsyncMock()
    orch._filter_jobs = lambda jobs, **kw: jobs
    orch._classify_apply_readiness = lambda j: ("ready", "")
    return orch, state


@pytest.mark.asyncio
async def test_legacy_path_gets_universal_started_and_finished_events(tmp_path):
    """A source with NO live Page anywhere in its apply() (e.g. usajobs) still
    gets one attempt_id joining a started/finished pair, with rich evidence
    explicitly marked unavailable."""
    job = {"job_id": "j1", "title": "Analyst", "company": "Acme",
          "source": "usajobs", "score": 90}
    orch, state = _make_orch(tmp_path, [job])

    scraper = MagicMock()
    scraper.apply = AsyncMock(return_value=True)  # no run_log, no ctx — pure legacy
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")

    with patch.dict("src.orchestrator.SOURCE_MAP", {"usajobs": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"), \
         patch("src.orchestrator._get_run_log", return_value=run_log):
        await orch.apply_approved(auto_submit=True)

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    started = [e for e in events if e["event"] == "apply_attempt_started"]
    finished = [e for e in events if e["event"] == "apply_attempt_finished"]

    assert len(started) == 1
    assert len(finished) == 1
    assert started[0]["attempt_id"] == finished[0]["attempt_id"]
    assert started[0]["job_id"] == "j1"
    assert started[0]["source"] == "usajobs"
    assert started[0]["rich_evidence_available"] is False
    assert finished[0]["rich_evidence_available"] is False
    assert finished[0]["status"] == "applied"
    assert finished[0]["applied"] is True

    # No forensic_phase rich evidence exists for this attempt_id — legacy path.
    rich = [e for e in events if e["event"] == "forensic_phase"
           and e.get("attempt_id") == started[0]["attempt_id"]]
    assert rich == []

    state.set_status.assert_any_call("j1", "applied")


@pytest.mark.asyncio
async def test_legacy_path_finished_event_fires_even_when_apply_raises(tmp_path):
    """The outer finally must close the attempt record on the crash path too
    (never leave a started event dangling with no matching finished event)."""
    job = {"job_id": "j2", "title": "Analyst", "company": "Acme",
          "source": "usajobs", "score": 90}
    orch, state = _make_orch(tmp_path, [job])

    scraper = MagicMock()
    scraper.apply = AsyncMock(side_effect=RuntimeError("boom"))
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")

    with patch.dict("src.orchestrator.SOURCE_MAP", {"usajobs": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"), \
         patch("src.orchestrator._get_run_log", return_value=run_log):
        await orch.apply_approved(auto_submit=True)

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    started = [e for e in events if e["event"] == "apply_attempt_started"]
    finished = [e for e in events if e["event"] == "apply_attempt_finished"]
    assert len(started) == 1
    assert len(finished) == 1
    assert started[0]["attempt_id"] == finished[0]["attempt_id"]
    assert finished[0]["status"] == "error"
    assert finished[0]["applied"] is False


@pytest.mark.asyncio
async def test_legacy_path_not_applied_records_false_and_real_status(tmp_path):
    job = {"job_id": "j3", "title": "Analyst", "company": "Acme",
          "source": "usajobs", "score": 90}
    orch, state = _make_orch(tmp_path, [job])

    scraper = MagicMock()
    scraper.apply = AsyncMock(return_value=False)
    scraper.last_apply_status = "form_not_reached"
    scraper.last_apply_detail = "no apply button found"
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")

    with patch.dict("src.orchestrator.SOURCE_MAP", {"usajobs": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"), \
         patch("src.orchestrator._get_run_log", return_value=run_log):
        await orch.apply_approved(auto_submit=True)

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    finished = [e for e in events if e["event"] == "apply_attempt_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "form_not_reached"
    assert finished[0]["applied"] is False


@pytest.mark.asyncio
async def test_universal_events_never_break_the_run_when_get_run_log_fails(tmp_path):
    """If the shared RunLog singleton can't be obtained at all, the apply run
    must still complete and the job must still be marked applied — universal
    capture is best-effort, never load-bearing."""
    job = {"job_id": "j4", "title": "Analyst", "company": "Acme",
          "source": "usajobs", "score": 90}
    orch, state = _make_orch(tmp_path, [job])

    scraper = MagicMock()
    scraper.apply = AsyncMock(return_value=True)
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    with patch.dict("src.orchestrator.SOURCE_MAP", {"usajobs": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"), \
         patch("src.orchestrator._get_run_log", side_effect=RuntimeError("no run log")):
        await orch.apply_approved(auto_submit=True)

    state.set_status.assert_any_call("j4", "applied")
