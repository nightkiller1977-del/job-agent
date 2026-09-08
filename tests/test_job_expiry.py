"""
Job expiry feature tests.

Covers:
  - check_job_alive(): 404/410 → dead, closed-marker body → dead,
    healthy 200 → alive, auth wall / network error → unknown (never expire)
  - StateManager.mark_expired(): status + extra_json metadata, exclusion from
    the applyable pools, no resurrection via upsert_job()
  - Orchestrator.expiry_sweep(): TTL fallback, probe pass, throttling,
    config disable
  - apply_approved(): approved-but-expired job is skipped with a logged
    reason and surfaced via a status change
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.job_expiry import check_job_alive
from src.orchestrator import Orchestrator
from src.sources.base import JobExpiredError
from src.state_manager import StateManager, parse_extra_json


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_status(tmp_path, monkeypatch):
    status_file = tmp_path / "agent_status.json"
    monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)
    return status_file


def _make_orchestrator(tmp_path, config_extra: dict | None = None) -> Orchestrator:
    """Orchestrator wired to a temp DB with no cloud sync (mirrors test_reauth_feature)."""
    resume_pdf = tmp_path / "resume.pdf"
    resume_pdf.write_text("real resume")
    config = {"state_db_path": str(tmp_path / "jobs.db"), "local_resume_path": str(resume_pdf)}
    if config_extra:
        config.update(config_extra)
    with patch("src.orchestrator.JobScorer"):
        orc = Orchestrator.__new__(Orchestrator)
        orc.config = config
        orc.state = StateManager(config["state_db_path"])
        orc.scorer = MagicMock()
    return orc


@pytest.fixture
def orchestrator(tmp_path):
    return _make_orchestrator(tmp_path)


def _job(job_id="j1", status="approved", source="jobright", days_old=0, **overrides):
    job = {
        "job_id": job_id,
        "source": source,
        "title": "Director of Engineering",
        "company": "Acme",
        "url": "https://jobright.ai/jobs/info/abc123",
        "status": status,
        "score": 90,
        "discovered_at": (datetime.utcnow() - timedelta(days=days_old)).isoformat(),
    }
    job.update(overrides)
    return job


def _mock_response(status_code=200, text="<html>Apply now</html>"):
    return httpx.Response(
        status_code,
        text=text,
        request=httpx.Request("GET", "https://example.com/job/1"),
    )


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    async def get(self, url):
        if self._exc:
            raise self._exc
        return self._response


# ── check_job_alive() ─────────────────────────────────────────────────────────

class TestCheckJobAlive:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [404, 410])
    async def test_gone_status_is_dead(self, code):
        alive, reason = await check_job_alive(
            "https://example.com/job/1", client=_FakeClient(_mock_response(code))
        )
        assert alive is False
        assert str(code) in reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "marker",
        [
            "No longer accepting applications",
            "This job has expired",
            "position has been filled",
            "Announcement has closed",
        ],
    )
    async def test_closed_marker_in_body_is_dead(self, marker):
        body = f"<html><body><h1>Engineer</h1><p>{marker}</p></body></html>"
        alive, reason = await check_job_alive(
            "https://example.com/job/1", client=_FakeClient(_mock_response(200, body))
        )
        assert alive is False
        assert "closed marker" in reason

    @pytest.mark.asyncio
    async def test_healthy_page_is_alive(self):
        alive, _ = await check_job_alive(
            "https://example.com/job/1",
            client=_FakeClient(_mock_response(200, "<html>Apply for this role</html>")),
        )
        assert alive is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [401, 403, 429, 500, 503, 999])
    async def test_ambiguous_status_is_unknown(self, code):
        alive, _ = await check_job_alive(
            "https://example.com/job/1", client=_FakeClient(_mock_response(code))
        )
        assert alive is None

    @pytest.mark.asyncio
    async def test_network_error_is_unknown_not_dead(self):
        alive, reason = await check_job_alive(
            "https://example.com/job/1",
            client=_FakeClient(exc=httpx.ConnectError("boom")),
        )
        assert alive is None
        assert "probe error" in reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["", None, "not-a-url", "ftp://x"])
    async def test_unprobeable_url_is_unknown(self, url):
        alive, _ = await check_job_alive(url)
        assert alive is None


# ── StateManager.mark_expired() ──────────────────────────────────────────────

class TestMarkExpired:
    def test_sets_status_and_metadata(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        state.upsert_job(_job("e1", status="approved"))

        assert state.mark_expired("e1", reason="http 404", signal="probe") is True

        row = state.get_job("e1")
        assert row["status"] == "expired"
        extra = parse_extra_json(row["extra_json"])
        assert extra["expired_reason"] == "http 404"
        assert extra["expired_signal"] == "probe"
        assert extra["expired_prior_status"] == "approved"
        assert extra["expired_at"]

    def test_preserves_existing_extra_json(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        state.upsert_job(_job("e2", status="approved", has_easy_apply=True))
        state.record_apply_attempt("e2", "browser_timeout", "slow page")

        state.mark_expired("e2", reason="ttl", signal="ttl")

        extra = parse_extra_json(state.get_job("e2")["extra_json"])
        assert extra["has_easy_apply"] is True
        assert extra["apply_last_status"] == "browser_timeout"
        assert extra["expired_signal"] == "ttl"

    def test_missing_job_returns_false(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        assert state.mark_expired("nope", reason="x") is False

    def test_expired_excluded_from_applyable_pools(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        state.upsert_job(_job("keep", status="approved"))
        state.upsert_job(_job("dead", status="approved"))
        state.upsert_job(_job("pend", status="discovered"))
        state.upsert_job(_job("pend_dead", status="discovered"))

        state.mark_expired("dead", reason="gone", signal="source")
        state.mark_expired("pend_dead", reason="gone", signal="source")

        approved_ids = {j["job_id"] for j in state.get_approved_unapplied()}
        pending_ids = {j["job_id"] for j in state.get_pending_review()}
        assert approved_ids == {"keep"}
        assert pending_ids == {"pend"}

    def test_rediscovery_does_not_resurrect_expired_job(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        state.upsert_job(_job("dead", status="discovered"))
        state.mark_expired("dead", reason="gone", signal="source")

        # Scraper re-finds the same posting on a later run
        assert state.upsert_job(_job("dead", status="discovered")) is False
        assert state.get_job("dead")["status"] == "expired"

    def test_stale_query_ignores_expired_jobs(self, tmp_path):
        state = StateManager(db_path=str(tmp_path / "jobs.db"))
        state.upsert_job(_job("old_dead", status="discovered", days_old=90))
        state.mark_expired("old_dead", reason="gone", signal="source")
        assert state.get_stale_jobs(max_age_days=30) == []


# ── Orchestrator.expiry_sweep() ──────────────────────────────────────────────

class TestExpirySweep:
    @pytest.mark.asyncio
    async def test_ttl_expires_old_jobs_only(self, tmp_path, tmp_status):
        orc = _make_orchestrator(tmp_path, {"expiry": {"max_age_days": 30}})
        orc.state.upsert_job(_job("old_disc", status="discovered", days_old=45))
        orc.state.upsert_job(_job("old_appr", status="approved", days_old=45))
        orc.state.upsert_job(_job("fresh", status="approved", days_old=2))
        orc.state.upsert_job(_job("applied", status="applied", days_old=90))

        summary = await orc.expiry_sweep(force=True)

        assert summary["ran"] is True
        assert summary["expired_ttl"] == 2
        assert orc.state.get_job("old_disc")["status"] == "expired"
        assert orc.state.get_job("old_appr")["status"] == "expired"
        assert orc.state.get_job("fresh")["status"] == "approved"
        # Applied jobs are history, never TTL-expired
        assert orc.state.get_job("applied")["status"] == "applied"
        extra = parse_extra_json(orc.state.get_job("old_appr")["extra_json"])
        assert extra["expired_signal"] == "ttl"
        assert "older than 30 days" in extra["expired_reason"]

    @pytest.mark.asyncio
    async def test_probe_expires_dead_url_and_keeps_unknown(self, tmp_path, tmp_status):
        orc = _make_orchestrator(
            tmp_path, {"expiry": {"probe_enabled": True, "max_age_days": 30}}
        )
        orc.state.upsert_job(_job("dead", status="approved", url="https://x.com/dead"))
        orc.state.upsert_job(_job("unsure", status="approved", url="https://x.com/wall"))
        orc.state.upsert_job(_job("live", status="approved", url="https://x.com/live"))

        results = {
            "https://x.com/dead": (False, "http 404"),
            "https://x.com/wall": (None, "http 403 (needs session/blocked)"),
            "https://x.com/live": (True, "http 200"),
        }

        async def fake_probe(url, timeout_s=10.0, client=None):
            return results[url]

        with patch("src.orchestrator.check_job_alive", side_effect=fake_probe):
            summary = await orc.expiry_sweep(force=True)

        assert summary["expired_probe"] == 1
        assert summary["probed"] == 3
        assert orc.state.get_job("dead")["status"] == "expired"
        assert orc.state.get_job("unsure")["status"] == "approved"
        assert orc.state.get_job("live")["status"] == "approved"
        extra = parse_extra_json(orc.state.get_job("dead")["extra_json"])
        assert extra["expired_signal"] == "probe"
        assert extra["expired_reason"] == "http 404"

    @pytest.mark.asyncio
    async def test_sweep_is_throttled_between_runs(self, tmp_path, tmp_status):
        orc = _make_orchestrator(tmp_path, {"expiry": {"sweep_interval_hours": 24}})
        first = await orc.expiry_sweep()
        second = await orc.expiry_sweep()
        forced = await orc.expiry_sweep(force=True)
        assert first["ran"] is True
        assert second["ran"] is False
        assert forced["ran"] is True

    @pytest.mark.asyncio
    async def test_sweep_disabled_via_config(self, tmp_path, tmp_status):
        orc = _make_orchestrator(tmp_path, {"expiry": {"enabled": False}})
        orc.state.upsert_job(_job("old", status="approved", days_old=90))
        summary = await orc.expiry_sweep(force=True)
        assert summary["ran"] is False
        assert orc.state.get_job("old")["status"] == "approved"


# ── apply_approved(): approved-but-expired skip ──────────────────────────────

class TestApplyApprovedExpired:
    @pytest.mark.asyncio
    async def test_expired_job_is_skipped_with_reason_and_status_change(
        self, tmp_path, tmp_status, caplog
    ):
        orc = _make_orchestrator(tmp_path, {"expiry": {"enabled": False}})
        orc.state.upsert_job(_job("gone1", status="approved"))

        scraper = AsyncMock()
        scraper.apply = AsyncMock(
            side_effect=JobExpiredError("LinkedIn: Job is closed or no longer accepting applications.")
        )
        scraper_cls = MagicMock(return_value=scraper)

        with patch.dict("src.orchestrator.SOURCE_MAP", {"jobright": scraper_cls}), \
             patch.object(Orchestrator, "_pull_approved_from_cloud", new_callable=AsyncMock), \
             patch.object(Orchestrator, "_push_status_to_cloud", new_callable=AsyncMock) as push_status, \
             patch.object(Orchestrator, "_push_apply_attempt_to_cloud", new_callable=AsyncMock), \
             caplog.at_level("WARNING", logger="src.orchestrator"):
            await orc.apply_approved(auto_submit=False)

        row = orc.state.get_job("gone1")
        assert row["status"] == "expired"
        extra = parse_extra_json(row["extra_json"])
        assert extra["expired_signal"] == "source"
        assert "no longer accepting applications" in extra["expired_reason"]
        # The failed attempt is recorded for telemetry, with the reason
        assert extra["apply_last_status"] == "expired"
        # The status change is surfaced (pushed to the dashboard)
        push_status.assert_awaited_once_with("gone1", "expired")
        # And the skip reason is logged
        assert any("apply.skip.expired" in rec.getMessage() for rec in caplog.records)

        # Never auto-selected again
        assert orc.state.get_approved_unapplied() == []
