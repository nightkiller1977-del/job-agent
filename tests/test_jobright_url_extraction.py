"""ACES-440: Jobright's __NEXT_DATA__ extraction reads dead pageProps keys.

`_extract_external_url` strategy 1 parses `__NEXT_DATA__` and reads
`pageProps.job || pageProps.jobDetail || pageProps.jobInfo`. A live,
unauthenticated fetch of a real Jobright job page on 2026-09-23 showed those
three keys are all absent from the payload — its actual keys are:

    ['isMobile', 'initialIsMobile', 'isSsrMobile', 'dataSource', 'baseSalary',
     'jobLocation', 'logined', 'jobHashedId', 'isTntDetail', 'pageUrl',
     'keepVisitorLayoutForPendingOnboarding', 'industryTagGroup']

so strategy 1 silently returns nothing and the failure was previously
indistinguishable from an ordinary "no external URL exists" outcome
(missing_ats_url). That probe was unauthenticated, so this file pins two
things separately: the schema-drift signal (the payload parses but the known
fields are absent) and the auth-gated signal (pageProps.logined is explicitly
false) — both derived from the SAME observed pageProps shape, since
`logined` was one of its actual keys.

Two layers of coverage:
  * `_classify_missing_ats_url` is a pure function — plain dict fixtures,
    no browser.
  * `_extract_external_url`'s own __NEXT_DATA__ parsing runs in a REAL
    headless Chromium subprocess against synthetic HTML fixtures (mirrors
    tests/test_receipt_dom_truthfulness.py's harness) — no live Jobright
    fetch, no authentication, no employer contact.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.blocker_classifier import BlockerClass, classify
from src.sources.jobright import JobrightScraper


# --------------------------------------------------------------------------- #
# 1. Pure status-decision logic — no browser
# --------------------------------------------------------------------------- #

def test_next_data_absent_falls_back_to_generic_missing_url():
    """No __NEXT_DATA__ at all (a different page state entirely, e.g. a CF
    block) — genuinely no signal, keep the original generic status."""
    status, detail = JobrightScraper._classify_missing_ats_url({"next_data_parsed": False})
    assert status == "missing_ats_url"


def test_logined_false_reports_auth_required():
    diagnostic = {"next_data_parsed": True, "logined": False, "known_field_found": False}
    status, detail = JobrightScraper._classify_missing_ats_url(diagnostic)
    assert status == "jobright_auth_required"
    assert "logined" in detail or "authenticated" in detail


def test_logined_true_but_fields_missing_reports_schema_drift():
    """Logged in (or at least not explicitly logged out), yet none of the
    known job/jobDetail/jobInfo keys exist — a real schema drift, not auth."""
    diagnostic = {"next_data_parsed": True, "logined": True, "known_field_found": False}
    status, detail = JobrightScraper._classify_missing_ats_url(diagnostic)
    assert status == "jobright_schema_drift"


def test_logined_unknown_and_fields_missing_still_reports_schema_drift():
    """logined absent/non-boolean (a further schema change) must not silently
    swallow a genuine field-drift back into the generic status — default to
    the louder, actionable signal."""
    diagnostic = {"next_data_parsed": True, "logined": None, "known_field_found": False}
    status, detail = JobrightScraper._classify_missing_ats_url(diagnostic)
    assert status == "jobright_schema_drift"


def test_known_fields_present_but_still_no_url_is_the_ordinary_case():
    """The schema is fine (job/jobDetail/jobInfo present) but this particular
    job genuinely has no external URL — must NOT be reported as drift."""
    diagnostic = {"next_data_parsed": True, "logined": True, "known_field_found": True}
    status, detail = JobrightScraper._classify_missing_ats_url(diagnostic)
    assert status == "missing_ats_url"


def test_auth_required_takes_priority_over_schema_drift():
    """When both signals could apply, the more specific/actionable one
    (auth) wins — a session fix might make the fields reappear too."""
    diagnostic = {"next_data_parsed": True, "logined": False, "known_field_found": False}
    status, _ = JobrightScraper._classify_missing_ats_url(diagnostic)
    assert status == "jobright_auth_required"


# --------------------------------------------------------------------------- #
# 2. Wired into the circuit-breaker classifier
# --------------------------------------------------------------------------- #

def test_jobright_auth_required_routes_to_auth_required_class():
    assert classify("jobright_auth_required") == BlockerClass.AUTH_REQUIRED


def test_jobright_schema_drift_routes_to_needs_human_class():
    """NEEDS_HUMAN, not UNKNOWN/TRANSIENT — retrying without a code fix can't
    succeed, and it must surface rather than blind-retry."""
    assert classify("jobright_schema_drift") == BlockerClass.NEEDS_HUMAN


def test_missing_ats_url_is_unaffected_by_the_new_statuses():
    """The original status must keep its own (unmapped -> model-cache/UNKNOWN
    fallback) behavior — this ticket must not change what missing_ats_url
    itself resolves to for existing rows."""
    assert classify("jobright_schema_drift") != classify("missing_ats_url")


# --------------------------------------------------------------------------- #
# 3. DOM regression — real headless Chromium, synthetic HTML, no network
# --------------------------------------------------------------------------- #
# Every scenario runs in a fresh subprocess for the same reason given in
# test_receipt_dom_truthfulness.py: conftest.py globally stubs
# playwright.async_api for the whole pytest run so unit modules can import
# src.sources.* without the browser stack, and an in-process unstub would be
# collection-order-dependent. A subprocess gives each scenario a clean
# interpreter where the real playwright is imported first.

_CHILD_SCRIPT = r"""
import asyncio, json, sys, traceback


