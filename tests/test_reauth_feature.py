"""
Feature tests for self-healing auth integration.

Tests the full flow through the Orchestrator — AuthFailedError raised by a
scraper is caught, ReauthManager is invoked, source is retried or skipped.
No live browser or network calls; scrapers and ReauthManager are mocked at
the boundary.

Coverage:
  - discover(): AuthFailedError → reauth succeeds → scraper retried
  - discover(): AuthFailedError → reauth fails → source skipped, 0 jobs
  - discover(): AuthFailedError → reauth succeeds → retry also fails → 0 jobs
  - apply_approved(): AuthFailedError → reauth succeeds → apply retried and submitted
  - apply_approved(): AuthFailedError → reauth fails → job recorded as reauth_failed
  - apply_approved(): AuthFailedError → reauth succeeds → retry apply fails → error recorded
  - _safe_evaluate integration: non-fatal evaluate error returns default, not exception
  - _safe_evaluate integration: browser-death error propagates out of scrape()
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.sources.base import AuthFailedError, JobExpiredError
from src.orchestrator import Orchestrator


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_status(tmp_path, monkeypatch):
    status_file = tmp_path / "agent_status.json"
    monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)
    return status_file


@pytest.fixture
def orchestrator(tmp_path):
    """Orchestrator wired to an in-memory DB with no cloud sync."""
    config = {"state_db_path": str(tmp_path / "jobs.db")}
    with patch("src.orchestrator.JobScorer"), \
         patch("src.orchestrator.Orchestrator.load_credentials_from_dashboard", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator.hydrate_external_jobs", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock):
        orc = Orchestrator.__new__(Orchestrator)
        orc.config = config
        from src.state_manager import StateManager
        orc.state = StateManager(config["state_db_path"])
        orc.scorer = MagicMock()
        orc.scorer.score = MagicMock(return_value=(90, "Good fit", "remote", "apply"))
    return orc


def _approved_job(job_id="job1", source="jobright"):
    return {
        "job_id": job_id,
        "source": source,
        "title": "Director of Engineering",
        "company": "Acme",
        "url": "https://jobright.ai/jobs/info/abc123",
        "status": "approved",
        "score": 90,
    }


# ── discover() integration ────────────────────────────────────────────────────

class TestDiscoverReauth:
    @pytest.mark.asyncio
    async def test_auth_failure_then_reauth_success_retries_scrape(self, orchestrator, tmp_status):
        """AuthFailedError → reauth succeeds → scraper retried → jobs returned."""
        good_job = {"job_id": "j1", "source": "jobright", "title": "Dir Eng", "company": "X", "url": "https://x.com", "status": "discovered"}

        first_scraper = AsyncMock()
        first_scraper.scrape = AsyncMock(side_effect=AuthFailedError("jobright", "redirect to /login"))

        retry_scraper = AsyncMock()
        retry_scraper.scrape = AsyncMock(return_value=[good_job])

        scraper_cls = MagicMock(side_effect=[first_scraper, retry_scraper])

        mock_reauth = AsyncMock(return_value=True)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr:
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.discover(source="jobright", no_review=True)

        mock_reauth.assert_called_once_with("jobright", "redirect to /login", context="discover")
        assert retry_scraper.scrape.call_count == 1

    @pytest.mark.asyncio
    async def test_auth_failure_then_reauth_failure_returns_zero_jobs(self, orchestrator, tmp_status):
        """AuthFailedError → reauth fails → source skipped, 0 jobs saved."""
        scraper = AsyncMock()
        scraper.scrape = AsyncMock(side_effect=AuthFailedError("jobright", "no session"))
        scraper_cls = MagicMock(return_value=scraper)

        mock_reauth = AsyncMock(return_value=False)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr:
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.discover(source="jobright", no_review=True)

        assert orchestrator.state.get_jobs_by_status("discovered") == []

    @pytest.mark.asyncio
    async def test_auth_failure_reauth_succeeds_but_retry_also_fails(self, orchestrator, tmp_status):
        """AuthFailedError → reauth returns True → retry scraper also raises → 0 jobs, no crash."""
        first_scraper = AsyncMock()
        first_scraper.scrape = AsyncMock(side_effect=AuthFailedError("indeed", "bad session"))

        retry_scraper = AsyncMock()
        retry_scraper.scrape = AsyncMock(side_effect=Exception("SSL error"))

        scraper_cls = MagicMock(side_effect=[first_scraper, retry_scraper])
        mock_reauth = AsyncMock(return_value=True)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"indeed": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr:
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.discover(source="indeed", no_review=True)

        assert orchestrator.state.get_jobs_by_status("discovered") == []

    @pytest.mark.asyncio
    async def test_non_auth_exception_is_not_routed_to_reauth(self, orchestrator, tmp_status):
        """A generic Exception must not trigger ReauthManager."""
        scraper = AsyncMock()
        scraper.scrape = AsyncMock(side_effect=Exception("timeout"))
        scraper_cls = MagicMock(return_value=scraper)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr:
            await orchestrator.discover(source="jobright", no_review=True)
            MockReauthMgr.return_value.handle.assert_not_called()


# ── apply_approved() integration ──────────────────────────────────────────────

class TestApplyReauth:
    def _seed_job(self, orchestrator, job):
        orchestrator.state.upsert_job(job)
        orchestrator.state.set_status(job["job_id"], "approved")

    @pytest.mark.asyncio
    async def test_auth_failure_reauth_success_applies_job(self, orchestrator, tmp_status):
        """AuthFailedError in apply() → reauth succeeds → retry apply → submitted."""
        job = _approved_job()
        self._seed_job(orchestrator, job)

        first_scraper = AsyncMock()
        first_scraper.apply = AsyncMock(side_effect=AuthFailedError("jobright", "session expired"))

        retry_scraper = AsyncMock()
        retry_scraper.apply = AsyncMock(return_value=True)
        retry_scraper._apply_analytics = None

        scraper_cls = MagicMock(side_effect=[first_scraper, retry_scraper])
        mock_reauth = AsyncMock(return_value=True)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr, \
             patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._push_status_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator.load_credentials_from_dashboard", new_callable=AsyncMock):
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.apply_approved(auto_submit=True)

        mock_reauth.assert_called_once_with("jobright", "session expired", context="apply")
        assert retry_scraper.apply.call_count == 1
        db_job = orchestrator.state.get_job("job1")
        assert db_job["status"] == "applied"

    @pytest.mark.asyncio
    async def test_auth_failure_reauth_fails_records_reauth_failed(self, orchestrator, tmp_status):
        """AuthFailedError in apply() → reauth fails → job recorded as reauth_failed."""
        job = _approved_job()
        self._seed_job(orchestrator, job)

        scraper = AsyncMock()
        scraper.apply = AsyncMock(side_effect=AuthFailedError("jobright", "no cookie"))
        scraper_cls = MagicMock(return_value=scraper)

        mock_reauth = AsyncMock(return_value=False)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr, \
             patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator.load_credentials_from_dashboard", new_callable=AsyncMock):
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.apply_approved(auto_submit=True)

        db_job = orchestrator.state.get_job("job1")
        extra = json.loads(db_job.get("extra_json") or "{}")
        assert extra.get("apply_last_status") == "reauth_failed"

    @pytest.mark.asyncio
    async def test_auth_failure_reauth_success_retry_apply_fails(self, orchestrator, tmp_status):
        """AuthFailedError → reauth returns True → retry apply raises → error recorded."""
        job = _approved_job()
        self._seed_job(orchestrator, job)

        first_scraper = AsyncMock()
        first_scraper.apply = AsyncMock(side_effect=AuthFailedError("jobright", "expired"))

        retry_scraper = AsyncMock()
        retry_scraper.apply = AsyncMock(side_effect=Exception("ATS portal crashed"))

        scraper_cls = MagicMock(side_effect=[first_scraper, retry_scraper])
        mock_reauth = AsyncMock(return_value=True)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr, \
             patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator.load_credentials_from_dashboard", new_callable=AsyncMock):
            MockReauthMgr.return_value.handle = mock_reauth
            await orchestrator.apply_approved(auto_submit=True)

        db_job = orchestrator.state.get_job("job1")
        extra = json.loads(db_job.get("extra_json") or "{}")
        assert extra.get("apply_last_status") == "reauth_retry_error"

    @pytest.mark.asyncio
    async def test_job_expired_not_routed_to_reauth(self, orchestrator, tmp_status):
        """JobExpiredError must not trigger ReauthManager."""
        job = _approved_job()
        self._seed_job(orchestrator, job)

        scraper = AsyncMock()
        scraper.apply = AsyncMock(side_effect=JobExpiredError("job gone"))
        scraper_cls = MagicMock(return_value=scraper)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch("src.orchestrator.ReauthManager") as MockReauthMgr, \
             patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._push_status_to_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
             patch("src.orchestrator.Orchestrator.load_credentials_from_dashboard", new_callable=AsyncMock):
            await orchestrator.apply_approved(auto_submit=True)
            MockReauthMgr.return_value.handle.assert_not_called()

        assert orchestrator.state.get_job("job1") is None  # expired → deleted


# ── _safe_evaluate integration ────────────────────────────────────────────────

class TestSafeEvaluateIntegration:
    """Verify _safe_evaluate behaviour through a concrete scraper instance."""

    @pytest.mark.asyncio
    async def test_non_fatal_evaluate_error_returns_default(self):
        from src.sources.jobright import JobrightScraper
        scraper = JobrightScraper(config={})
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("some selector parse error"))

        result = await scraper._safe_evaluate(page, "document.title", default="fallback")
        assert result == "fallback"

    @pytest.mark.asyncio
    async def test_browser_death_propagates_out_of_safe_evaluate(self):
        from src.sources.linkedin import LinkedInScraper
        scraper = LinkedInScraper(config={})
        page = AsyncMock()
        page.evaluate = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed")
        )

        with pytest.raises(Exception, match="closed"):
            await scraper._safe_evaluate(page, "document.title", default="x")

    @pytest.mark.asyncio
    async def test_safe_evaluate_scroll_returns_none_default_on_error(self):
        from src.sources.indeed import IndeedScraper
        scraper = IndeedScraper(config={})
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("evaluate timeout expired"))

        result = await scraper._safe_evaluate(
            page, "window.scrollTo(0, document.body.scrollHeight)", default=None
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_safe_evaluate_list_default_on_error(self):
        from src.sources.usajobs import USAJobsScraper
        scraper = USAJobsScraper(config={})
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("execution context destroyed"))

        result = await scraper._safe_evaluate(page, "Array.from(document.links)", default=[])
        assert result == []
