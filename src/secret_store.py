"""Central secrets resolver — Phase 1 of the single-source secrets store.

This replaces the fragmented, per-entry-point credential loading that caused
scheduled reauth failures (see commit 55e1e55 "band-aid" and its TODO(secrets)).
There is now ONE resolution order, shared by every entry point in this repo and
mirrored by the email-agent (src/lib/secrets.js):

  1. process / shell env         — a non-empty value here always wins*
  2. project .env                — loaded by main.load_env() / Orchestrator*
  3. central store (AI Commander, ~/Library/Application Support/ai-command-center):
       a. `aicc-secrets get <NAME>` CLI, if on PATH        (Phase 3 target)
       b. secrets.enc.env decrypted via `sops -d`          (Phase 2 target)
       c. .env plaintext                                   (Phase 1 / today)
  4. Azure Key Vault             — Phase 4, gated by USE_KEYVAULT (stub below)

* except STORE_AUTHORITATIVE_KEYS (shared AI-service credentials such as
  ANTHROPIC_API_KEY): for those the central store wins over any local value.

Fill semantics are **fill-missing, empty-string treated as absent**: we never
overwrite a real value already in os.environ (store-authoritative keys aside), but
an absent OR empty ("") key is filled from the central store. Empty-as-absent matters because Claude Code sets
ANTHROPIC_API_KEY="" in the shell — a plain `override=False` load would treat
that as "present" and refuse to fill it.

See SECRETS.md for the canonical key catalog and the migration phases.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

_log = logging.getLogger("job-agent.secrets")

# Directory that owns the central store. Overridable via AICC_SECRETS_DIR (used by
# tests and by anyone who relocates the AI Commander userData dir).
# Default follows Electron's app.getPath('userData') per platform:
#   macOS  → ~/Library/Application Support/ai-command-center
#   Linux  → ~/.config/ai-command-center   (XDG_CONFIG_HOME)
#   Windows→ %APPDATA%/ai-command-center
def _default_commander_dir() -> Path:
    import sys
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "ai-command-center"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / "ai-command-center"
    # Linux / other POSIX
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(xdg) / "ai-command-center"


def _commander_dir() -> Path:
    override = os.environ.get("AICC_SECRETS_DIR")
    return Path(override).expanduser() if override else _default_commander_dir()


# Every secret this repo may need. Used as the default set for fill_missing() and
# documented in SECRETS.md. Keep in sync with the email-agent catalog for shared keys.
CANONICAL_KEYS: tuple[str, ...] = (
    "AICC_OPENROUTER_API_KEY",
    "OPENROUTER_GATEWAY_URL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD",
    "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
    "INDEED_EMAIL", "INDEED_PASSWORD",
    "USAJOBS_EMAIL", "USAJOBS_PASSWORD", "USAJOBS_2FA_SECRET", "IMAP_PASSWORD",  # gitleaks:allow — key NAMES, not values
    "COMPANY_EMAIL", "COMPANY_PASSWORD",
    "COMPANY_EMAIL_ALT", "COMPANY_PASSWORD_ALT",
    "DASHBOARD_URL", "SYNC_SECRET", "CREDENTIAL_ENCRYPTION_KEY",
    "NOTIFY_PHONE",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "APPROVAL_SEND_EMAIL", "APPROVAL_SEND_PASSWORD", "APPROVAL_NOTIFY_EMAIL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)


# Shared service credentials that are OWNED by the central store. For these the store
# is authoritative: a value it holds replaces whatever the process env / project .env
# carried. Everything else keeps fill-missing semantics.
#
# Why: these keys are shared by every agent (job-agent, email-agent, the desktop app)
# and rotated in one place — aicc-secrets. With fill-missing alone, a stale copy in a
# project .env silently won over the rotation: on 2026-09-04 every local copy of
# ANTHROPIC_API_KEY was identical to a store value that Anthropic rejected with 401,
# and a rotation in the store would not have reached the agent (ACES-282). Local
# copies of these keys are a misconfiguration, not a fallback.
STORE_AUTHORITATIVE_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AICC_OPENROUTER_API_KEY",
    "OPENROUTER_GATEWAY_URL",
)


def _missing(name: str) -> bool:
    """True when *name* is absent or set to an empty/whitespace value."""
    return not (os.environ.get(name) or "").strip()


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (shell-style .env). Ignores comments/blank lines and
    strips one layer of surrounding quotes. Deliberately tiny — no interpolation."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


@lru_cache(maxsize=1)
def _read_store() -> dict[str, str]:
    """Read the central store once. Prefers the SOPS-encrypted file when both the
    file and the `sops` binary are present; otherwise falls back to plaintext .env.
    Never raises — a broken/missing store yields {} so callers degrade gracefully."""
    d = _commander_dir()
    enc = d / "secrets.enc.env"
    if enc.exists() and shutil.which("sops"):
        try:
            sops_env = os.environ.copy()
            if sops_env.get("SOPS_AGE_KEY_FILE"):
                sops_env["SOPS_AGE_KEY_FILE"] = str(Path(sops_env["SOPS_AGE_KEY_FILE"]).expanduser())
            res = subprocess.run(
                ["sops", "-d", str(enc)],
                capture_output=True, text=True, timeout=15,
                env=sops_env,
            )
            if res.returncode == 0:
                return _parse_env_text(res.stdout)
            _log.warning("secrets: sops -d failed (rc=%s) — falling back to plaintext", res.returncode)
        except Exception as exc:  # noqa: BLE001 — never let store IO break startup
            _log.warning("secrets: sops decrypt error (%s) — falling back to plaintext", exc)

    plain = d / ".env"
    if plain.exists():
        try:
            return _parse_env_text(plain.read_text())
        except Exception as exc:  # noqa: BLE001
            _log.warning("secrets: could not read %s (%s)", plain, exc)
    return {}


def _cli_get(name: str) -> str | None:
    """Resolve via the `aicc-secrets get <NAME>` CLI shipped by AI Commander.
    Returns None if the CLI is absent, errors, or reports the key missing."""
    if not shutil.which("aicc-secrets"):
        return None
    try:
        res = subprocess.run(
            ["aicc-secrets", "get", name],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if res.returncode != 0:
        return None
    val = res.stdout.strip()
    return val or None


def resolve_secret(name: str) -> str | None:
    """Resolve a single secret following the documented precedence. Does NOT mutate
    os.environ. Returns None when unresolved anywhere."""
    env_val = (os.environ.get(name) or "").strip()
    if env_val:
        return env_val
    cli_val = _cli_get(name)
    if cli_val:
        return cli_val
    store_val = _read_store().get(name, "").strip()
    return store_val or None


def _store_get(name: str) -> str | None:
    """The central store's value for *name* (CLI → sops → plaintext), ignoring the
    process env. None when the store does not have it."""
    cli_val = _cli_get(name)
    if cli_val:
        return cli_val
    store_val = _read_store().get(name, "").strip()
    return store_val or None


def apply_store_authoritative(names: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """For every STORE_AUTHORITATIVE_KEYS entry in *names* (default: all of them) that
    the central store holds, set os.environ to the store's value — overriding a
    different local value. Keys the store lacks are left alone, so a machine with no
    store (CI) is unaffected.

    Returns the NAMES whose local value was replaced (values are never logged).
    """
    keys = names if names is not None else STORE_AUTHORITATIVE_KEYS
    overridden: list[str] = []
    for name in keys:
        if name not in STORE_AUTHORITATIVE_KEYS:
            continue
        store_val = _store_get(name)
        if not store_val:
            continue
        if (os.environ.get(name) or "").strip() != store_val:
            if not _missing(name):
                overridden.append(name)
            os.environ[name] = store_val
    if overridden:
        _log.warning(
            "secrets: central store overrode a local value for %d key(s): %s — these are "
            "store-owned; remove the local copy (see SECRETS.md)",
            len(overridden), ", ".join(overridden),
        )
    return overridden


def fill_missing(names: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """Fill os.environ for every *names* entry that is absent or empty, pulling from
    the central store (CLI → sops → plaintext). Never overwrites a real value —
    EXCEPT for STORE_AUTHORITATIVE_KEYS, where the store wins (see
    apply_store_authoritative); both entry points get that for free by calling this.

    Returns the list of key NAMES that were filled (values are never logged), so the
    caller can report central-store usage without leaking secrets.
    """
    keys = names if names is not None else CANONICAL_KEYS
    filled: list[str] = []
    for name in keys:
        if not _missing(name):
            continue
        val = resolve_secret(name)
        if val:
            os.environ[name] = val
            filled.append(name)
    if filled:
        _log.info("secrets: filled %d key(s) from central store: %s", len(filled), ", ".join(filled))
    apply_store_authoritative([k for k in keys if k in STORE_AUTHORITATIVE_KEYS])
    return filled


def clear_cache() -> None:
    """Drop the cached store read (tests / after the store is rewritten)."""
    _read_store.cache_clear()
