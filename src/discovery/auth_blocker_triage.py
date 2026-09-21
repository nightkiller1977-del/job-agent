"""ACES-403 — triage auth blockers: bot interstitial vs a login wall worth
checking further.

Gate this answers: of the jobs whose latest apply attempt hit an
AUTH_REQUIRED status (needs_session_prep, workday_session_expired, ...), how
many show clear bot/WAF detection (misreported as an auth wall — those
should NOT burn blocker_classifier's AUTH_REQUIRED retry cap) versus a real
login wall worth investigating further (candidates for prepare-sessions,
pending the Option B check below)?

Scope decision: Option A (see ACES-403's Jira comment). Read state/jobs.db
strictly read-only (see read_jobs_with_apply_status below — never through
StateManager's constructor, which creates directories/the db file/WAL
sidecars and runs schema migrations as side effects; a diagnostic tool
advertised as read-only must not touch a missing or older database before
its first query), filter to AUTH_REQUIRED via blocker_classifier.classify()
(the canonical classification — not a second hardcoded status list), then
navigate each job's ATS URL LOGGED OUT — a brand-new, in-memory browser
context with no storage_state and no saved cookies, never the per-source
persistent profile under state/sessions/ — and classify by:

    challenge iframe present                    -> bot_interstitial
    password field present, no challenge iframe  -> login_wall
    neither                                      -> no_wall_detected

Jobs job-agent's OWN workflow status already marks `expired` are excluded
from live navigation and reported separately as `posting_expired` — a stale
posting was confirmed against a real run: Workday still returns HTTP 200 and
a rendered page for a removed job req, just with "The page you are looking
for doesn't exist" as the body text, indistinguishable from a real page by
title/status-code alone. Navigating those tells you nothing about session or
bot state; job-agent already knows the answer to a different question
("does this posting still exist") without a live request.

IMPORTANT (Copilot + Codex review, PR #140): a password field in this
deliberately logged-out, cookie-less context does NOT prove the *saved*
session expired — it only proves the portal requires login when accessed
with no cookies at all, which is true of essentially every authenticated job
portal regardless of whether the real saved session is still valid. So
`login_wall` is not "confirmed expired_session" — it is the candidate set
Option B should check next. Determining genuine expiry requires testing the
isolated, persistent saved profile itself (Option B), which is explicitly
deferred — only worth pursuing if Option A's login_wall count is a
meaningful share of the set.

apply_last_status is recorded per-attempt, on the ORIGINAL discovery source
(job.url) for source-owned auth, but for jobs routed through an external ATS
(LinkedIn/Indeed -> Workday etc.) the wall actually occurred on the ATS
portal, recorded separately in extra_json.ats_url. Reuses the existing
external_ats_url() resolver (src/sources/adapters/auth_routing.py) rather
than a second implementation, falling back to job.url only when no ATS URL
was recorded — a pure, side-effect-free helper, not adapter/browser logic,
so importing it doesn't couple this tool to the live apply pipeline.

Read-only: this script never writes to state/jobs.db, never touches
state/sessions/, and never calls prepare-sessions itself — it only produces
a report for a human (or a later, separate change) to act on. No production
apply/session-recovery path is touched.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from ..blocker_classifier import BlockerClass, classify
from ..sources.adapters.auth_routing import external_ats_url
from ..state_manager import parse_extra_json

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks"

# Same signal shape as GenericAtsAdapter._detect_blocker
# (src/sources/adapters/generic.py:225-239) and patchright_spike.classify_outcome
# — deliberately re-declared here rather than imported: the probe itself must
# stay decoupled from the live apply/browser pipeline (src.sources.adapters.*
# beyond the one pure URL-resolution helper above), not import from it.
_PROBE_JS = r"""() => {
    const password = !!document.querySelector('input[type="password"]');
    const challengeFrame = !!document.querySelector(
        'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="turnstile" i]');
    // Copilot review, PR #140: an iframe-only check misses a text-only
    // JS challenge (e.g. some Cloudflare "checking your browser"
    // interstitials render no iframe at all) — same signal generic.py's
    // _detect_blocker and patchright_spike.py's classify_outcome both check.
    const bodyText = (document.body && document.body.innerText || '').toLowerCase();
    const challengeText = /attention required|access denied|security check|please confirm you are human|checking your browser|verify you are human|verify your connection|cloudflare/.test(bodyText);
    return { password, challenge: challengeFrame || challengeText };
}"""

CLASSIFICATIONS = ("login_wall", "bot_interstitial", "no_wall_detected", "posting_expired", "error")


def read_jobs_with_apply_status(db_path: str = "state/jobs.db") -> List[dict]:
    """Every job with a recorded apply_last_status, read via a strictly
    read-only SQLite connection (URI mode=ro): never creates the db file or
    its parent directory, never touches WAL sidecar files, never runs a
    schema migration — unlike StateManager's constructor, which does all of
    that as a side effect (Codex review, PR #140). A diagnostic tool
    advertised as read-only must not be able to create or modify
    state/jobs.db just by being pointed at a missing or older one.

    Returns [] if the database file doesn't exist yet — that's a valid,
    reportable state for a triage tool, not an error to raise on.
    """
    path = Path(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE extra_json LIKE '%apply_last_status%'"
        ).fetchall()
    finally:
        conn.close()

    out: List[dict] = []
    for r in rows:
        job = dict(r)
        extra = parse_extra_json(job.get("extra_json"))
        status = extra.get("apply_last_status")
        if status:
            job["apply_last_status"] = status
            job["ats_url"] = extra.get("ats_url", "")
            out.append(job)
    return out


def auth_blocked_jobs(jobs: List[dict]) -> List[dict]:
    """Filter an already-fetched job list (see read_jobs_with_apply_status)
    to those whose apply_last_status classifies as AUTH_REQUIRED, via the
    public blocker_classifier.classify() — the canonical classification, not
    a second hardcoded status list.

    Copilot + Codex review (PR #140): the original version queried the
    workflow `jobs.status` column via StateManager.get_jobs_by_status().
    Apply-blocker outcomes are persisted in extra_json.apply_last_status
    instead — record_apply_attempt's own docstring says it "does NOT change
    the job status field" (src/state_manager.py) — so that version silently
    returned zero rows against real data, which was not evidence that no
    blockers exist.

    Pure function (no I/O) — trivial to unit test with plain dicts.
    """
    return [j for j in jobs if classify(j.get("apply_last_status")) is BlockerClass.AUTH_REQUIRED]


# Statuses meaning "the discovery source's OWN login session was the blocker"
# — mirrors src/orchestrator.py's _OWN_SESSION_STATUSES_ANY / _OWN_SESSION_STATUSES
# (duplicated, not imported: orchestrator.py is the live pipeline's entry
# point — heavy to pull into a read-only diagnostic tool, same reasoning as
# _PROBE_JS above). External-ATS walls (workday_session_expired,
# brassring_login_required, ...) are deliberately NOT here — a discovery
# source's own re-auth never clears those (orchestrator.py's own comment).
_SOURCE_OWNED_STATUSES_ANY = {"reauth_failed", "needs_session_prep"}
_SOURCE_OWNED_STATUSES_BY_SOURCE = {
    "linkedin": {"linkedin_authwall", "linkedin_login_required"},
    "usajobs": {"usajobs_login_required"},
}


def blocker_url(job: dict) -> str:
    """The URL where the recorded blocker actually occurred.

    Copilot review (PR #140): extra_json.ats_url can be stale — record_apply_attempt()
    (state_manager.py) merges new metadata into extra_json without clearing
    older fields, so an ats_url from an earlier, unrelated external-ATS
    attempt can still be sitting there when the CURRENT apply_last_status is
    actually a source-owned status (the discovery source's own session, not
    any ATS portal). Only prefer ats_url for portal-owned statuses; a
    source-owned status always uses job.url.
    """
    status = job.get("apply_last_status") or ""
    source = job.get("source") or ""
    is_source_owned = (
        status in _SOURCE_OWNED_STATUSES_ANY
        or status in _SOURCE_OWNED_STATUSES_BY_SOURCE.get(source, set())
    )
    if is_source_owned:
        return str(job.get("url") or "")
    return external_ats_url(job) or str(job.get("url") or "")


async def classify_one(url: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    """Navigate one URL logged-out and classify it. Never raises — a
    per-job navigation failure degrades to {"classification": "error"}
    and the triage continues with the rest of the set.

    Waits for `networkidle`, not a fixed sleep after `domcontentloaded`:
    confirmed against a real Workday URL that `domcontentloaded` + 1s fires
    while the page is still an empty client-rendered shell (title='',
    body_len=0) — every real signal (title, password field, challenge
    iframe) is still unrendered at that point, silently producing
    `no_wall_detected` for pages that were never actually inspected.
    """
    result: Dict[str, Any] = {
        "classification": "error", "password_present": None,
        "challenge_present": None, "title": "", "error": None,
    }
    try:
        async with async_playwright() as p:
            # Fresh, in-memory, throwaway context: no storage_state, no
            # user_data_dir — never the shared per-source profile under
            # state/sessions/, and nothing persists after this closes.
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    try:
                        await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                    except Exception:
                        # A page that never goes network-idle (long-poll,
                        # analytics beacon, etc.) shouldn't fail the whole
                        # probe — domcontentloaded + a fixed settle window
                        # already got it, just fall back to that.
                        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        await asyncio.sleep(3)
                    result["title"] = await page.title()
                    probe = await page.evaluate(_PROBE_JS)
                    password = bool(probe.get("password"))
                    challenge = bool(probe.get("challenge"))
                    result["password_present"] = password
                    result["challenge_present"] = challenge
                    if challenge:
                        result["classification"] = "bot_interstitial"
                    elif password:
                        result["classification"] = "login_wall"
                    else:
                        result["classification"] = "no_wall_detected"
                except Exception as e:
                    result["error"] = str(e)
            finally:
                await browser.close()
    except Exception as e:
        result["error"] = str(e)
    return result


async def run_triage(db_path: str = "state/jobs.db",
                     jobs: Optional[List[dict]] = None) -> Dict[str, Any]:
    """jobs: pre-fetched rows from read_jobs_with_apply_status(), or None to
    read them from db_path via the read-only path above. Tests inject plain
    dicts here directly — no database, no StateManager, no fakes needed."""
    all_jobs = jobs if jobs is not None else read_jobs_with_apply_status(db_path)
    blocked = auth_blocked_jobs(all_jobs)
    live_candidates = [j for j in blocked if j.get("status") != "expired"]
    already_expired = [j for j in blocked if j.get("status") == "expired"]

    print("================================================================")
    print("   ACES-403 AUTH BLOCKER TRIAGE: bot interstitial vs login wall ")
    print("================================================================")
    print(f"{len(blocked)} job(s) with a latest apply attempt classifying as AUTH_REQUIRED "
          f"({len(already_expired)} already marked expired in job-agent's own tracking — "
          f"excluded from live navigation, reported as posting_expired).\n")

    rows: List[Dict[str, Any]] = []
    counts = {c: 0 for c in CLASSIFICATIONS}

    for job in already_expired:
        counts["posting_expired"] += 1
        rows.append({
            "job_id": job.get("job_id") or "", "source": job.get("source") or "",
            "apply_last_status": job.get("apply_last_status") or "",
            "url_used": "", "classification": "posting_expired",
            "password_present": None, "challenge_present": None, "title": "", "error": None,
        })

    for job in live_candidates:
        url = blocker_url(job)
        job_id = job.get("job_id") or ""
        source = job.get("source") or ""
        status = job.get("apply_last_status") or ""
        if not url:
            outcome = {"classification": "error", "password_present": None,
                      "challenge_present": None, "title": "", "error": "no_url"}
        else:
            print(f" -> {source}/{job_id} ({status}): {url[:70]}")
            outcome = await classify_one(url)
        counts[outcome["classification"]] += 1
        rows.append({
            "job_id": job_id, "source": source, "apply_last_status": status,
            "url_used": url,
            **outcome,
        })

    print("\n\n================================================================")
    print("                        TRIAGE REPORT                           ")
    print("================================================================")
    for c in CLASSIFICATIONS:
        print(f"{c:<18}: {counts[c]}")
    print("-" * 66)
    print(f"{counts['posting_expired']} already marked expired by job-agent — excluded from "
          "the gate below (a live navigation there answers a different question).")
    print(f"Gate (of {len(live_candidates)} still-live postings): "
          f"{counts['bot_interstitial']} are bot/WAF interstitials misreported as auth — "
          "these should not burn the AUTH_REQUIRED retry cap.")
    print(f"{counts['login_wall']} show a login wall with no bot challenge — "
          "candidates for prepare-sessions, NOT confirmed expirations. Option B "
          "(checking the isolated persistent saved profile) is needed to tell a "
          "genuinely expired session apart from one that was simply never "
          "authenticated in this fresh, logged-out context.")
    if counts["no_wall_detected"]:
        print(f"{counts['no_wall_detected']} showed neither signal — worth a manual look "
              "(could still be a slow-rendering page; classify_one waits for networkidle "
              "but a very slow SPA could still race it).")
    print("================================================================")
    print("\nThis report does NOT change state/jobs.db or call prepare-sessions — "
          "read-only triage only. Act on the results as a separate, deliberate step.")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_auth_blocked": len(blocked),
        "live_candidate_count": len(live_candidates),
        "counts": counts,
        "jobs": rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "aces-403-auth-blocker-triage-results.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out_path}")

    return report


if __name__ == "__main__":
    asyncio.run(run_triage())
