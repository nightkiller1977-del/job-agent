"""Regression tests for model configuration.

The OpenRouter tier defaults previously pointed at retired Anthropic models
("anthropic/claude-3.5-sonnet" retired Oct 2025, "anthropic/claude-3.5-haiku"
retired Feb 2026), so tier 2 of the cascade failed with model-not-found on
every request.
"""
import importlib
import json

import httpx
import pytest

from src import model_client
from src.model_client import ModelClient, OPENROUTER_TASK_MODELS

# The four overrides OPENROUTER_TASK_MODELS reads at import time — cleared in
# test_reasoning_and_general_defaults_do_not_route_to_claude so the test
# exercises the literal hardcoded defaults, not whatever the ambient
# developer/CI environment happens to have exported.
_OVERRIDE_ENV_VARS = (
    "JOB_AGENT_OPENROUTER_REASONING_MODEL",
    "JOB_AGENT_OPENROUTER_GENERAL_MODEL",
    "JOB_AGENT_OPENROUTER_CODING_MODEL",
    "JOB_AGENT_OPENROUTER_MONITORING_MODEL",
)

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


def test_reasoning_and_general_defaults_do_not_route_to_claude(monkeypatch):
    """ACES-441: with Direct Claude (tier 3) unconfigured on most hosts,
    "reasoning"/"general" defaulting to an anthropic/* model was the only
    place a Claude call actually happened — just gateway-routed instead of
    direct. Pins the policy that OpenRouter's own defaults must not silently
    prefer Claude, independent of whether the model is still a live/retired
    one (test_openrouter_defaults_do_not_use_retired_models covers that).

    Reloads the module with the four JOB_AGENT_OPENROUTER_* overrides
    explicitly cleared first (Copilot review on PR #158): OPENROUTER_TASK_MODELS
    is built once at import time from os.environ, so asserting against the
    already-imported dict would fail for a developer/CI environment that has
    deliberately set e.g. JOB_AGENT_OPENROUTER_REASONING_MODEL=anthropic/... —
    a supported override this PR explicitly preserves, not a regression. This
    test is about the literal hardcoded default, not whatever's effectively
    active in the environment it happens to run in.
    """
    for var in _OVERRIDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    importlib.reload(model_client)
    try:
        for task in ("reasoning", "general"):
            model = model_client.OPENROUTER_TASK_MODELS[task]
            assert "claude" not in model.lower() and "anthropic" not in model.lower(), (
                f"OPENROUTER_TASK_MODELS[{task!r}] = {model!r} still routes to Claude"
            )
    finally:
        # monkeypatch restores the env vars on teardown, but this test's own
        # reload must not leave OTHER tests importing a module object built
        # from the artificially-cleared environment.
        importlib.reload(model_client)


@pytest.mark.asyncio
async def test_gateway_model_not_found_raises_clear_error(monkeypatch):
    """A 404/400 naming the model must surface a config-pointing error (and
    therefore escalate the cascade) instead of an opaque HTTPStatusError."""
    monkeypatch.setenv("AICC_OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_GATEWAY_URL", "http://127.0.0.1:3848")
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
