import asyncio
import time

import pytest

from src.model_client import ModelClient


@pytest.mark.asyncio
async def test_ollama_calls_to_same_endpoint_serialize(monkeypatch):
    ModelClient.reset_semaphores()
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "1")
    active = 0
    max_active = 0

    async def fake_post(self, url, json):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": "ok"}}

        return Response()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    client = ModelClient(ollama_base_url="http://same-endpoint")

    start = time.perf_counter()
    result = await asyncio.gather(
        client._call_ollama("model-a", [{"role": "user", "content": "a"}], "", 100),
        client._call_ollama("model-b", [{"role": "user", "content": "b"}], "", 100),
    )

    assert result == ["ok", "ok"]
    assert max_active == 1
    assert time.perf_counter() - start >= 0.035


def test_reset_semaphores_clears_cached_instances(monkeypatch):
    ModelClient.reset_semaphores()
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "1")
    client = ModelClient(ollama_base_url="http://endpoint")
    ModelClient._semaphores[client.ollama_base_url] = object()

    assert ModelClient._semaphores
    ModelClient.reset_semaphores()
    assert ModelClient._semaphores == {}
