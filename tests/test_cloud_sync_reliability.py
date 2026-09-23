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
        "status": "applied",
    }
    orchestrator.state.upsert_job(local_job)
    cloud_job = {**local_job, "status": "approved"}
    orchestrator._cloud_request = AsyncMock(
        return_value=MagicMock(status_code=200, json=lambda: [cloud_job])
    )

    await orchestrator._pull_approved_from_cloud()

    assert orchestrator.state.get_job("job-applied")["status"] == "applied"


@pytest.mark.asyncio
async def test_action_timeout_is_attempted_exactly_once(tmp_path, monkeypatch):
    from src.orchestrator import Orchestrator

    monkeypatch.setenv("DASHBOARD_URL", "https://dashboard.example")
    monkeypatch.setenv("SYNC_SECRET", "test-secret")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout(""))

    orchestrator = Orchestrator(config_path=str(tmp_path / "missing.json"))
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
