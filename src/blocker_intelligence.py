"""Model-backed learning layer over :mod:`blocker_classifier`.

The static status → class map handles known outcomes. Everything the
scrapers emit new (a fresh vendor blocker, a novel timeout string) falls
to :class:`BlockerClass.UNKNOWN` and gets the default cap of 2, whether
that status has succeeded 90 % of the time or has never worked.

This module folds the persisted history (:meth:`StateManager.get_apply_funnel`)
back into the decision:

1. **Unknown-blocker classification.** For each unmapped status, we
   send the status name + short samples of reason strings + the
   per-source outcome history to the ModelClient cascade and ask it to
   classify into one of the same buckets used by :class:`BlockerClass`
   (transient / auth_required / needs_human / permanent). The answer is
   cached to ``state/blocker_intelligence.json`` keyed on
   ``(status, sha256(sample_reasons))``.

2. **Adaptive retry cap.** Even for known-class statuses, if a
   (source, status) pair has a measured success rate of 0 % over ≥5
   attempts, the cap drops to 1 for that pair — keep trying it for one
   more sample per new job, but stop the "17 retries on a doomed job"
   loop the static cap alone let through.

Values from the store are never sent to the model. Only status names,
reason texts, and success rates.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path

_log = logging.getLogger("job-agent.blocker_intelligence")

_CACHE_PATH = Path(__file__).parent.parent / "state" / "blocker_intelligence.json"

# The buckets returned by the classifier — must match BlockerClass names in
# src.blocker_classifier so the caller can convert without a translation map.
_VALID_CLASSES = ("transient", "auth_required", "needs_human", "permanent")

# A (source, status) pair with this many attempts and zero submits gets a
# hard-lowered retry cap even when the static class would allow more.
_DOOMED_MIN_ATTEMPTS = 5


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
        _log.warning("blocker_intelligence: cache write failed (%s)", exc)


def _samples_hash(samples: list[str]) -> str:
    joined = "\n".join(s.strip() for s in samples if s and s.strip())
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def classified_status(status: str, sample_reasons: list[str]) -> str | None:
    """Return the cached bucket name for *status*, or None on cache miss.

    Cache miss when: no entry yet, or the sample reasons have changed
    materially since the model last saw them (new failure texts = re-ask).
    """
    entry = _cache_load().get("classifications", {}).get(status)
    if not entry:
        return None
    if entry.get("samples_hash") != _samples_hash(sample_reasons):
        return None
    verdict = entry.get("class")
    return verdict if verdict in _VALID_CLASSES else None


def _store_classification(status: str, sample_reasons: list[str], verdict: str) -> None:
    data = _cache_load()
    data.setdefault("classifications", {})[status] = {
        "samples_hash": _samples_hash(sample_reasons),
        "class": verdict,
    }
    _cache_save(data)


def adaptive_cap(
    source: str,
    status: str,
    static_cap: int,
    funnel: dict | None = None,
) -> tuple[int, str]:
    """Return an adjusted retry cap for a (source, status) pair.

    Reads the persisted per-source funnel (or an inline copy for tests) and
    lowers the cap when a (source, status) has proven doomed over
    ≥ :const:`_DOOMED_MIN_ATTEMPTS` samples with 0 submits. Never raises
    the cap: the static map is the ceiling, not the floor.
    """
    reason = ""
    if not source or not status or static_cap <= 1:
        return static_cap, reason
    try:
        data = funnel if funnel is not None else _cache_load().get("funnel", {})
    except Exception:
        data = {}
    pair = (
        data.get("per_source_status", {})
        .get(source, {})
        .get(status)
    )
    if not pair:
        return static_cap, reason
    attempts = int(pair.get("attempts", 0) or 0)
    submitted = int(pair.get("submitted", 0) or 0)
    if attempts >= _DOOMED_MIN_ATTEMPTS and submitted == 0:
        return 1, f"adaptive-cap: 0/{attempts} historical success for {source}:{status}"
    return static_cap, reason


def store_funnel_snapshot(per_source_status: dict) -> None:
    """Persist a compact per-(source, status) attempt/submit rollup so the
    sync ``adaptive_cap`` can read it without touching the DB on every call.

    Expected shape: ``{source: {status: {attempts: int, submitted: int}}}``.
    """
    data = _cache_load()
    data["funnel"] = {"per_source_status": per_source_status}
    _cache_save(data)


def _parse_verdict(text: str) -> str | None:
    """Extract one of _VALID_CLASSES from the model's reply."""
    if not text:
        return None
    lowered = text.strip().lower()
    # Model may return JSON like {"class": "auth_required"} or plain text.
    m = re.search(r'"class"\s*:\s*"([a-z_]+)"', lowered)
    if m and m.group(1) in _VALID_CLASSES:
        return m.group(1)
    for candidate in _VALID_CLASSES:
        # Match on a word boundary so "transient" doesn't get picked out
        # of the word "transiently".
        if re.search(rf"\b{candidate}\b", lowered):
            return candidate
    return None


