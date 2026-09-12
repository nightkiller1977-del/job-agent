"""Model-backed secret classifier — teach the app what each store key is for
without adding a regex or a canonical-name entry per credential.

Purpose-based key discovery in :mod:`secret_store` uses regex over the KEY
NAME, which works for names close to a convention (IMAP_PASSWORD,
ICLOUD_APP_PASSWORD_MAC) but misses names the user chose freely
(``mail_bot_key_v2``, ``inbox-token-personal``). This module asks the shared
:class:`ModelClient` cascade to classify every unknown key by NAME + short
description prompt, and caches the ranked answer to disk so we run the
model at most once per (purpose, key-set) — a rotation that adds/removes a
key invalidates only that entry.

Values are never sent to the model — only KEY NAMES. The store's contents
stay local.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path

_log = logging.getLogger("job-agent.secret_classifier")

_CACHE_PATH = Path(__file__).parent.parent / "state" / "secret_purpose_cache.json"

# Purpose registry — the model prompt for each purpose and how many
# candidates to keep. Add a purpose here and every caller of
# :func:`classified_keys` picks it up.
PURPOSE_PROMPTS: dict[str, str] = {
    "imap_password": (
        "An IMAP inbox password used to read incoming email. This is an "
        "app-specific password / API key / access token for Apple iCloud, "
        "Gmail, Yahoo, or Outlook mail — NOT a website login password, "
        "NOT an LLM API key, NOT a Twilio/Telegram token."
    ),
    "imap_address": (
        "The email address whose inbox we should read for 2FA codes and "
        "delivery receipts. This is a mail address, not an ATS site login."
    ),
    "linkedin_login": (
        "A LinkedIn account credential (email or password)."
    ),
    "jobright_login": (
        "A jobright.ai account credential (email or password)."
    ),
    "telegram_bot_token": (
        "A Telegram Bot API token — used to post notifications to a chat."
    ),
    "twilio_sms": (
        "A Twilio account SID, auth token, or from-number used to send SMS."
    ),
}


def _cache_load() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return {}


def _cache_save(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(_CACHE_PATH)
    except Exception as exc:  # noqa: BLE001
        _log.warning("secret_classifier: could not persist cache (%s)", exc)


def _key_set_hash(keys: list[str]) -> str:
    """Order-independent hash of the CANDIDATE key set (names only)."""
    joined = "|".join(sorted(k.strip() for k in keys if k.strip()))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def classified_keys(purpose: str, candidate_keys: list[str]) -> list[str]:
    """Return cached model-ranked key names for *purpose*.

    Empty list when there is no cache entry yet or the candidate set has
    changed since the last classification (a new/removed key busts the
    entry). Callers should still fall back to regex discovery.
    """
    entry = _cache_load().get(purpose)
    if not entry:
        return []
    if entry.get("keys_hash") != _key_set_hash(candidate_keys):
        return []
    return [k for k in entry.get("ranked", []) if k in candidate_keys]


def store_classification(purpose: str, candidate_keys: list[str], ranked: list[str]) -> None:
    """Persist a classification for *purpose* keyed on the exact candidate set."""
    data = _cache_load()
    data[purpose] = {
        "keys_hash": _key_set_hash(candidate_keys),
        "keys_count": len(candidate_keys),
        "ranked": ranked,
    }
    _cache_save(data)


def _parse_ranked_names(text: str, valid_names: set[str]) -> list[str]:
    """Extract a list of key names from the model's reply.

    Accepts a JSON array (preferred), a comma-separated list, or one name
    per line. Filters out anything not in *valid_names* so the model can't
    invent keys we don't actually have.
    """
    if not text:
        return []
    text = text.strip()
    # Try JSON first (the prompt asks for it).
    for candidate in (text, _re_json_block(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                names = [str(x).strip() for x in parsed]
                return [n for n in names if n in valid_names]
            if isinstance(parsed, dict) and "keys" in parsed:
                names = [str(x).strip() for x in parsed["keys"]]
                return [n for n in names if n in valid_names]
        except Exception:
            pass
    # Fallback: split on commas / newlines and match tokens against valid_names.
    tokens = re.split(r"[,\n\r]+", text)
    ranked: list[str] = []
    for tok in tokens:
        tok = tok.strip().strip("`\"' ")
        if tok in valid_names and tok not in ranked:
            ranked.append(tok)
    return ranked


def _re_json_block(text: str) -> str:
    """Pull the first ```json``` fenced block or bare [] block out of text."""
    m = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"(\[[^\[\]]*\])", text, re.DOTALL)
    return m.group(1) if m else ""


async def classify_purpose_async(
    purpose: str,
    candidate_keys: list[str],
    *,
    force: bool = False,
    max_return: int = 6,
) -> list[str]:
    """Ask the ModelClient cascade which of *candidate_keys* serve *purpose*.

    Returns the ranked list (best fit first). Persists to cache so the next
    call for the same (purpose, candidate set) is instant.

    ``force=False`` short-circuits when a cache entry already covers the
    current candidate set. Set ``force=True`` when the caller wants to
    re-classify (e.g. after a manual key rotation).
    """
    prompt_desc = PURPOSE_PROMPTS.get(purpose)
    if not prompt_desc:
        _log.info("secret_classifier: unknown purpose=%s — no classification", purpose)
        return []
    if not candidate_keys:
        return []
    if not force:
        cached = classified_keys(purpose, candidate_keys)
        if cached:
            return cached

    try:
        from src.model_client import ModelClient
    except Exception as exc:  # noqa: BLE001
        _log.warning("secret_classifier: ModelClient import failed (%s)", exc)
        return []

    key_lines = "\n".join(f"- {k}" for k in candidate_keys)
    system = (
        "You classify environment variable NAMES by purpose. You never see or "
        "output values. Output only the JSON array requested; no prose."
    )
    user = (
        f"Purpose: {prompt_desc}\n\n"
        f"Candidate variable names (names only, values redacted):\n{key_lines}\n\n"
        f"Return a JSON array of ONLY the names above that match this purpose, "
        f"ordered best fit first. Include at most {max_return}. If none match, "
        f"return []. Example: [\"NAME_ONE\", \"NAME_TWO\"]"
    )

    try:
        mc = ModelClient(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )
        text = await mc.complete(
            messages=[{"role": "user", "content": user}],
            system=system,
            task_type="classification",
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("secret_classifier: model call failed purpose=%s error=%s", purpose, exc)
        return []

    ranked = _parse_ranked_names(text, set(candidate_keys))
    _log.info(
        "secret_classifier: model ranked purpose=%s count=%d/%d",
        purpose, len(ranked), len(candidate_keys),
    )
    store_classification(purpose, candidate_keys, ranked)
    return ranked


def refresh_purpose_cache_background(purposes: list[str] | None = None) -> None:
    """Fire-and-forget cache refresh: spawn an asyncio task that classifies
    every purpose the caller cares about.

    Callers hold no reference to the task on purpose — a failing refresh
    must never take down the pipeline. If there is no running loop we skip
    silently (the same class of call from sync context works via the
    on-demand path in resolve_imap_credentials).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    from src.secret_store import _all_store_keys  # local: avoid import cycle

    candidate_keys = _all_store_keys()
    targets = purposes or list(PURPOSE_PROMPTS.keys())

    async def _run() -> None:
        for p in targets:
            try:
                await classify_purpose_async(p, candidate_keys)
            except Exception as exc:  # noqa: BLE001
                _log.debug("secret_classifier: refresh purpose=%s failed (%s)", p, exc)

    loop.create_task(_run())
