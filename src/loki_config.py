"""Atomic Grafana Loki remote-export configuration (ACES-293 fleet contract).

One resolver owns destination + credential validation for every Loki path in
this repository (``src/telemetry.py`` and ``dashboard/observability.py``).
Stdlib-only on purpose: the Dashboard deploys with ``rootDir: dashboard`` and
must be able to load this module without the CLI's dependencies.

Contract (mirrors the email-agent/Node and Go implementations):

* ``LOKI_URL_REMOTE`` + ``LOKI_REMOTE_AUTH`` are an **atomic pair**. Remote
  export is enabled only when both are present from the process environment
  (a consistent source — ``src/secret_store.py`` fills the pair atomically
  from the central store and never mixes an env URL with a store credential)
  and both are valid. A one-sided pair disables export with a single warning.
  Values are never logged.
* ``LOKI_REMOTE_AUTH`` must be ``Basic <base64(user:password)>`` with a
  non-empty user and password and no CR/LF/control characters. Anything else
  disables export up front — it must never start an export worker that loops
  over failed sends.
* Default policy: remote export auto-enables in production and is off in
  dev/test unless ``OBSERVABILITY_REMOTE=1``. ``OBSERVABILITY_REMOTE=0`` opts
  out even in production. Production is detected via the ``RENDER`` env var,
  which Render sets on every service it runs (this repo's ``render.yaml``
  defines no explicit mode variable). Local Loki logging (``LOKI_URL``) is
  independent of this policy and unchanged.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

_log = logging.getLogger("loki-config")

# Reasons already warned about, so steady-state emit() polling logs once, not
# per event. Never contains secret values, only reason codes.
_warned: set[str] = set()


@dataclass(frozen=True)
class LokiConfig:
    """Validated, atomic remote-export destination."""
    enabled: bool
    url: str | None = None
    auth: str | None = None
    source: str | None = None  # authority the pair came from ("env")
    reason: str | None = None  # why export is disabled (never a value)


def validate_basic_auth(value: str) -> bool:
    """True only for ``Basic <base64>`` decoding to non-empty ``user:pass``.

    Rejects CR/LF and other control characters (header injection), non-Basic
    schemes, invalid base64, and empty user or password.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    scheme, _, payload = value.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return False
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return False
    user, sep, password = decoded.partition(":")
    return bool(sep) and bool(user) and bool(password)


def basic_auth_credentials(value: str) -> tuple[str, str] | None:
    """Decode a *validated* Basic header into ``(user, password)``."""
    if not validate_basic_auth(value):
        return None
    decoded = base64.b64decode(value.split(" ", 1)[1], validate=True).decode("utf-8")
    user, _, password = decoded.partition(":")
    return (user, password)


def _valid_push_url(url: str) -> bool:
    try:
        target = urlsplit(url)
    except ValueError:
        return False
    return bool(
        target.scheme == "https" and target.hostname
        and not target.username and not target.password
        and not target.query and not target.fragment
    )


def is_production(env: os._Environ | dict | None = None) -> bool:
    """Render sets ``RENDER`` on every service (documented in loki_config)."""
    env = os.environ if env is None else env
    return bool(env.get("RENDER"))


def _disabled(reason: str, *, warn: bool = True) -> LokiConfig:
    if warn and reason not in _warned:
        _warned.add(reason)
        _log.warning("loki remote export disabled: %s (values are never logged)", reason)
    return LokiConfig(enabled=False, reason=reason)


def resolve_loki_config(env: os._Environ | dict | None = None) -> LokiConfig:
    """Resolve the atomic remote pair + policy. Never raises, never logs values."""
    env = os.environ if env is None else env
    url = (env.get("LOKI_URL_REMOTE") or "").strip()
    auth = (env.get("LOKI_REMOTE_AUTH") or "").strip()

    opt = (env.get("OBSERVABILITY_REMOTE") or "").strip()
    if opt == "0":
        return _disabled("opted_out", warn=False)
    if opt != "1" and not is_production(env):
        return _disabled("non_production_default_off", warn=False)

    if not url and not auth:
        return _disabled("unconfigured", warn=False)
    if bool(url) != bool(auth):
        return _disabled("partial_pair")
    if not _valid_push_url(url):
        return _disabled("invalid_url")
    if not validate_basic_auth(auth):
        return _disabled("invalid_auth")
    return LokiConfig(enabled=True, url=url, auth=auth, source="env")


def reset_warnings() -> None:
    """Test hook: allow one-shot warnings to fire again."""
    _warned.clear()
