"""Safe, bounded descriptions of failures at operational boundaries.

This module classifies a caught exception for logs and run-journal events.  It
does not authorize a retry: callers must also supply their operation's
idempotency and state-transition policy to :func:`is_retry_authorized`.
"""
from __future__ import annotations

import re
import ssl
import socket
from collections.abc import Iterator


OPERATIONS = frozenset({
    "cloud_pull_approved",
    "cloud_sync_jobs",
    "cloud_action",
    "desktop_notification",
})
ENDPOINT_CLASSES = frozenset({
    "dashboard_read",
    "dashboard_sync",
    "dashboard_action",
    "local_notification",
})
_RETRYABLE_TRANSPORT_KINDS = frozenset({"timeout", "dns", "connect"})
_MAX_MESSAGE = 300
_URL = re.compile(r"\bhttps?://[^\s\]\[\"']+", re.IGNORECASE)
_QUERY = re.compile(
    r"(?<!\w)[\"']?(?:[\w.-]*?(?:token|secret|password)[\w.-]*|[\w.-]*[_-]key|key|authorization)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\])]+)",
    re.IGNORECASE,
)
_AUTH_HEADER = re.compile(r"\bauthorization\s*:\s*(?:basic|bearer)\s+[^\s,;]+", re.IGNORECASE)
_SECRET_WORD = re.compile(r"\b[\w.-]*(?:secret|token|password)[\w.-]*\b", re.IGNORECASE)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Yield each unique explicit/context cause without looping."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _kind(exc: BaseException) -> str:
    saw_connect = False
    for item in _exception_chain(exc):
        if isinstance(item, (TimeoutError,)) or item.__class__.__name__ in {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}:
            return "timeout"
        if isinstance(item, socket.gaierror):
            return "dns"
        if isinstance(item, ssl.SSLError) or (item.__class__.__name__ in {"SSLError", "ConnectError"} and "ssl" in str(item).lower()):
            return "tls"
        if isinstance(item, (ConnectionError, ConnectionRefusedError)) or item.__class__.__name__ in {"ConnectError", "NetworkError"}:
            saw_connect = True
    return "connect" if saw_connect else "unknown"


def _safe_message(exc: BaseException, kind: str) -> str:
    pieces = [str(item).strip() for item in _exception_chain(exc) if str(item).strip()]
    if not pieces:
        return kind
    message = "; ".join(pieces)
    message = _URL.sub("[redacted-url]", message)
    message = _AUTH_HEADER.sub("[redacted]", message)
    message = _QUERY.sub("[redacted]", message)
    message = _SECRET_WORD.sub("[redacted]", message)
    return message[:_MAX_MESSAGE] or kind


def describe_failure(operation: str, endpoint_class: str, exc: BaseException) -> dict[str, object]:
    """Return an allowlisted, redacted failure record safe for durable events."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    if endpoint_class not in ENDPOINT_CLASSES:
        raise ValueError(f"unknown endpoint class: {endpoint_class}")
    kind = _kind(exc)
    return {
        "operation": operation,
        "endpoint_class": endpoint_class,
        "kind": kind,
        "message": _safe_message(exc, kind),
        "retryable_transport": kind in _RETRYABLE_TRANSPORT_KINDS,
    }


def describe_http_failure(operation: str, endpoint_class: str, status_code: int) -> dict[str, object]:
    """Return a safe failure record for a non-success HTTP response."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    if endpoint_class not in ENDPOINT_CLASSES:
        raise ValueError(f"unknown endpoint class: {endpoint_class}")
    return {
        "operation": operation,
        "endpoint_class": endpoint_class,
        "kind": "http_status",
        "message": f"http {status_code}",
        "status_code": int(status_code),
        "retryable_transport": False,
    }


def is_retry_authorized(
    operation: str,
    *,
    idempotent: bool,
    state_changing: bool,
    submission_state: str | None,
    attempts: int,
    max_attempts: int,
    breaker_allows: bool,
) -> bool:
    """Return whether a caller may repeat an operation after a transport error."""
    return (
        operation in {"cloud_pull_approved", "cloud_sync_jobs"}
        and idempotent
        and not state_changing
        and submission_state not in {"submission_unverified", "submit_in_progress"}
        and attempts < max_attempts
        and breaker_allows
    )
