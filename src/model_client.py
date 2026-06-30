"""
ModelClient — Ollama-first, Claude-fallback, OpenAI-last model routing for job-agent.

Mirrors the routing logic in AI Command Center Desktop App
(electron/services/modelService.js) so both systems share the same
local-model preference and Claude-escalation behaviour.

Priority:
  1. Ollama (http://127.0.0.1:11434) — auto-detect installed models,
     pick the best fit for the task type (coding vs reasoning).
  2. Claude via Anthropic API — only if Ollama is unavailable, times
     out, or returns an empty / clearly invalid response.
  3. OpenAI / Codex — final fallback when both Ollama and Claude are unavailable.

Usage:
    client = ModelClient()
    response = await client.complete(
        messages=[{"role": "user", "content": "..."}],
        system="You are ...",
        task_type="reasoning",   # "coding" | "reasoning" | "general"
    )
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

try:
    from .telemetry import model_span as _model_span
except Exception:
    import contextlib

    @contextlib.contextmanager
    def _model_span(*args, **kwargs):
        yield {}

try:
    from .notifier import notify_error as _notify_error
except Exception:
    _notify_error = None  # type: ignore[assignment]


_log = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
DEFAULT_ANTHROPIC_MODEL = os.environ.get("COMMANDER_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.environ.get("COMMANDER_OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MAX_TOKENS = 2048

# Model preference order per task type — first match in the pulled list wins.
# Mirrors AI Commander's selectModelForTask() preference ordering.
# deepseek-r1 is listed first for reasoning — strong chain-of-thought locally.
# devstral is Mistral's coding specialist; qwen3-coder is the large coding model.
MODEL_PREFERENCES: dict[str, list[str]] = {
    "coding": [
        "devstral", "qwen2.5-coder", "qwen3-coder", "codellama", "deepseek-coder",
        "codegemma", "starcoder2", "llama3", "mistral", "phi3",
    ],
    "reasoning": [
        "deepseek-r1", "llama3.1", "llama3", "llama3.2", "mistral", "mixtral",
        "qwen3", "phi3", "gemma4", "gemma2", "gemma3", "qwen2.5",
    ],
    "general": [
        "llama3.1", "llama3", "mistral", "qwen3", "phi3",
        "gemma4", "gemma2", "qwen2.5-coder", "codellama", "devstral",
    ],
}


class ModelClient:
    """Routes completions through Ollama first, Claude second."""

    def __init__(
        self,
        ollama_base_url: str = OLLAMA_BASE_URL,
        anthropic_api_key: str = "",
        anthropic_model: str = "",
    ):
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self._api_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._anthropic_model = anthropic_model or DEFAULT_ANTHROPIC_MODEL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        system: str = "",
        task_type: str = "general",
        max_tokens: int = ANTHROPIC_MAX_TOKENS,
    ) -> str:
        """
        Return the model's response text.
        Cascade: Ollama → Claude → OpenAI (Codex). Each tier is instrumented
        with model_span() so latency and token usage appear in Loki/Grafana.
        """
        # Tier 1: Ollama
        ollama_model = await self._pick_ollama_model(task_type)
        if ollama_model:
            try:
                with _model_span("ollama", ollama_model):
                    text = await self._call_ollama(ollama_model, messages, system, max_tokens)
                if text and text.strip():
                    _log.info("ModelClient: used Ollama model %s", ollama_model)
                    return text
                _log.warning("ModelClient: Ollama returned empty — escalating to Claude")
            except Exception as exc:
                _log.warning("ModelClient: Ollama failed (%s) — escalating to Claude", exc)
        else:
            _log.info("ModelClient: no Ollama models available — trying Claude")

        # Tier 2: Claude
        if self._api_key:
            try:
                with _model_span("anthropic", self._anthropic_model):
                    text = await self._call_claude(messages, system, max_tokens)
                if text and text.strip():
                    return text
                _log.warning("ModelClient: Claude returned empty — escalating to OpenAI")
            except Exception as exc:
                _log.warning("ModelClient: Claude failed (%s) — escalating to OpenAI", exc)
        else:
            _log.info("ModelClient: no ANTHROPIC_API_KEY — skipping Claude, trying OpenAI")

        # Tier 3: OpenAI / Codex
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        last_error: str = ""
        if openai_key:
            try:
                with _model_span("openai", OPENAI_MODEL):
                    return await self._call_openai(messages, system, max_tokens)
            except Exception as exc:
                last_error = str(exc)
                _log.error("ModelClient: OpenAI also failed: %s", exc)

        # All three tiers exhausted — emit a visible alert before returning the
        # degraded fallback string so the failure surfaces in agent_status.json
        # and in Loki via the structured log entry.
        _degraded_msg = (
            "No model available: Ollama is not running, ANTHROPIC_API_KEY and "
            "OPENAI_API_KEY are not set."
        )
        _log.error(
            "ModelClient: cascade total failure — all inference tiers failed. last_error=%s",
            last_error,
            extra={"tags": {"level": "error", "alert": "true"}},
        )
        if _notify_error is not None:
            _notify_error(
                "Model cascade total failure",
                f"All inference tiers failed (Ollama + Claude + OpenAI). "
                f"Scoring degraded. Last error: {last_error}",
            )
        return _degraded_msg

    async def get_ollama_models(self) -> list[str]:
        """Return names of all locally pulled Ollama models."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.ollama_base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception as exc:
            _log.debug("ModelClient: cannot reach Ollama: %s", exc)
            return []

    async def is_ollama_available(self) -> bool:
        """Return True if Ollama is running and reachable."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self.ollama_base_url}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _pick_ollama_model(self, task_type: str) -> str | None:
        """Return the best available local model for task_type, or None."""
        pulled = await self.get_ollama_models()
        if not pulled:
            return None

        preferences = MODEL_PREFERENCES.get(task_type, MODEL_PREFERENCES["general"])
        pulled_lower = {m.lower(): m for m in pulled}

        for preferred in preferences:
            for pulled_name_lower, pulled_name in pulled_lower.items():
                if preferred.lower() in pulled_name_lower:
                    return pulled_name

        # No preference match — use whatever's installed
        return pulled[0]

    async def _call_ollama(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
    ) -> str:
        full_messages = list(messages)
        if system:
            full_messages = [{"role": "system", "content": system}] + full_messages

        # Adaptive context: use 8192 for coding/reasoning, 4096 for general
        num_ctx = 8192 if max_tokens > 1024 else 4096

        payload: dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "options": {
                "temperature": 0.2,
                "num_ctx": num_ctx,
            },
            "keep_alive": "10m",
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(
                f"{self.ollama_base_url}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")

    async def _call_claude(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
    ) -> str:
        import anthropic as _anthropic  # deferred — not required if Ollama works

        client = _anthropic.Anthropic(api_key=self._api_key)
        non_system = [m for m in messages if m.get("role") != "system"]
        response = client.messages.create(
            model=self._anthropic_model,
            max_tokens=max_tokens,
            system=system or "You are a helpful assistant.",
            messages=non_system,
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    async def _call_openai(
        self,
        messages: list[dict],
        system: str,
        max_tokens: int,
    ) -> str:
        import openai as _openai  # deferred — not required if earlier tiers succeed

        openai_key = os.environ.get("OPENAI_API_KEY", "")
        client = _openai.OpenAI(api_key=openai_key)
        full_messages = list(messages)
        if system:
            full_messages = [{"role": "system", "content": system}] + full_messages

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