async def _run(case):
    from playwright.async_api import async_playwright
    from src.sources.jobright import JobrightScraper

    base_url = case["base_url"]
    initial_html = case["initial_html"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()

            async def _route(route):
                if route.request.url.rstrip("/") == base_url.rstrip("/"):
                    await route.fulfill(
                        status=200, content_type="text/html", body=initial_html,
                    )
                else:
                    await route.abort()

            await ctx.route("**/*", _route)
            page = await ctx.new_page()
            await page.goto(base_url)

            scraper = JobrightScraper.__new__(JobrightScraper)
            scraper._last_url_extraction_diagnostic = {}

            async def _no_autofill(_page):
                return ""

            scraper._reveal_external_url_with_autofill = _no_autofill

            url = await scraper._extract_external_url(page)
            return {"url": url, "diagnostic": scraper._last_url_extraction_diagnostic}
        finally:
            await browser.close()


def main():
    try:
        case = json.loads(sys.stdin.read())
        result = asyncio.run(_run(case))
        sys.stdout.write(json.dumps(result))
    except Exception:
        sys.stdout.write(json.dumps({"error": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
"""


def _run_extraction_case(**case) -> dict:
    repo_root = os.fspath(Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.run(  # noqa: S603 — args controlled, no shell
            [sys.executable, "-c", _CHILD_SCRIPT],
            input=json.dumps(case),
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "PYTHONPATH": repo_root},
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"Jobright DOM harness subprocess timed out: {exc}")

    if proc.returncode != 0 and not proc.stdout.strip():
        pytest.fail(
            f"Jobright DOM harness subprocess exited {proc.returncode} with no stdout.\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        pytest.fail(
            f"Jobright DOM harness subprocess produced non-JSON stdout: {exc}\n"
            f"stdout was: {proc.stdout!r}\nstderr:\n{proc.stderr}"
        )
    if "error" in parsed:
        pytest.fail(f"Jobright DOM harness subprocess raised inside the child:\n{parsed['error']}")
    return parsed


BASE_URL = "https://jobright.ai/jobs/info/fixture0000000000000000"


def _next_data_html(page_props: dict) -> str:
    payload = {"props": {"pageProps": page_props}}
    return f"""
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>
    </body></html>
    """


# ── the exact dead shape observed in production, 2026-09-23 ─────────────────

DEAD_PAGE_PROPS = {
    "isMobile": False, "initialIsMobile": False, "isSsrMobile": False,
    "dataSource": "web", "baseSalary": None, "jobLocation": "Remote",
    "logined": False, "jobHashedId": "6a92f53a9864261ccd2a16be",
    "isTntDetail": False, "pageUrl": "/jobs/info/6a92f53a9864261ccd2a16be",
    "keepVisitorLayoutForPendingOnboarding": False, "industryTagGroup": None,
}


def test_observed_dead_schema_yields_no_url_and_flags_auth_required():
    """Pins the exact production payload shape (ACES-440 investigation): no
    job/jobDetail/jobInfo, logined=false. Must not silently resolve to ''
    with no distinguishing signal."""
    r = _run_extraction_case(base_url=BASE_URL, initial_html=_next_data_html(DEAD_PAGE_PROPS))
    assert r["url"] == ""
    assert r["diagnostic"]["next_data_parsed"] is True
    assert r["diagnostic"]["logined"] is False
    assert r["diagnostic"]["known_field_found"] is False

    status, _ = JobrightScraper._classify_missing_ats_url(r["diagnostic"])
    assert status == "jobright_auth_required"


def test_dead_schema_but_logged_in_flags_schema_drift_not_auth():
    """Same dead field set, but logined=true — proves the schema-drift path
    is reachable independently of the auth path, not just a renamed alias."""
    props = dict(DEAD_PAGE_PROPS, logined=True)
    r = _run_extraction_case(base_url=BASE_URL, initial_html=_next_data_html(props))
    assert r["url"] == ""
    assert r["diagnostic"]["logined"] is True
    assert r["diagnostic"]["known_field_found"] is False

    status, _ = JobrightScraper._classify_missing_ats_url(r["diagnostic"])
    assert status == "jobright_schema_drift"


def test_falsey_known_job_field_is_not_misreported_as_schema_drift():
    """A present-but-empty known field is an ordinary record shape, not a missing schema key."""
    props = dict(DEAD_PAGE_PROPS, logined=True, job=None)
    r = _run_extraction_case(base_url=BASE_URL, initial_html=_next_data_html(props))
    assert r["url"] == ""
    assert r["diagnostic"]["known_field_found"] is True
    status, _ = JobrightScraper._classify_missing_ats_url(r["diagnostic"])
    assert status == "missing_ats_url"


def test_working_schema_still_extracts_the_url_normally():
    """Regression guard: when pageProps.job carries a real URL, strategy 1
    must still work exactly as before — this fix only adds diagnostics to
    the failure path, it must not touch the success path."""
    props = dict(DEAD_PAGE_PROPS, logined=True)
    props["job"] = {"applyUrl": "https://boards.greenhouse.io/acme/jobs/123"}
    r = _run_extraction_case(base_url=BASE_URL, initial_html=_next_data_html(props))
    assert r["url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert r["diagnostic"]["known_field_found"] is True


def test_jobright_hosted_apply_url_is_rejected_like_before():
    """A same-origin (jobright.ai) URL in the known field must still be
    rejected, exactly as the pre-existing `!url.includes('jobright.ai')`
    guard did — a regression guard for behavior this change did not intend
    to touch."""
    props = dict(DEAD_PAGE_PROPS, logined=True)
    props["job"] = {"applyUrl": "https://jobright.ai/jobs/apply/123"}
    r = _run_extraction_case(base_url=BASE_URL, initial_html=_next_data_html(props))
    assert r["url"] == ""
    # The field WAS present (job existed) — this must read as the ordinary
    # missing-URL case, not schema drift.
    assert r["diagnostic"]["known_field_found"] is True
    status, _ = JobrightScraper._classify_missing_ats_url(r["diagnostic"])
    assert status == "missing_ats_url"


def test_anchor_scan_fallback_still_works_when_next_data_is_absent():
    """Strategy 2 (anchor scan) regression guard: no __NEXT_DATA__ element at
    all, but a real ATS anchor exists in the DOM."""
    html = """
    <html><body>
      <a href="https://boards.greenhouse.io/acme/jobs/456">Apply</a>
    </body></html>
    """
    r = _run_extraction_case(base_url=BASE_URL, initial_html=html)
    assert r["url"] == "https://boards.greenhouse.io/acme/jobs/456"
    assert r["diagnostic"]["next_data_parsed"] is False


def test_original_job_post_link_fallback_still_works():
    """Strategy 3 regression guard."""
    html = """
    <html><body>
      <a href="https://careers.acme.com/req/789">Original Job Post</a>
    </body></html>
    """
    r = _run_extraction_case(base_url=BASE_URL, initial_html=html)
    assert r["url"] == "https://careers.acme.com/req/789"
