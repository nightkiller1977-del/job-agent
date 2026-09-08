"""Regression tests for model configuration.

The OpenRouter tier defaults previously pointed at retired Anthropic models
("anthropic/claude-3.5-sonnet" retired Oct 2025, "anthropic/claude-3.5-haiku"
retired Feb 2026), so tier 2 of the cascade failed with model-not-found on
every request.
"""
import json

import httpx
import pytest

from src import model_client
from src.model_client import ModelClient, OPENROUTER_TASK_MODELS

RETIRED_MODEL_FRAGMENTS = (
    "claude-3.5-sonnet",
    "claude-3-5-sonnet",
    "claude-3.5-haiku",
    "claude-3-5-haiku",
    "claude-3-sonnet",
    "claude-3-opus",
    "claude-2",
)


def test_openrouter_defaults_do_not_use_retired_models():
    for task, model in OPENROUTER_TASK_MODELS.items():
        for fragment in RETIRED_MODEL_FRAGMENTS:
            assert fragment not in model, (
                f"OPENROUTER_TASK_MODELS[{task!r}] = {model!r} references retired "
                f"model family {fragment!r}"
            )


def test_anthropic_defaults_do_not_use_retired_models():
    for name in (model_client.DEFAULT_ANTHROPIC_MODEL,):
        for fragment in RETIRED_MODEL_FRAGMENTS:
            assert fragment not in name


@pytest.mark.asyncio
async def test_gateway_model_not_found_raises_clear_error(monkeypatch):
    """A 404/400 naming the model must surface a config-pointing error (and
    therefore escalate the cascade) instead of an opaque HTTPStatusError."""
    client = ModelClient()

    async def fake_post(self, url, json=None, headers=None):
        request = httpx.Request("POST", url)
        return httpx.Response(
            404,
            text=json_dumps({"error": {"message": "model not found: anthropic/claude-3.5-sonnet"}}),
            request=request,
        )

    def json_dumps(obj):
        return json.dumps(obj)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(RuntimeError) as exc_info:
        await client._call_openrouter_gateway(
            messages=[{"role": "user", "content": "hi"}],
            system="",
            task_type="reasoning",
            max_tokens=16,
        )
    msg = str(exc_info.value)
    assert "rejected model" in msg
    assert "JOB_AGENT_OPENROUTER" in msg
