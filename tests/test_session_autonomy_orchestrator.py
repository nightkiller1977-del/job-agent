from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.session_watchdog import ReauthPreflightResult
from src.state_manager import StateManager, parse_extra_json


def _approved_job(job_id: str, source: str = "linkedin") -> dict:
    return {
        "job_id": job_id,
        "source": source,
        "title": "Director of Engineering",
        "company": "Acme",
        "url": "https://www.linkedin.com/jobs/view/123",
        "status": "approved",
        "score": 95,
    }


@pytest.fixture
def orchestrator(tmp_path):
    resume_pdf = tmp_path / "resume.pdf"
    resume_pdf.write_text("resume")
    config = {
        "state_db_path": str(tmp_path / "jobs.db"),
        "local_resume_path": str(resume_pdf),
    }
    orc = Orchestrator.__new__(Orchestrator)
    orc.config = config
    orc.state = StateManager(config["state_db_path"])
    orc.scorer = MagicMock()
    return orc


def _seed_job(orchestrator: Orchestrator, job: dict) -> None:
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "approved")


def _common_apply_patches():
    return (
        patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock),
        patch("src.orchestrator.Orchestrator.expiry_sweep", new_callable=AsyncMock),
        patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock),
        patch("src.orchestrator.Orchestrator._push_status_to_cloud", new_callable=AsyncMock),
        patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock),
        patch("src.orchestrator.Orchestrator._mark_confirmation_submitted"),
        patch("src.orchestrator.ResumeTailor", return_value=MagicMock()),
        patch(
            "src.orchestrator.evaluate_resume_gate",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    proceed=True,
                    resume_path=None,
                    status="ready",
                    detail="",
                )
            ),
        ),
        patch("src.orchestrator.notify_info"),
        patch("src.orchestrator.notify_warning"),
        patch("src.orchestrator.record_run_stats"),
    )


@pytest.mark.asyncio
async def test_background_preflight_reauth_unblocks_and_applies_in_same_run(orchestrator):
    job = _approved_job("li-own-auth")
    _seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt(
        "li-own-auth", "linkedin_authwall", "login required"
    )

    scraper = AsyncMock()
    scraper.apply = AsyncMock(return_value=True)
    scraper._apply_analytics = None
    scraper._apply_validation_metrics = {}
    scraper.last_apply_ats_url = ""
    scraper_cls = MagicMock(return_value=scraper)
    preflight = ReauthPreflightResult(
        health={},
        refreshed_sources=frozenset({"linkedin"}),
        notified_sources=frozenset(),
    )
    async_preflight = AsyncMock(return_value=preflight)

    patches = _common_apply_patches()
    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.preflight_session_check_with_reauth", async_preflight, create=True), \
         patch("src.orchestrator.preflight_session_check") as legacy_preflight, \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10]:
        await orchestrator.apply_approved(auto_submit=True)

    async_preflight.assert_awaited_once()
    assert async_preflight.await_args.kwargs["force_reauth"] == {"linkedin"}
    legacy_preflight.assert_not_called()
    scraper.apply.assert_awaited_once()
    row = orchestrator.state.get_job("li-own-auth")
    assert row["status"] == "applied"


@pytest.mark.asyncio
async def test_source_reauth_does_not_clear_external_portal_wall(orchestrator):
    job = _approved_job("li-workday")
    job["url"] = "https://www.linkedin.com/jobs/view/456"
    job["ats_url"] = "https://acme.wd1.myworkdayjobs.com/job/456"
    _seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt(
        "li-workday", "workday_session_expired", "portal login"
    )

    preflight = ReauthPreflightResult(
        health={},
        refreshed_sources=frozenset({"linkedin"}),
        notified_sources=frozenset(),
    )
    async_preflight = AsyncMock(return_value=preflight)
    scraper_cls = MagicMock()

    patches = _common_apply_patches()
    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.preflight_session_check_with_reauth", async_preflight, create=True), \
         patch("src.orchestrator.preflight_session_check"), \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10]:
        await orchestrator.apply_approved(auto_submit=True)

    async_preflight.assert_awaited_once()
    assert async_preflight.await_args.kwargs["force_reauth"] == set()
    scraper_cls.assert_not_called()
    row = orchestrator.state.get_job("li-workday")
    assert "session_prepared_at" not in parse_extra_json(row.get("extra_json"))
    assert parse_extra_json(row.get("extra_json")).get("apply_last_status") == "workday_session_expired"


@pytest.mark.asyncio
async def test_async_preflight_error_falls_back_to_legacy_notification_once(orchestrator):
    job = _approved_job("li-blocked")
    _seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt(
        "li-blocked", "linkedin_authwall", "login required"
    )

    async_preflight = AsyncMock(side_effect=RuntimeError("synthetic preflight failure"))
    patches = _common_apply_patches()
    with patch("src.orchestrator.preflight_session_check_with_reauth", async_preflight, create=True), \
         patch("src.orchestrator.preflight_session_check") as legacy_preflight, \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10]:
        await orchestrator.apply_approved(auto_submit=True)

    async_preflight.assert_awaited_once()
    legacy_preflight.assert_called_once_with(["linkedin"])
