"""ACES-403 — triage auth blockers: expired session vs bot interstitial.

Gate this answers: are the jobs currently blocked with an AUTH_REQUIRED
status (needs_session_prep, workday_session_expired, ...) genuinely stuck on
an expired login, or actually hitting bot/WAF detection misreported as an
auth wall? If genuine, they belong on the existing `prepare-sessions` path
instead of burning blocker_classifier's AUTH_REQUIRED retry cap (5) on
retries that can never succeed.

Scope decision: Option A (see ACES-403's Jira comment). Read state/jobs.db
for the current blocker set via StateManager.get_jobs_by_status() (existing,
read-only), then navigate each job's URL LOGGED OUT — a brand-new, in-memory
browser context with no storage_state and no saved cookies, never the
per-source persistent profile under state/sessions/ — and classify by:

    challenge iframe present                    -> bot_interstitial
    password field present, no challenge iframe  -> expired_session
    neither                                      -> inconclusive

Option B (reusing a persistent profile to test whether the saved session
itself is still valid) is explicitly deferred — only worth pursuing if
Option A proves inconclusive for a meaningful share of the blocker set.

Read-only: this script never writes to state/jobs.db, never touches
state/sessions/, and never calls prepare-sessions itself — it only produces
a report for a human (or a later, separate change) to act on. No production
apply/session-recovery path is touched.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from ..blocker_classifier import BlockerClass, _STATUS_TO_CLASS
from ..state_manager import StateManager

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks"

# Same signal shape as GenericAtsAdapter._detect_blocker
# (src/sources/adapters/generic.py:225-239) and patchright_spike.classify_outcome
# — deliberately re-declared here rather than imported: this tool must stay
# decoupled from the live apply pipeline (src.sources.*), not import from it.
_PROBE_JS = r"""() => {
    const password = !!document.querySelector('input[type="password"]');
    const challenge = !!document.querySelector(
        'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="turnstile" i]');
    return { password, challenge };
}"""

CLASSIFICATIONS = ("expired_session", "bot_interstitial", "inconclusive", "error")


def auth_blocked_jobs(state: StateManager) -> List[dict]:
    """Every job currently on an AUTH_REQUIRED status, via the existing
    get_jobs_by_status() (read-only) — one call per status string that
    blocker_classifier.classify() maps to AUTH_REQUIRED, so this always stays
    in sync with the canonical classification instead of a second hardcoded
    list."""
    auth_statuses = [s for s, cls in _STATUS_TO_CLASS.items() if cls == BlockerClass.AUTH_REQUIRED]
    jobs: List[dict] = []
    seen_ids = set()
    for status in auth_statuses:
        for job in state.get_jobs_by_status(status):
            if job.get("job_id") in seen_ids:
                continue
            seen_ids.add(job.get("job_id"))
            jobs.append(job)
    return jobs


async def classify_one(url: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    """Navigate one URL logged-out and classify it. Never raises — a
    per-job navigation failure degrades to {"classification": "error"}
    and the triage continues with the rest of the set."""
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
                    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    await asyncio.sleep(1)
                    result["title"] = await page.title()
                    probe = await page.evaluate(_PROBE_JS)
                    password = bool(probe.get("password"))
                    challenge = bool(probe.get("challenge"))
                    result["password_present"] = password
                    result["challenge_present"] = challenge
                    if challenge:
                        result["classification"] = "bot_interstitial"
                    elif password:
                        result["classification"] = "expired_session"
                    else:
                        result["classification"] = "inconclusive"
                except Exception as e:
                    result["error"] = str(e)
            finally:
                await browser.close()
    except Exception as e:
        result["error"] = str(e)
    return result


async def run_triage(db_path: str = "state/jobs.db",
                     state: Optional[StateManager] = None) -> Dict[str, Any]:
    state = state or StateManager(db_path)
    jobs = auth_blocked_jobs(state)

    print("================================================================")
    print("     ACES-403 AUTH BLOCKER TRIAGE: expired session vs bot wall  ")
    print("================================================================")
    print(f"{len(jobs)} job(s) currently on an AUTH_REQUIRED status.\n")

    rows: List[Dict[str, Any]] = []
    counts = {c: 0 for c in CLASSIFICATIONS}

    for job in jobs:
        url = job.get("url") or ""
        job_id = job.get("job_id") or ""
        source = job.get("source") or ""
        status = job.get("status") or ""
        if not url:
            outcome = {"classification": "error", "password_present": None,
                      "challenge_present": None, "title": "", "error": "no_url"}
        else:
            print(f" -> {source}/{job_id} ({status}): {url[:70]}")
            outcome = await classify_one(url)
        counts[outcome["classification"]] += 1
        rows.append({
            "job_id": job_id, "source": source, "status": status,
            **outcome,
        })

    print("\n\n================================================================")
    print("                        TRIAGE REPORT                           ")
    print("================================================================")
    for c in CLASSIFICATIONS:
        print(f"{c:<18}: {counts[c]}")
    print("-" * 66)
    print(f"Gate: {counts['expired_session']} of {len(jobs)} are genuine expired "
          f"sessions (route to prepare-sessions); {counts['bot_interstitial']} are "
          f"bot/WAF interstitials misreported as auth.")
    if counts["inconclusive"]:
        print(f"{counts['inconclusive']} inconclusive — Option B "
              "(persistent-profile session-validity check) may be worth pursuing for these.")
    print("================================================================")
    print("\nThis report does NOT change state/jobs.db or call prepare-sessions — "
          "read-only triage only. Act on the results as a separate, deliberate step.")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_auth_blocked": len(jobs),
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
