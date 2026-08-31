"""
ModelClient — resource-aware model routing for job-agent.

Mirrors AI Command Center's model routing pipeline:
  - SystemPerformanceGate: battery / thermal / swap pressure checks (macOS)
  - MODEL_REGISTRY: single source of truth for model capabilities & RAM requirements
  - resolveModelForCapacity: downgrade automatically when RAM cap is exceeded
  - Cascade: Ollama (local, best-fit) → Claude (Anthropic) → OpenAI
  - Retries with exponential backoff on transient Ollama failures (3 attempts)
  - model_span() telemetry logging mirrors the Grafana panels in AI Commander

Reference: electron/services/modelRegistry.js, modelRouterService.js, modelService.js
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
from contextlib import contextmanager
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telemetry (optional — gracefully absent)
# ---------------------------------------------------------------------------
try:
    from .telemetry import model_span as _model_span
except Exception:
    @contextmanager
    def _model_span(*args, **kwargs):
        yield {}

try:
    from .notifier import notify_error as _notify_error
except Exception:
    _notify_error = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120"))
ANTHROPIC_MAX_TOKENS = 2048

# Mirrors AI Commander's default fallback model names
DEFAULT_ANTHROPIC_MODEL = os.environ.get("COMMANDER_ANTHROPIC_MODEL", "claude-haiku-4-5")
OPENAI_MODEL = os.environ.get("COMMANDER_OPENAI_MODEL", "gpt-4o-mini")

# ---------------------------------------------------------------------------
# MODEL_REGISTRY — mirrors electron/services/modelRegistry.js
# ---------------------------------------------------------------------------
MODEL_REGISTRY: dict[str, dict] = {
    "devstral": {
        "contextWindow": 131072,
        "minRamGb": 18,
        "bestFor": ["multifile", "coding"],
        "tier": "specialist",
        "cloudEquivalent": "gemini-2.0-flash",
    },
    "deepseek-r1:14b": {
        "contextWindow": 16384,
        "minRamGb": 10,
        "bestFor": ["reasoning"],
        "tier": "specialist",
        "cloudEquivalent": "claude-sonnet-4-5",
    },
    "qwen3-coder:30b": {
        "contextWindow": 32768,
        "minRamGb": 20,
        "bestFor": ["coding"],
        "tier": "specialist",
        "cloudEquivalent": None,
    },
    "qwen3-coder:14b": {
        "contextWindow": 32768,
        "minRamGb": 10,
        "bestFor": ["coding"],
        "tier": "specialist",
        "cloudEquivalent": None,
    },
    "qwen3-coder:7b": {
        "contextWindow": 32768,
        "minRamGb": 5,
        "bestFor": ["coding"],
        "tier": "specialist",
        "cloudEquivalent": None,
    },
    "qwen2.5-coder:7b": {
        "contextWindow": 32768,
        "minRamGb": 5,
        "bestFor": ["coding"],
        "tier": "specialist",
        "cloudEquivalent": None,
    },
    "llama3.1:8b": {
        "contextWindow": 8192,
        "minRamGb": 5,
        "bestFor": ["chat", "tools", "general"],
        "tier": "fast",
        "cloudEquivalent": None,
    },
    "llama3.1": {
        "contextWindow": 8192,
        "minRamGb": 5,
        "bestFor": ["chat", "tools", "general"],
        "tier": "fast",
        "cloudEquivalent": None,
    },
    "gemma2": {
        "contextWindow": 8192,
        "minRamGb": 7,
        "bestFor": ["chat", "monitoring", "summarization", "general"],
        "tier": "fast",
        "cloudEquivalent": "claude-haiku-4-5",
    },
}

# Task type → ordered preferred models (first available/fitting wins)
# Mirrors TASK_MODEL_PROFILES + selectModelForTask cascade in AI Commander
TASK_PREFERENCES: dict[str, list[str]] = {
    "coding": [
        "devstral",
        "qwen3-coder:30b",
        "qwen2.5-coder:7b",
        "qwen3-coder:14b",
        "qwen3-coder:7b",
        "deepseek-r1:14b",
        "llama3.1",
        "gemma2",
    ],
    "reasoning": [
        "deepseek-r1:14b",
        "devstral",
        "qwen3-coder:30b",
        "qwen2.5-coder:7b",
        "qwen3-coder:14b",
        "qwen3-coder:7b",
        "llama3.1",
        "gemma2",
    ],
    "general": [
        "llama3.1",
        "llama3.1:8b",
        "gemma2",
        "devstral",
        "qwen2.5-coder:7b",
        "qwen3-coder:30b",
    ],
    "monitoring": [
        "gemma2",
        "llama3.1",
        "llama3.1:8b",
    ],
}

# ---------------------------------------------------------------------------
# Helpers — mirrors modelRegistry.js canonicalizeModelName
# ---------------------------------------------------------------------------

def _canonicalize(name: str) -> str:
    """Strip :latest and quantization suffixes, lowercase. e.g. 'devstral:latest' -> 'devstral'."""
    if not name:
        return ""
    parts = name.lower().strip().split(":", 1)
    base = parts[0]
    tag = parts[1] if len(parts) > 1 else ""
    # Strip quantization tags like q4_k_m, q8_0
    import re
    tag = re.sub(r"-q\d+.*$", "", tag)
    return f"{base}:{tag}" if tag and tag != "latest" else base


def _get_registry_entry(model_name: str) -> dict:
    canonical = _canonicalize(model_name)
    if canonical in MODEL_REGISTRY:
        return MODEL_REGISTRY[canonical]
    base = canonical.split(":")[0]
    if base in MODEL_REGISTRY:
        return MODEL_REGISTRY[base]
    # Fuzzy family match
    if "qwen" in base and ("coder" in base or "code" in base):
        return MODEL_REGISTRY["qwen3-coder:14b"]
    if "deepseek" in base or "r1" in base:
        return MODEL_REGISTRY["deepseek-r1:14b"]
    if "llama" in base:
        return MODEL_REGISTRY["llama3.1:8b"]
    if "gemma" in base:
        return MODEL_REGISTRY["gemma2"]
    # Unknown — generic profile
    return {"contextWindow": 4096, "minRamGb": 8, "bestFor": ["chat"], "tier": "fast", "cloudEquivalent": None}


def _resolve_for_capacity(model_name: str, cap_gb: float) -> str:
    """Return model_name if it fits, else the largest same-purpose model that fits.

    Mirrors resolveModelForCapacity() in electron/services/modelRegistry.js.
    """
    entry = _get_registry_entry(model_name)
    if entry["minRamGb"] <= cap_gb:
        return model_name

    canonical = _canonicalize(model_name)
    same_purpose = [
        (name, e) for name, e in MODEL_REGISTRY.items()
        if name != canonical
        and any(tag in entry["bestFor"] for tag in e["bestFor"])
        and e["minRamGb"] <= cap_gb
    ]
    same_purpose.sort(key=lambda t: t[1]["minRamGb"], reverse=True)
    if same_purpose:
        return same_purpose[0][0]

    smallest_fit = [
        (name, e) for name, e in MODEL_REGISTRY.items()
        if e["minRamGb"] <= cap_gb
    ]
    smallest_fit.sort(key=lambda t: t[1]["minRamGb"])
    return smallest_fit[0][0] if smallest_fit else model_name

# ---------------------------------------------------------------------------
# SystemPerformanceGate — mirrors modelRouterService.js SystemPerformanceGate
# ---------------------------------------------------------------------------

class SystemPerformanceGate:
    """Check battery, thermal, and swap state to determine safe model size cap.

    macOS-only for battery/thermal checks (matches AI Commander behaviour).
    On Linux/Windows, returns full available memory without eco restrictions.
    """

    @staticmethod
    def _run(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(cmd, timeout=2, stderr=subprocess.DEVNULL).decode()
        except Exception:
            return ""

    @classmethod
    def is_on_battery(cls) -> bool:
        if platform.system() != "Darwin":
            return False
        out = cls._run(["pmset", "-g", "batt"])
        return "Currently drawing from 'Battery Power'" in out

    @classmethod
    def get_thermal_level(cls) -> int:
        if platform.system() != "Darwin":
            return 0
        out = cls._run(["sysctl", "-n", "kern.thermal_level"])
        try:
            return int(out.strip())
        except ValueError:
            return 0

    @classmethod
    def get_memory_status(cls) -> dict:
        import os as _os
        import re

        # Use psutil if available for accurate memory figures; otherwise use sysconf
        try:
            import psutil
            vm = psutil.virtual_memory()
            total_gb = vm.total / (1024 ** 3)
            free_gb = vm.available / (1024 ** 3)
        except ImportError:
            total_gb = _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
            free_gb = total_gb * 0.4  # conservative fallback

        swap_gb = 0.0
        if platform.system() == "Darwin":
            out = cls._run(["sysctl", "-n", "vm.swapusage"])
            m = re.search(r"used\s*=\s*([\d.]+)([KMGT])", out, re.IGNORECASE)
            if m:
                val, unit = float(m.group(1)), m.group(2).upper()
                swap_gb = val / (1024 if unit == "M" else 1)

        return {"total_gb": total_gb, "free_gb": free_gb, "swap_gb": swap_gb}

    @classmethod
    def get_optimal_cap(cls) -> dict:
        """Return memoryCapGb, ecoMode, and throttleReason.

        Mirrors SystemPerformanceGate.getOptimalModelSizeCap() in AI Commander.
        """
        on_battery = cls.is_on_battery()
        thermal = cls.get_thermal_level()
        mem = cls.get_memory_status()

        OS_HEADROOM_GB = 8 if platform.system() == "Darwin" else 4
        cap_gb = max(2.0, mem["total_gb"] - OS_HEADROOM_GB)

        eco_mode = False
        throttle_reason = None

        if on_battery:
            eco_mode = True
            throttle_reason = "battery"
            cap_gb = min(cap_gb, 8.0)
        elif thermal > 0:
            eco_mode = True
            throttle_reason = "thermal"
            cap_gb = min(cap_gb, 10.0)
        elif mem["swap_gb"] > 8:
            throttle_reason = "high_swap"
            cap_gb = max(2.0, min(cap_gb, mem["free_gb"] + 2.0))

        return {
            "cap_gb": cap_gb,
            "eco_mode": eco_mode,
            "throttle_reason": throttle_reason,
            "metrics": {**mem, "on_battery": on_battery, "thermal_level": thermal},
        }

# ---------------------------------------------------------------------------
# ModelClient
# ---------------------------------------------------------------------------

class ModelCascadeError(Exception):
    """Raised when all inference tiers in the cascade fail."""
    pass


class ModelClient:
    """Routes completions through Ollama first (resource-aware), Claude second, OpenAI last.

    Model selection mirrors AI Commander's cascade:
      1. Query Ollama for pulled + warm models
      2. Apply SystemPerformanceGate to get RAM cap
      3. For the preferred model list (by task_type), pick the first that:
         a) Is pulled locally
         b) Fits within RAM cap (resolveModelForCapacity)
      4. Call Ollama with 3-attempt retry + backoff (matches modelService.js)
      5. Escalate to Claude (Anthropic) on failure or empty response
      6. Escalate to OpenAI as final fallback
    """

    _semaphores: dict[str, asyncio.Semaphore] = {}

    @classmethod
    def reset_semaphores(cls) -> None:
        """Reset all cached concurrency semaphores."""
        cls._semaphores.clear()

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
        temperature: float | None = None,
    ) -> str:
        """Return the model response text.  Cascade: Ollama → Claude → OpenAI.

        Pass temperature to override the default (0.2) for all backends.
        """
        # Tier 1: Ollama with resource-aware model selection
        ollama_model = await self._pick_ollama_model(task_type)
        if ollama_model:
            try:
                with _model_span("ollama", ollama_model):
                    text = await self._call_ollama(ollama_model, messages, system, max_tokens, temperature=temperature)
                if text and text.strip():
                    _log.info("ModelClient: Ollama model=%s task=%s", ollama_model, task_type)
                    return text
                _log.warning("ModelClient: Ollama returned empty — escalating to Claude")
            except Exception as exc:
                _log.warning("ModelClient: Ollama failed (%s) — escalating to Claude", exc)
        else:
            _log.info("ModelClient: no Ollama models fit constraints — trying Claude")

        # Tier 2: Claude
        if self._api_key:
            try:
                with _model_span("anthropic", self._anthropic_model):
                    text = await self._call_claude(messages, system, max_tokens, temperature=temperature)
                if text and text.strip():
                    _log.info("ModelClient: Claude model=%s task=%s", self._anthropic_model, task_type)
                    return text
                _log.warning("ModelClient: Claude returned empty — escalating to OpenAI")
            except Exception as exc:
                _log.warning("ModelClient: Claude failed (%s) — escalating to OpenAI", exc)
        else:
            _log.info("ModelClient: no ANTHROPIC_API_KEY — skipping Claude")

        # Tier 3: OpenAI
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        last_error = ""
        if openai_key:
            try:
                with _model_span("openai", OPENAI_MODEL):
                    text = await self._call_openai(messages, system, max_tokens, temperature=temperature)
                _log.info("ModelClient: OpenAI model=%s task=%s", OPENAI_MODEL, task_type)
                return text
            except Exception as exc:
                last_error = str(exc)
                _log.error("ModelClient: OpenAI also failed: %s", exc)

        # All tiers exhausted
        degraded = (
            "No model available: Ollama is not running or has no models that fit RAM constraints, "
            "ANTHROPIC_API_KEY and OPENAI_API_KEY are not set or failed."
        )
        _log.error(
            "ModelClient: all inference tiers failed last_error=%s", last_error,
            extra={"tags": {"level": "error", "alert": "true"}},
        )
        if _notify_error is not None:
            _notify_error(
                "Model cascade total failure",
                f"All tiers failed (Ollama + Claude + OpenAI). Scoring degraded. Last error: {last_error}",
            )
        raise ModelCascadeError(degraded)

    async def get_ollama_models(self) -> dict:
        """Return {'pulled': [...], 'warm': [...]} from Ollama /api/tags and /api/ps."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                tags_res, ps_res = await asyncio.gather(
                    client.get(f"{self.ollama_base_url}/api/tags"),
                    client.get(f"{self.ollama_base_url}/api/ps"),
                    return_exceptions=True,
                )
            pulled = [m["name"] for m in (tags_res.json().get("models", []) if not isinstance(tags_res, Exception) and tags_res.is_success else [])]
            warm = [m["name"] for m in (ps_res.json().get("models", []) if not isinstance(ps_res, Exception) and ps_res.is_success else [])]
            return {"pulled": pulled, "warm": warm}
        except Exception as exc:
            _log.debug("ModelClient: cannot reach Ollama: %s", exc)
            return {"pulled": [], "warm": []}

    async def is_ollama_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.ollama_base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Resource-aware model selection
    # ------------------------------------------------------------------

    async def _pick_ollama_model(self, task_type: str) -> str | None:
        """Pick the best available local model for task_type within RAM cap.

        1. Get performance cap (battery/thermal/swap)
        2. Get pulled models from Ollama
        3. Prefer warm (already loaded) models if they fit the task
        4. Walk preference list; for each, check pulled + resolve for capacity
        """
        ollama = await self.get_ollama_models()
        if not ollama["pulled"]:
            return None

        # Get resource constraints from SystemPerformanceGate
        try:
            gate = SystemPerformanceGate.get_optimal_cap()
        except Exception:
            gate = {"cap_gb": 16.0, "eco_mode": False, "throttle_reason": None, "metrics": {}}

        cap_gb = gate["cap_gb"]
        if gate["throttle_reason"]:
            _log.info(
                "ModelClient: resource cap %.1fGB reason=%s",
                cap_gb, gate["throttle_reason"],
            )

        pulled_canonical = {_canonicalize(m): m for m in ollama["pulled"]}
        warm_canonical = {_canonicalize(m) for m in ollama["warm"]}

        preferences = TASK_PREFERENCES.get(task_type, TASK_PREFERENCES["general"])

        # Prefer warm + task-matching models first (avoids cold-load latency)
        for preferred in preferences:
            canonical = _canonicalize(preferred)
            if canonical in warm_canonical and canonical in pulled_canonical:
                resolved = _resolve_for_capacity(preferred, cap_gb)
                resolved_canonical = _canonicalize(resolved)
                if resolved_canonical in pulled_canonical:
                    if resolved != preferred:
                        _log.info("ModelClient: downgraded %s → %s (cap=%.1fGB)", preferred, resolved, cap_gb)
                    return pulled_canonical[resolved_canonical]

        # No warm match — pick first preference that's pulled and fits
        for preferred in preferences:
            canonical = _canonicalize(preferred)
            if canonical in pulled_canonical:
                resolved = _resolve_for_capacity(preferred, cap_gb)
                resolved_canonical = _canonicalize(resolved)
                if resolved_canonical in pulled_canonical:
                    if resolved != preferred:
                        _log.info("ModelClient: downgraded %s → %s (cap=%.1fGB)", preferred, resolved, cap_gb)
                    return pulled_canonical[resolved_canonical]

        # No preference match — use whatever is pulled (first available)
        return ollama["pulled"][0]

    # ------------------------------------------------------------------
    # Ollama call — 3-attempt retry + backoff (mirrors modelService.js)
    # ------------------------------------------------------------------

    async def _call_ollama(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        sem_key = self.ollama_base_url

        # Lazily instantiate the semaphore for this scope
        if sem_key not in self._semaphores:
            try:
                max_concurrency = max(1, int(os.getenv("OLLAMA_MAX_CONCURRENCY", "1")))
            except (ValueError, TypeError):
                max_concurrency = 1
            self._semaphores[sem_key] = asyncio.Semaphore(max_concurrency)

        sem = self._semaphores[sem_key]
        async with sem:
            full_messages = list(messages)
            if system:
                full_messages = [{"role": "system", "content": system}] + full_messages

            # Adaptive context sizing: mirrors TASK_CTX_DEFAULTS in modelService.js
            num_ctx = 8192 if max_tokens > 1024 else 4096
            total_chars = sum(len(str(m.get("content", ""))) for m in full_messages)
            estimated_tokens = total_chars // 3
            if estimated_tokens > num_ctx:
                num_ctx = min(32768, ((estimated_tokens // 4096) + 1) * 4096)

            payload: dict[str, Any] = {
                "model": model,
                "messages": full_messages,
                "options": {"temperature": temperature if temperature is not None else 0.2, "num_ctx": num_ctx},
                "keep_alive": "10m",
                "stream": False,
            }

            max_attempts = 3
            backoff_s = 1.5
            last_exc: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                        resp = await client.post(f"{self.ollama_base_url}/api/chat", json=payload)
                        resp.raise_for_status()
                        data = resp.json()
                        return data.get("message", {}).get("content", "")
                except Exception as exc:
                    last_exc = exc
                    if "404" in str(exc) or "Model not found" in str(exc):
                        raise
                    if attempt < max_attempts:
                        _log.warning("ModelClient: Ollama attempt %d failed (%s), retrying…", attempt, exc)
                        await asyncio.sleep(backoff_s)

            raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Claude (Anthropic)
    # ------------------------------------------------------------------

    async def _call_claude(self, messages: list[dict], system: str, max_tokens: int, temperature: float | None = None) -> str:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=self._api_key)
        non_system = [m for m in messages if m.get("role") != "system"]
        kwargs: dict = dict(
            model=self._anthropic_model,
            max_tokens=max_tokens,
            system=system or "You are a helpful assistant.",
            messages=non_system,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = client.messages.create(**kwargs)
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    # ------------------------------------------------------------------
    # OpenAI
    # ------------------------------------------------------------------

    async def _call_openai(self, messages: list[dict], system: str, max_tokens: int, temperature: float | None = None) -> str:
        import openai as _openai
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        client = _openai.OpenAI(api_key=openai_key)
        full_messages = list(messages)
        if system:
            full_messages = [{"role": "system", "content": system}] + full_messages
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=full_messages,
            max_tokens=max_tokens,
            temperature=temperature if temperature is not None else 0.2,
        )
        return response.choices[0].message.content or ""
