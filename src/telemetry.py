"""
Observability for the job agent and future AI agents.

Ships all Python logging to Loki (http://localhost:3100) so every agent's
events, model calls, errors, and latencies appear in Grafana automatically.

Usage:
    from src.telemetry import setup, model_span

    setup(agent="job-agent")          # once at startup

    async with model_span("anthropic", "claude-sonnet-4-6") as span:
        result = await client.messages.create(...)
        span["output_tokens"] = result.usage.output_tokens
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import time
from typing import Generator

import openlit

_LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

_setup_done = False

log = logging.getLogger("telemetry")


def setup(agent: str = "job-agent", environment: str = "production") -> None:
    """Wire Loki log handler + OpenLIT LLM auto-instrumentation. Safe to call multiple times."""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    _setup_loki_handler(agent, environment)
    _setup_openlit(agent, environment)
    log.info("Telemetry initialised", extra={"tags": {"agent": agent, "env": environment}})


def _setup_loki_handler(agent: str, environment: str) -> None:
    from urllib.parse import urlparse
    import socket

    # Quick TCP check to avoid spawning a Loki emitter thread when the server is offline
    try:
        parsed = urlparse(_LOKI_URL)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except Exception:
        print("[telemetry] Loki server offline — logging to console only")
        return

    try:
        import logging_loki
        logging_loki.emitter.LokiEmitter.level_tag = "level"
        handler = logging_loki.LokiHandler(
            url=_LOKI_URL,
            tags={
                "application": "ai-agents",
                "agent": agent,
                "environment": environment,
            },
            auth=None,
            version="1",
        )
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        if not any(isinstance(h, logging_loki.LokiHandler) for h in root.handlers):
            root.addHandler(handler)
        # Ensure root logger passes INFO+ to the handler (default is WARNING)
        if root.level == logging.WARNING or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        # Flush the background push thread on clean exit so short-lived
        # processes (smoke tests, quick commands) don't lose their last logs.
        atexit.register(lambda: time.sleep(1.5))
    except Exception as exc:
        print(f"[telemetry] Loki handler failed to init ({exc}) — logging to console only")


def _setup_openlit(agent: str, environment: str) -> None:
    if not _OTLP_ENDPOINT:
        # No OTel collector configured — skip OpenLIT to avoid noisy console
        # span output. Set OTEL_EXPORTER_OTLP_ENDPOINT in .env to enable
        # full distributed tracing (e.g. when Tempo is added later).
        return
    try:
        openlit.init(
            otlp_endpoint=_OTLP_ENDPOINT,
            application_name=agent,
            environment=environment,
            capture_message_content=False,
        )
    except Exception as exc:
        print(f"[telemetry] OpenLIT init failed ({exc}) — LLM auto-tracing off")


@contextlib.contextmanager
def model_span(
    provider: str,
    model: str,
    agent: str = "job-agent",
    **extra_labels: str,
) -> Generator[dict, None, None]:
    """
    Context manager that measures and logs every model call to Loki.

    Usage:
        async with model_span("anthropic", "claude-sonnet-4-6") as span:
            resp = await client.messages.create(...)
            span["input_tokens"]  = resp.usage.input_tokens
            span["output_tokens"] = resp.usage.output_tokens

    Loki labels: provider, model, agent, success
    Loki line:   JSON with latency_ms, tokens, error (if any)
    """
    _log = logging.getLogger("model.call")
    span: dict = {"provider": provider, "model": model, "agent": agent}
    t_start = time.perf_counter()
    error: str | None = None
    try:
        yield span
    except Exception as exc:
        error = type(exc).__name__
        span["error"] = str(exc)
        raise
    finally:
        latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
        record_extra: dict = {
            "tags": {
                "provider": provider,
                "model": model,
                "agent": agent,
                "success": "false" if error else "true",
                **extra_labels,
            }
        }
        msg = (
            f"model_call provider={provider} model={model} "
            f"latency_ms={latency_ms} success={'false' if error else 'true'}"
        )
        if error:
            msg += f" error={error}"
        for k, v in span.items():
            if k not in ("provider", "model", "agent", "error"):
                msg += f" {k}={v}"

        if error:
            _log.error(msg, extra=record_extra)
        else:
            _log.info(msg, extra=record_extra)
