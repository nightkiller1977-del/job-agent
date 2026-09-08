"""
Observability for the job agent and future AI agents.

Ships all Python logging to Loki so every agent's events, model calls, errors,
and latencies appear in Grafana automatically. Local Loki (LOKI_URL, default
localhost) remains the default and is independent of remote export; Grafana
Cloud is selected only through the shared atomic contract resolved by
src/loki_config.resolve_loki_config(): LOKI_URL_REMOTE + LOKI_REMOTE_AUTH,
auto-on in production (RENDER), off in dev/test unless OBSERVABILITY_REMOTE=1.
The legacy LOKI_USER/LOKI_API_KEY split-key fallback is retired (ACES-293).
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import time
from typing import Generator

import openlit

from src.loki_config import basic_auth_credentials, resolve_loki_config

_setup_done = False
log = logging.getLogger("telemetry")


def resolve_loki_url() -> str:
    config = resolve_loki_config()
    if config.enabled:
        return config.url
    return os.environ.get("LOKI_URL") or "http://localhost:3100/loki/api/v1/push"


def resolve_loki_auth() -> tuple[str, str] | None:
    """Credentials only when the atomic remote pair is enabled and valid.

    Local Loki (LOKI_URL / localhost) is unauthenticated; a partial or invalid
    remote pair disables remote export instead of half-configuring it.
    """
    config = resolve_loki_config()
    if not config.enabled:
        return None
    return basic_auth_credentials(config.auth)


def setup(agent: str = "job-agent", environment: str = "production") -> None:
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

    loki_url = resolve_loki_url()
    try:
        parsed = urlparse(loki_url)
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
            url=loki_url,
            tags={
                "application": "ai-agents",
                "agent": agent,
                "environment": environment,
            },
            auth=resolve_loki_auth(),
            version="1",
        )
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        if not any(isinstance(h, logging_loki.LokiHandler) for h in root.handlers):
            root.addHandler(handler)
        if root.level in (logging.WARNING, logging.NOTSET):
            root.setLevel(logging.INFO)
        atexit.register(lambda: time.sleep(1.5))
    except Exception as exc:
        print(f"[telemetry] Loki handler failed to init ({exc}) — logging to console only")


def _setup_openlit(agent: str, environment: str) -> None:
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not otlp_endpoint:
        return
    try:
        openlit.init(
            otlp_endpoint=otlp_endpoint,
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
        (_log.error if error else _log.info)(msg, extra=record_extra)
