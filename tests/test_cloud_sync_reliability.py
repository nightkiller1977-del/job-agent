"""Cloud-sync transport tests: diagnostic retries never repeat state actions."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _client_context(client):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


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
