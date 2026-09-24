"""Cloud-sync transport tests: diagnostic retries never repeat state actions."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _client_context(client):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


def test_orchestrator_reuses_process_run_log(tmp_path):
    from src.orchestrator import Orchestrator

    shared = MagicMock()
    with patch("src.orchestrator._get_run_log", return_value=shared):
        orchestrator = Orchestrator(config_path=str(tmp_path / "missing.json"))

    assert orchestrator.run_log is shared


@pytest.mark.asyncio
async def test_pull_retries_one_timeout_then_succeeds(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("SYNC_SECRET", "test-secret")
    client = MagicMock()
    client.get = AsyncMock(side_effect=[httpx.ReadTimeout(""), MagicMock(status_code=200, json=lambda: [])])

    orchestrator = Orchestrator(config_path=str(tmp_path / "missing.json"))
    with patch("httpx.AsyncClient", return_value=_client_context(client)), patch("src.orchestrator.asyncio.sleep", new=AsyncMock()):
        await orchestrator._pull_approved_from_cloud()

    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_pull_approved_preserves_local_applied_state(tmp_path, monkeypatch):
    """A stale cloud approval must not make a submitted job eligible again."""
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    local_job = {
        "job_id": "job-applied",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "status": "approved",
    }
    orchestrator.state.upsert_job(local_job)
    orchestrator.state.set_status(local_job["job_id"], "applied")
    cloud_job = {**local_job, "status": "approved", "status_revision": 4}
    orchestrator._cloud_request = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: [cloud_job])
    )

    await orchestrator._pull_approved_from_cloud()

    preserved = orchestrator.state.get_job("job-applied")
    assert preserved["status"] == "applied"
    from src.state_manager import parse_extra_json
    marker = parse_extra_json(preserved["extra_json"])[
        "cloud_status_sync_pending"
    ]
    assert marker["status"] == "applied"
    assert marker["expected_status"] == "approved"
    assert marker["expected_revision"] == 4


@pytest.mark.asyncio
async def test_pending_applied_sync_retries_until_cloud_confirms(tmp_path, monkeypatch):
    """A failed status push remains durable and a later run clears it on 200."""
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-recovered",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/recovered",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")
    failed = MagicMock(status_code=503)
    confirmed = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "applied",
            "status_revision": 1,
            "deduplicated": True,
        },
    )
    orchestrator._cloud_request = AsyncMock(side_effect=[failed, confirmed])

    first_result = await orchestrator._push_status_to_cloud(job["job_id"], "applied")

    assert first_result is False
    pending = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )
    assert pending["cloud_status_sync_pending"]["status"] == "applied"

    await orchestrator._retry_pending_cloud_status_sync()

    assert orchestrator._cloud_request.await_count == 2
    first_payload = orchestrator._cloud_request.await_args_list[0].kwargs["json"]
    retry_payload = orchestrator._cloud_request.await_args_list[1].kwargs["json"]
    assert first_payload["idempotency_key"]
    assert retry_payload["idempotency_key"] == first_payload["idempotency_key"]
    assert first_payload["expected_status"] == "approved"
    assert first_payload["expected_revision"] == 0
    cleared = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )
    assert "cloud_status_sync_pending" not in cleared


@pytest.mark.asyncio
async def test_status_push_keeps_obligation_when_200_reports_wrong_status(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-status-mismatch",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/status-mismatch",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")
    orchestrator._cloud_request = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {"status": "approved", "deduplicated": True},
        )
    )

    result = await orchestrator._push_status_to_cloud(job["job_id"], "applied")

    assert result is False
    extra = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )
    assert extra["cloud_status_sync_pending"]["status"] == "applied"


@pytest.mark.asyncio
async def test_status_push_keeps_obligation_when_revision_did_not_advance(
    tmp_path, monkeypatch
):
    """A target-status response at the baseline revision proves no action."""
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-stale-revision-confirmation",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/stale-revision-confirmation",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")
    orchestrator._cloud_request = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "status": "applied",
                "status_revision": 0,
                "deduplicated": True,
            },
        )
    )

    result = await orchestrator._push_status_to_cloud(job["job_id"], "applied")

    assert result is False
    marker = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert marker["expected_revision"] == 0


@pytest.mark.asyncio
async def test_revision_conflict_rebases_exact_pending_generation(
    tmp_path, monkeypatch
):
    """A lost predecessor response must not strand the newer transition."""
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-rebase",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/rebase",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")
    orchestrator.state.set_status(
        job["job_id"], "skipped", queue_cloud_sync=True
    )
    initial_marker = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    conflict = MagicMock(
        status_code=409,
        json=lambda: {
            "detail": {
                "current_status": "applied",
                "current_revision": 1,
            }
        },
    )
    confirmed = MagicMock(
        status_code=200,
        json=lambda: {
            "status": "skipped",
            "status_revision": 2,
            "deduplicated": False,
        },
    )
    orchestrator._cloud_request = AsyncMock(side_effect=[conflict, confirmed])

    first = await orchestrator._push_status_to_cloud(job["job_id"], "skipped")

    assert first is False
    rebased = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert rebased["generation"] != initial_marker["generation"]
    assert rebased["expected_status"] == "applied"
    assert rebased["expected_revision"] == 1

    await orchestrator._retry_pending_cloud_status_sync()

    retry_payload = orchestrator._cloud_request.await_args_list[1].kwargs["json"]
    assert retry_payload["expected_status"] == "applied"
    assert retry_payload["expected_revision"] == 1
    assert "cloud_status_sync_pending" not in parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )


def test_later_local_status_clears_pending_applied_sync(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-reclassified",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/reclassified",
        "status": "approved",
    }
    state.upsert_job(job)
    state.set_status(job["job_id"], "applied")

    state.set_status(job["job_id"], "skipped")

    current = state.get_job(job["job_id"])
    assert current["status"] == "skipped"
    assert "cloud_status_sync_pending" not in parse_extra_json(
        current["extra_json"]
    )


@pytest.mark.asyncio
async def test_retry_drops_legacy_marker_when_local_status_has_changed(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-legacy-stale-marker",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/legacy-stale-marker",
        "status": "skipped",
        "extra_json": json.dumps(
            {"cloud_status_sync_pending": {"status": "applied"}}
        ),
    }
    orchestrator.state.upsert_job(job)
    orchestrator._cloud_request = AsyncMock()

    await orchestrator._retry_pending_cloud_status_sync()

    orchestrator._cloud_request.assert_not_awaited()
    current = orchestrator.state.get_job(job["job_id"])
    assert "cloud_status_sync_pending" not in parse_extra_json(
        current["extra_json"]
    )


@pytest.mark.asyncio
async def test_old_retry_cannot_clear_new_applied_generation(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-generation-race",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/generation-race",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")
    old_marker = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]

    async def _concurrent_new_generation(*_args, **_kwargs):
        orchestrator.state.set_status(job["job_id"], "skipped")
        orchestrator.state.set_status(job["job_id"], "applied")
        return MagicMock(
            status_code=200,
            json=lambda: {
                "status": "applied",
                "status_revision": 1,
                "deduplicated": False,
            },
        )

    orchestrator._cloud_request = AsyncMock(side_effect=_concurrent_new_generation)

    await orchestrator._retry_pending_cloud_status_sync()

    current_marker = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert current_marker["generation"] != old_marker["generation"]
    assert current_marker["expected_status"] == "applied"
    assert current_marker["expected_revision"] == 1


@pytest.mark.asyncio
async def test_new_applied_generation_uses_a_new_cloud_action_key(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-new-generation-key",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/new-generation-key",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator._cloud_request = AsyncMock(return_value=MagicMock(status_code=503))

    orchestrator.state.set_status(job["job_id"], "applied")
    await orchestrator._push_status_to_cloud(job["job_id"], "applied")
    first_key = orchestrator._cloud_request.await_args.kwargs["json"][
        "idempotency_key"
    ]
    orchestrator.state.set_status(job["job_id"], "skipped")
    orchestrator.state.set_status(job["job_id"], "applied")
    await orchestrator._push_status_to_cloud(job["job_id"], "applied")
    second_key = orchestrator._cloud_request.await_args.kwargs["json"][
        "idempotency_key"
    ]

    assert second_key != first_key


@pytest.mark.asyncio
async def test_markerless_status_push_is_rejected_without_network(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-markerless",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/markerless",
        "status": "skipped",
    }
    orchestrator.state.upsert_job(job)
    orchestrator._cloud_request = AsyncMock()

    result = await orchestrator._push_status_to_cloud(job["job_id"], "skipped")

    assert result is False
    orchestrator._cloud_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_marker_without_generation_is_rejected_without_network(
    tmp_path, monkeypatch
):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-legacy-generation",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/legacy-generation",
        "status": "applied",
        "extra_json": json.dumps(
            {
                "cloud_status_sync_pending": {
                    "status": "applied",
                    "expected_status": "approved",
                    "expected_revision": 0,
                    "queued_at": "2026-09-23T00:00:00",
                }
            }
        ),
    }
    orchestrator.state.upsert_job(job)
    orchestrator._cloud_request = AsyncMock()

    result = await orchestrator._push_status_to_cloud(job["job_id"], "applied")

    assert result is False
    orchestrator._cloud_request.assert_not_awaited()


def test_requested_skipped_sync_gets_generation_and_expected_status(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-skipped-sync",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/skipped-sync",
        "status": "approved",
    }
    state.upsert_job(job)

    state.set_status(job["job_id"], "skipped", queue_cloud_sync=True)

    marker = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert marker["status"] == "skipped"
    assert marker["expected_status"] == "approved"
    assert marker["expected_revision"] == 0
    assert marker["generation"]


def test_authoritative_refresh_assigns_generation_to_legacy_marker(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-legacy-authoritative-refresh",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/legacy-authoritative-refresh",
        "status": "applied",
        "extra_json": json.dumps(
            {
                "cloud_status_sync_pending": {
                    "status": "applied",
                    "expected_status": "approved",
                    "expected_revision": 0,
                    "queued_at": "2026-09-23T00:00:00",
                }
            }
        ),
    }
    state.upsert_job(job)

    state.set_status(
        job["job_id"],
        "applied",
        expected_cloud_status="approved",
        expected_cloud_revision=4,
    )

    marker = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert marker["expected_status"] == "approved"
    assert marker["expected_revision"] == 4
    assert marker["generation"]


def test_replacing_pending_marker_preserves_cloud_baseline(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-transition-chain",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/transition-chain",
        "status": "approved",
    }
    state.upsert_job(job)
    state.set_status(job["job_id"], "applied")
    first = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]

    state.set_status(job["job_id"], "skipped", queue_cloud_sync=True)

    replacement = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert replacement["generation"] != first["generation"]
    assert replacement["expected_status"] == "approved"
    assert replacement["expected_revision"] == 0


def test_requested_expiry_sync_replaces_prior_applied_generation(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-expired-sync",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/expired-sync",
        "status": "approved",
    }
    state.upsert_job(job)
    state.set_status(job["job_id"], "applied")
    applied_marker = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]

    state.mark_expired(
        job["job_id"],
        reason="posting removed",
        signal="probe",
        queue_cloud_sync=True,
    )

    marker = parse_extra_json(
        state.get_job(job["job_id"])["extra_json"]
    )["cloud_status_sync_pending"]
    assert marker["status"] == "expired"
    assert marker["expected_status"] == "approved"
    assert marker["expected_revision"] == 0
    assert marker["generation"] != applied_marker["generation"]


def test_expiry_clears_pending_applied_sync(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-expired-after-applied",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/expired-after-applied",
        "status": "approved",
    }
    state.upsert_job(job)
    state.set_status(job["job_id"], "applied")

    state.mark_expired(job["job_id"], reason="posting removed", signal="probe")

    current = state.get_job(job["job_id"])
    assert current["status"] == "expired"
    assert "cloud_status_sync_pending" not in parse_extra_json(
        current["extra_json"]
    )


def test_archive_clears_pending_applied_sync(tmp_path):
    from src.state_manager import StateManager, parse_extra_json

    state = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job-archived-after-applied",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/archived-after-applied",
        "status": "approved",
    }
    state.upsert_job(job)
    state.set_status(job["job_id"], "applied")

    archived = state.archive_job(job["job_id"], reason="posting removed")

    assert archived is not None
    assert "cloud_status_sync_pending" not in parse_extra_json(
        archived["extra_json"]
    )


@pytest.mark.asyncio
async def test_pending_applied_sync_survives_missing_dashboard_url(
    tmp_path, monkeypatch
):
    """No dashboard configuration is not confirmation of cloud state."""
    from src.orchestrator import Orchestrator
    from src.state_manager import parse_extra_json

    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    job = {
        "job_id": "job-offline",
        "source": "jobright",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/offline",
        "status": "approved",
    }
    orchestrator.state.upsert_job(job)
    orchestrator.state.set_status(job["job_id"], "applied")

    result = await orchestrator._push_status_to_cloud(job["job_id"], "applied")

    assert result is False
    pending = parse_extra_json(
        orchestrator.state.get_job(job["job_id"])["extra_json"]
    )
    assert pending["cloud_status_sync_pending"]["status"] == "applied"


@pytest.mark.asyncio
async def test_action_timeout_is_attempted_exactly_once(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("SYNC_SECRET", "test-secret")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout(""))

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"state_db_path": str(tmp_path / "jobs.db")})
    )
    orchestrator = Orchestrator(config_path=str(config_path))
    orchestrator.state.upsert_job(
        {
            "job_id": "job-1",
            "source": "jobright",
            "title": "Engineer",
            "company": "Acme",
            "url": "https://example.com/jobs/1",
            "status": "approved",
        }
    )
    orchestrator.state.mark_expired(
        "job-1",
        reason="posting removed",
        queue_cloud_sync=True,
    )
    with patch("httpx.AsyncClient", return_value=_client_context(client)):
        await orchestrator._push_status_to_cloud("job-1", "expired")

    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_pull_http_failure_records_boundary_without_retry_or_body_leak(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("SYNC_SECRET", "test-secret")
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(
        503,
        request=httpx.Request("GET", "https://dashboard.example/api/jobs/approved"),
        text='{"error":"token=super-secret"}',
    ))

    orchestrator = Orchestrator(config_path=str(tmp_path / "missing.json"))
    emit = MagicMock()
    monkeypatch.setattr(orchestrator.run_log, "emit", emit)
    notify = MagicMock()
    monkeypatch.setattr("src.orchestrator.notify_error", notify)

    with patch("httpx.AsyncClient", return_value=_client_context(client)):
        await orchestrator._pull_approved_from_cloud()

    client.get.assert_awaited_once()
    emit.assert_called_once_with(
        "boundary_failure",
        operation="cloud_pull_approved",
        endpoint_class="dashboard_read",
        kind="http_status",
        message="http 503",
        status_code=503,
        retryable_transport=False,
        attempt=1,
    )
    notify.assert_called_once_with("Cloud sync failed: cloud_pull_approved", "http 503")


@pytest.mark.asyncio
async def test_sync_http_failure_does_not_retry_state_changing_write(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("SYNC_SECRET", "test-secret")
    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(
        429,
        request=httpx.Request("POST", "https://dashboard.example/api/sync"),
        text='{"detail":"authorization=******"}',
    ))

    orchestrator = Orchestrator(config_path=str(tmp_path / "missing.json"))
    emit = MagicMock()
    monkeypatch.setattr(orchestrator.run_log, "emit", emit)
    notify = MagicMock()
    monkeypatch.setattr("src.orchestrator.notify_error", notify)

    with patch("httpx.AsyncClient", return_value=_client_context(client)):
        await orchestrator._sync_to_cloud([{"job_id": "job-1", "title": "Engineer"}])

    client.post.assert_awaited_once()
    emit.assert_called_once_with(
        "boundary_failure",
        operation="cloud_sync_jobs",
        endpoint_class="dashboard_sync",
        kind="http_status",
        message="http 429",
        status_code=429,
        retryable_transport=False,
        attempt=1,
    )
    notify.assert_called_once_with("Cloud sync failed: cloud_sync_jobs", "http 429")
