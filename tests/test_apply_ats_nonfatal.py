"""Regression: an ATSReadabilityError on one job must NOT abort the whole apply run.

Before this fix, orchestrator.apply_approved() re-raised ATSReadabilityError, which
crashed the entire command — every remaining approved job went unattempted. This test
pins the corrected behavior: the offending job is recorded as 'ats_failure' and the
loop continues to the next approved job.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.orchestrator import Orchestrator
from src.resume_helper import ATSReadabilityError, ATSValidationResult, PDFTextLayerError


@pytest.mark.asyncio
async def test_pdf_text_layer_error_pauses_the_batch():
    """Regression: an unreadable/corrupt generated PDF means the resume-compilation
    pipeline itself is broken, not just one job. A scraper that raises
    PDFTextLayerError must halt the apply loop for self-healing instead of the
    agent burning through the rest of the approved queue with the same broken
    PDF generation path (see PR #42 review — jobright.py previously swallowed
    this into a per-job False outcome instead of letting it propagate here)."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = {}

    job1 = {"job_id": "j1", "title": "Dir Eng", "company": "Acme", "source": "jobright"}
    job2 = {"job_id": "j2", "title": "VP IT", "company": "Globex", "source": "jobright"}

    state = MagicMock()
    state.get_approved_unapplied.return_value = [job1, job2]
    orch.state = state

    orch._pull_approved_from_cloud = AsyncMock()
    orch._push_apply_attempt_to_cloud = AsyncMock()
    orch._push_status_to_cloud = AsyncMock()
    orch._filter_jobs = lambda jobs, **kw: jobs
    orch._classify_apply_readiness = lambda j: ("ready", "")

    result = ATSValidationResult(
        passed=False, coverage=0.0, matched_keywords=[], unmatched_keywords=[],
        failure_type="pdf_text_layer", detail="Generated PDF has no extractable text layer.",
    )
    scraper = MagicMock()
    scraper.apply = AsyncMock(side_effect=PDFTextLayerError(result))
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"):
        await orch.apply_approved(auto_submit=True)

    # Loop paused after job1 — job2 was never attempted.
    assert scraper.apply.await_count == 1
    statuses = [c.args[1] for c in state.record_apply_attempt.call_args_list]
    assert statuses == ["pdf_text_layer_failed"]


@pytest.mark.asyncio
async def test_ats_readability_error_does_not_abort_batch():
    orch = Orchestrator.__new__(Orchestrator)  # skip __init__ side effects
    orch.config = {}

    job1 = {"job_id": "j1", "title": "Dir Eng", "company": "Acme", "source": "jobright"}
    job2 = {"job_id": "j2", "title": "VP IT", "company": "Globex", "source": "jobright"}

    # State stub
    state = MagicMock()
    state.get_approved_unapplied.return_value = [job1, job2]
    orch.state = state

    # Neutralize cloud / credential / notifier side effects
    orch._pull_approved_from_cloud = AsyncMock()
    orch._push_apply_attempt_to_cloud = AsyncMock()
    orch._push_status_to_cloud = AsyncMock()
    orch._filter_jobs = lambda jobs, **kw: jobs
    orch._classify_apply_readiness = lambda j: ("ready", "")

    # One shared fake scraper: first apply raises ATS error, second succeeds.
    scraper = MagicMock()
    scraper.apply = AsyncMock(side_effect=[ATSReadabilityError("missing keywords"), True])
    scraper._apply_analytics = None
    factory = MagicMock(return_value=scraper)

    with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": factory}, clear=True), \
         patch("src.orchestrator.notify_info"), \
         patch("src.orchestrator.notify_error"), \
         patch("src.orchestrator.notify_warning"), \
         patch("src.orchestrator.record_run_stats"):
        await orch.apply_approved(auto_submit=True)

    # Both jobs were attempted — the batch did NOT abort on the first job's ATS failure.
    assert scraper.apply.await_count == 2

    # First job recorded as ats_failure; second job marked applied.
    statuses = [c.args[1] for c in state.record_apply_attempt.call_args_list]
    assert "ats_failure" in statuses
    state.set_status.assert_any_call("j2", "applied")
