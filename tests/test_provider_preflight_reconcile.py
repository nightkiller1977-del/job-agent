"""Regression coverage for provider-health behavior lost during main/main-rewrite reconciliation."""
import asyncio
import urllib.error

import pytest

import src.model_client as model_client
from src.model_client import ModelClient


class MockHttpResp:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return b'{"data": [{"id": "model"}]}'


def test_preflight_skips_401_anthropic_and_uses_openai_once_alerted(monkeypatch):
    """A definitively bad Anthropic key must not make preflight report healthy."""
    model_client.reset_provider_status()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "good-openai")
    monkeypatch.delenv("AICC_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_GATEWAY_URL", raising=False)

    alerts = []
    monkeypatch.setattr(model_client, "_notify_error", lambda title, body: alerts.append((title, body)))
    calls = {"anthropic": 0, "openai": 0}

    def fake_urlopen(req, timeout=2):
        url = req.full_url
        if "/api/tags" in url:
            raise ConnectionRefusedError("ollama offline")
        if "api.anthropic.com" in url:
            calls["anthropic"] += 1
            raise urllib.error.HTTPError(url, 401, "Unauthorized", hdrs=None, fp=None)
        if "api.openai.com" in url:
            calls["openai"] += 1
            return MockHttpResp(200)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert model_client.check_inference_availability() == (True, "Direct OpenAI")
    assert model_client.check_inference_availability() == (True, "Direct OpenAI")
    assert calls["anthropic"] == 1, "401'd provider should be cached unavailable for the run"
    assert calls["openai"] == 2
    assert len(alerts) == 1, "provider-unavailable alert should be emitted once"


@pytest.mark.asyncio
async def test_complete_skips_anthropic_after_first_401(monkeypatch):
    """Once Anthropic returns 401, later completions in the run skip it."""
    model_client.reset_provider_status()
    monkeypatch.delenv("AICC_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_GATEWAY_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "good-openai")
    monkeypatch.setattr(model_client, "_notify_error", lambda *_: None)

    client = ModelClient(anthropic_api_key="bad-anthropic")

    async def no_ollama(_task_type):
        return None

    class Auth401(Exception):
        status_code = 401

    claude_calls = 0
    openai_calls = 0

    async def bad_claude(*args, **kwargs):
        nonlocal claude_calls
        claude_calls += 1
        raise Auth401("bad key")

    async def good_openai(*args, **kwargs):
        nonlocal openai_calls
        openai_calls += 1
        return "openai fallback"

    monkeypatch.setattr(client, "_pick_ollama_model", no_ollama)
    monkeypatch.setattr(client, "_call_claude", bad_claude)
    monkeypatch.setattr(client, "_call_openai", good_openai)

    assert await client.complete([{"role": "user", "content": "one"}]) == "openai fallback"
    assert await client.complete([{"role": "user", "content": "two"}]) == "openai fallback"
    assert claude_calls == 1
    assert openai_calls == 2