async def classify_status_async(
    status: str,
    sample_reasons: list[str],
    per_source_history: dict | None = None,
    *,
    force: bool = False,
) -> str | None:
    """Ask the model cascade which BlockerClass bucket *status* belongs in.

    *sample_reasons* is a small list (≤ 10) of the actual reason strings
    the scraper attached to this status in the DB — the model uses them
    as evidence. *per_source_history* is an optional
    ``{source: {"attempts": N, "submitted": M}}`` breakdown for the same
    status; it gives the model measured success rates to argue from.

    Returns one of :data:`_VALID_CLASSES` or None on error / unrecognized
    reply. Cached to disk so a second call for the same evidence is
    instant.
    """
    if not status:
        return None
    if not force:
        cached = classified_status(status, sample_reasons)
        if cached:
            return cached

    try:
        from src.model_client import ModelClient
    except Exception as exc:  # noqa: BLE001
        _log.warning("blocker_intelligence: ModelClient import failed (%s)", exc)
        return None

    history_lines = ""
    if per_source_history:
        parts = []
        for src, ps in per_source_history.items():
            att = int(ps.get("attempts", 0) or 0)
            sub = int(ps.get("submitted", 0) or 0)
            if att:
                rate = f"{(sub / att) * 100:.0f}%"
                parts.append(f"- {src}: {sub}/{att} succeeded ({rate})")
        if parts:
            history_lines = "\nHistorical outcomes for this status:\n" + "\n".join(parts)

    sample_block = ""
    if sample_reasons:
        samples = "\n".join(f"- {s.strip()[:200]}" for s in sample_reasons[:10] if s and s.strip())
        if samples:
            sample_block = f"\nSample reason strings observed for this status:\n{samples}"

    system = (
        "You classify job-application blocker statuses into one of exactly four "
        "control-flow buckets. Output ONLY the bucket name — one lowercase word.\n\n"
        "Buckets:\n"
        "- transient: a network/timeout/bot-block/temporary error worth retrying a few times.\n"
        "- auth_required: session expired, login wall, portal signout; needs reauth.\n"
        "- needs_human: page structure / field / submit issue; retrying won't help; a person must look.\n"
        "- permanent: unwinnable (bad URL, unknown source, credentials missing, job expired); never retry."
    )
    user = (
        f"Blocker status: {status}\n"
        f"{sample_block}\n"
        f"{history_lines}\n\n"
        f"Which bucket? Reply with exactly one of: {', '.join(_VALID_CLASSES)}"
    )

    try:
        mc = ModelClient(anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        text = await mc.complete(
            messages=[{"role": "user", "content": user}],
            system=system,
            task_type="classification",
            max_tokens=60,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("blocker_intelligence: model call failed status=%s error=%s", status, exc)
        return None

    verdict = _parse_verdict(text)
    if verdict:
        _log.info("blocker_intelligence: classified status=%s → %s", status, verdict)
        _store_classification(status, sample_reasons, verdict)
    return verdict


def refresh_from_funnel_background(funnel: dict) -> None:
    """Fire-and-forget: (1) persist the per-(source, status) rollup for
    :func:`adaptive_cap`, then (2) enqueue a model classification for every
    status that isn't in the static map.

    Callers hold no reference to the task on purpose — a failing refresh
    must never take down the pipeline.
    """
    try:
        from src.blocker_classifier import _STATUS_TO_CLASS
    except Exception:
        _STATUS_TO_CLASS = {}

    per_source_status: dict = {}
    unknown_statuses: dict[str, dict] = {}  # status → {samples, per_source_history}

    for src, statuses in (funnel.get("per_source_status") or {}).items():
        for status, stats in statuses.items():
            per_source_status.setdefault(src, {})[status] = stats
            if status in _STATUS_TO_CLASS:
                continue
            entry = unknown_statuses.setdefault(
                status, {"samples": [], "per_source": {}}
            )
            entry["per_source"][src] = {
                "attempts": stats.get("attempts", 0),
                "submitted": stats.get("submitted", 0),
            }
            for reason in (stats.get("sample_reasons") or [])[:5]:
                if reason and reason not in entry["samples"]:
                    entry["samples"].append(reason)

    store_funnel_snapshot(per_source_status)

    if not unknown_statuses:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        for status, evidence in unknown_statuses.items():
            try:
                await classify_status_async(
                    status,
                    evidence["samples"],
                    per_source_history=evidence["per_source"],
                )
            except Exception as exc:  # noqa: BLE001
                _log.debug(
                    "blocker_intelligence: refresh status=%s failed (%s)", status, exc
                )

    loop.create_task(_run())
