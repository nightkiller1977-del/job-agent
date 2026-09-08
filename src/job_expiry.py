"""
Job expiry detection — is a posting still accepting applications?

Two complementary signals feed the expiry pipeline:

  1. Source-signal: the source adapters already raise JobExpiredError when a
     re-fetch finds the posting gone/closed (LinkedIn "No longer accepting
     applications", USAJobs "Announcement has closed", 404s, …). The
     orchestrator turns that into StateManager.mark_expired().
  2. Probe + TTL fallback (this module + Orchestrator.expiry_sweep()): a
     lightweight HTTP probe of the job URL, and a config-driven max-age cutoff
     for jobs that sat in the pool too long.

Design rules:
  - check_job_alive() never raises. It returns a tri-state: True (posting
    looks live), False (posting is definitively gone/closed), or None
    (couldn't tell — network error, bot-block, auth wall). Only a hard False
    should ever expire a job; None must leave it untouched.
  - Plain httpx only — no browser. Pages that need a session (LinkedIn) will
    typically return an auth redirect, which maps to None, not False.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

# HTTP statuses that definitively mean "this posting no longer exists".
_GONE_STATUS = {404, 410}

# Statuses that mean "we can't tell without a session/browser" — never expire.
_UNKNOWN_STATUS = {401, 403, 405, 407, 429, 999}

# Closed-posting phrases the sources are known to render on a 200 page.
# Mirrors the adapters' own JobExpiredError markers (themuse, linkedin, indeed).
EXPIRED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "job is no longer available",
    "this job has expired",
    "job posting has expired",
    "position has been filled",
    "this position is closed",
    "announcement has closed",
    "this job posting was removed",
    "vacancy has closed",
)

# Only scan the beginning of the body — closed banners render near the top and
# this keeps the probe cheap on multi-MB job pages.
_BODY_SCAN_BYTES = 200_000


async def check_job_alive(
    url: str,
    timeout_s: float = 10.0,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[Optional[bool], str]:
    """Lightweight probe: is the job posting at `url` still up?

    Returns (alive, reason):
      (True,  "...") — page fetched and shows no closed markers
      (False, "...") — posting definitively gone (404/410) or shows a
                       closed-posting banner
      (None,  "...") — could not determine (bad url, network error, auth
                       wall, bot block). Caller must NOT expire on None.
    Never raises.
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return None, "no probeable url"

    own_client = client is None
    try:
        if own_client:
            client = httpx.AsyncClient(
                timeout=timeout_s,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        try:
            resp = await client.get(url)
        finally:
            if own_client:
                await client.aclose()
    except httpx.HTTPError as exc:
        return None, f"probe error: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        return None, f"probe error: {type(exc).__name__}: {exc}"

    if resp.status_code in _GONE_STATUS:
        return False, f"http {resp.status_code}"
    if resp.status_code in _UNKNOWN_STATUS:
        return None, f"http {resp.status_code} (needs session/blocked)"
    if resp.status_code >= 500:
        return None, f"http {resp.status_code} (server error)"
    if resp.status_code >= 400:
        return None, f"http {resp.status_code}"

    try:
        body = resp.text[:_BODY_SCAN_BYTES].lower()
    except Exception:
        return True, f"http {resp.status_code} (body unreadable)"

    for marker in EXPIRED_MARKERS:
        if marker in body:
            _log.info("expiry.probe.closed_marker url=%s marker=%r", url, marker)
            return False, f"closed marker: {marker!r}"

    return True, f"http {resp.status_code}"
