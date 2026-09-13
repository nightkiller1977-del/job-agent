from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.notifier as notifier
import src.session_watchdog as session_watchdog
from src.orchestrator import Orchestrator
from src.session_watchdog import ReauthPreflightResult, SessionHealth
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


def _make_orchestrator(tmp_path) -> Orchestrator:
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


def _apply_patches():
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
        patch("src.orchestrator.record_run_stats"),
    )


def test_linkedin_old_export_with_valid_auth_cookie_is_stale_not_expired(tmp_path, monkeypatch):
    session_file = tmp_path / "linkedin_chromium.json"
    session_file.write_text(json.dumps({
        "cookies": [
            {
                "name": "li_at",
                "domain": ".linkedin.com",
                "expires": time.time() + 24 * 3600,
            }
        ]
    }))
    old_mtime = time.time() - 72 * 3600
    os.utime(session_file, (old_mtime, old_mtime))
    monkeypatch.setattr(session_watchdog, "SESSIONS_DIR", tmp_path)

    health = session_watchdog.check_session_health(["linkedin"])[0]

    assert health.status == "stale"
    assert "expired" not in health.detail.lower()


@pytest.mark.asyncio
async def test_preflight_only_reports_verified_durable_refresh(monkeypatch):
    expired = SessionHealth(
        source="linkedin",
        status="expired",
        age_hours=60,
        session_path=MagicMock(),
        detail="expired",
    )
    still_missing = SessionHealth(
        source="linkedin",
        status="missing",
        age_hours=float("inf"),
        session_path=MagicMock(),
        detail="missing",
    )
    checks = iter([[expired], [still_missing]])
    monkeypatch.setattr(session_watchdog, "check_session_health", lambda sources: next(checks))
    deep_link = MagicMock()
    monkeypatch.setattr(session_watchdog, "_send_deep_link_notification", deep_link)

    manager = MagicMock()
    manager.attempt_automated = AsyncMock(return_value=True)
    with patch("src.reauth.ReauthManager", return_value=manager):
        result = await session_watchdog.preflight_session_check_with_reauth(["linkedin"], {})

    assert getattr(result, "attempted_sources", frozenset()) == frozenset({"linkedin"})
    assert result.refreshed_sources == frozenset()
    assert result.notified_sources == frozenset({"linkedin"})


def test_warning_durable_dedupe_suppresses_desktop_and_telegram(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "STATUS_FILE", tmp_path / "agent_status.json")
    notifier._last_notification_times.clear()
    send_telegram = MagicMock()
    desktop_notify = MagicMock()
    monkeypatch.setattr(notifier, "_send_telegram", send_telegram)
    monkeypatch.setattr(notifier, "_desktop_notify", desktop_notify)

    notifier.notify_warning(
        "Apply run: nothing submitted",
        "first detail",
        dedupe_key="apply_nothing_submitted",
        dedupe_seconds=21600,
    )
    notifier.notify_warning(
        "Apply run: nothing submitted",
        "second detail",
        dedupe_key="apply_nothing_submitted",
        dedupe_seconds=21600,
    )

    assert send_telegram.call_count == 1
    assert desktop_notify.call_count == 1


@pytest.mark.asyncio
async def test_apply_nothing_submitted_uses_stable_six_hour_warning_key(tmp_path):
    orchestrator = _make_orchestrator(tmp_path)
    job = _approved_job("warning-job", "linkedin")
    _seed_job(orchestrator, job)

    scraper = AsyncMock()
    scraper.apply = AsyncMock(return_value=False)
    scraper.last_apply_status = "submit_not_found"
    scraper.last_apply_detail = "submit control not found"
    scraper._apply_analytics = None
    scraper._apply_validation_metrics = {}
    scraper.last_apply_ats_url = ""
    scraper_cls = MagicMock(return_value=scraper)
    notify_warning = MagicMock()
    patches = _apply_patches()

    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.notify_warning", notify_warning), \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9]:
        await orchestrator.apply_approved(auto_submit=True)

    matching = [
        call for call in notify_warning.call_args_list
        if call.args and call.args[0] == "Apply run: nothing submitted"
    ]
    assert len(matching) == 1
    assert matching[0].kwargs["dedupe_key"] == "apply_nothing_submitted"
    assert matching[0].kwargs["dedupe_seconds"] == 21600


@pytest.mark.asyncio
async def test_preflight_attempt_ownership_seeds_apply_guard_and_reloads_ready_rows(tmp_path):
    orchestrator = _make_orchestrator(tmp_path)
    blocked = _approved_job("blocked-li", "linkedin")
    already_ready = _approved_job("ready-li", "linkedin")
    _seed_job(orchestrator, blocked)
    _seed_job(orchestrator, already_ready)
    orchestrator.state.record_apply_attempt("blocked-li", "linkedin_authwall", "login required")
    orchestrator.state.record_apply_attempt("ready-li", "reauth_failed", "previous reauth failed")

    preflight = SimpleNamespace(
        health={},
        attempted_sources=frozenset({"linkedin"}),
        refreshed_sources=frozenset({"linkedin"}),
        notified_sources=frozenset(),
    )
    async_preflight = AsyncMock(return_value=preflight)
    reauth_manager = MagicMock()
    reauth_manager.handle = AsyncMock(return_value=True)

    scraper = AsyncMock()
    scraper.apply = AsyncMock(return_value=False)
    scraper.last_apply_status = "submit_not_found"
    scraper.last_apply_detail = "not submitted"
    scraper._apply_analytics = None
    scraper._apply_validation_metrics = {}
    scraper.last_apply_ats_url = ""
    scraper_cls = MagicMock(return_value=scraper)
    patches = _apply_patches()

    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.preflight_session_check_with_reauth", async_preflight), \
         patch("src.orchestrator.ReauthManager", return_value=reauth_manager), \
         patch("src.orchestrator.notify_warning"), \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9]:
        await orchestrator.apply_approved(auto_submit=True)

    reauth_manager.handle.assert_not_awaited()
    assert scraper.apply.await_count == 2
    ready_call_job = next(
        call.args[0]
        for call in scraper.apply.await_args_list
        if call.args[0]["job_id"] == "ready-li"
    )
    assert parse_extra_json(ready_call_job.get("extra_json")).get("session_prepared_at")
