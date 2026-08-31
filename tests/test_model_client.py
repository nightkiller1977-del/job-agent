import asyncio
import json
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


@pytest.mark.asyncio
async def test_cascade_falls_through_to_openrouter_gateway(monkeypatch):
    """When Ollama fails, cascade routes to OpenRouter Gateway before direct cloud providers."""
    client = ModelClient()
    monkeypatch.setattr(client, "_pick_ollama_model", lambda task_type: asyncio.sleep(0, result=None))
    monkeypatch.setenv("AICC_OPENROUTER_API_KEY", "test-gateway-key")
    monkeypatch.setenv("OPENROUTER_GATEWAY_URL", "http://127.0.0.1:3848")

    openrouter_called = False
    claude_called = False

    async def mock_call_openrouter(*args, **kwargs):
        nonlocal openrouter_called
        openrouter_called = True
        return "openrouter response"

    async def mock_call_claude(*args, **kwargs):
        nonlocal claude_called
        claude_called = True
        return "claude response"

    monkeypatch.setattr(client, "_call_openrouter_gateway", mock_call_openrouter)
    monkeypatch.setattr(client, "_call_claude", mock_call_claude)

    res = await client.complete([{"role": "user", "content": "hi"}])
    assert res == "openrouter response"
    assert openrouter_called is True
    assert claude_called is False


@pytest.mark.asyncio
async def test_gateway_budget_denial_fails_closed(monkeypatch):
    """When OpenRouter Gateway returns 402 Budget Exceeded, cascade fails closed rather than bypassing budget."""
    from src.model_client import BudgetExceededError

    client = ModelClient(anthropic_api_key="sk-ant-test")
    monkeypatch.setattr(client, "_pick_ollama_model", lambda task_type: asyncio.sleep(0, result=None))
    monkeypatch.setenv("AICC_OPENROUTER_API_KEY", "test-gateway-key")

    claude_called = False

    async def mock_call_openrouter(*args, **kwargs):
        raise BudgetExceededError("Budget limit reached (HTTP 402)")

    async def mock_call_claude(*args, **kwargs):
        nonlocal claude_called
        claude_called = True
        return "claude response"

    monkeypatch.setattr(client, "_call_openrouter_gateway", mock_call_openrouter)
    monkeypatch.setattr(client, "_call_claude", mock_call_claude)

    with pytest.raises(BudgetExceededError):
        await client.complete([{"role": "user", "content": "hi"}])

    # Direct Claude must NEVER have been called when budget was denied
    assert claude_called is False


def test_check_inference_availability(monkeypatch):
    """check_inference_availability correctly identifies ready providers."""
    from src.model_client import check_inference_availability

    # Clear all keys
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AICC_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:99999")  # Unreachable

    # 1. None available
    avail, msg = check_inference_availability()
    assert avail is False
    assert "No inference provider available" in msg

    # 2. OpenRouter available
    monkeypatch.setenv("AICC_OPENROUTER_API_KEY", "aicc-token")
    avail, msg = check_inference_availability()
    assert avail is True
    assert msg == "AI-OpenRouter Gateway"

    # 3. Direct Anthropic available
    monkeypatch.delenv("AICC_OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    avail, msg = check_inference_availability()
    assert avail is True
    assert msg == "Direct Anthropic (Claude)"


@pytest.mark.asyncio
async def test_openrouter_gateway_parses_top_level_content_and_choices(monkeypatch):
    """Verifies that _call_openrouter_gateway parses native gateway {content: ...} and {choices: ...} contracts."""
    client = ModelClient()
    monkeypatch.setenv("AICC_OPENROUTER_API_KEY", "test-key")

    class MockResponse:
        def __init__(self, data, status_code=200):
            self._data = data
            self.status_code = status_code
            self.text = json.dumps(data)

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    # 1. Native AI-OpenRouter Gateway contract: { content: "...", model: "..." }
    async def mock_post_native(self, url, json, headers):
        return MockResponse({"content": "Grounded answer from AI-OpenRouter gateway"})

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post_native)
    res_native = await client._call_openrouter_gateway([{"role": "user", "content": "test"}], "", "general", 100)
    assert res_native == "Grounded answer from AI-OpenRouter gateway"

    # 2. OpenAI-compatible choices contract: { choices: [{ message: { content: "..." } }] }
    async def mock_post_choices(self, url, json, headers):
        return MockResponse({"choices": [{"message": {"content": "Answer from OpenAI style upstream"}}]})

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post_choices)
    res_choices = await client._call_openrouter_gateway([{"role": "user", "content": "test"}], "", "general", 100)
    assert res_choices == "Answer from OpenAI style upstream"


