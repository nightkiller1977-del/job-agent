"""ACES-437: BuiltIn renamed the field holding the application URL.

`builtin.py` read `job_blob["howToApply"]` at scrape time and again at apply
time. A live fetch of https://builtin.com/job/director-solution-advisory/10920296
on 2026-09-23 (HTTP 200, no Cloudflare challenge) returned a `jobPostInit` blob
keyed:

    ['id','drupalId','isSaved','applyUrl','applyText','companyName','title',
     'isEasyApply','resolvedBidId']

with no `howToApply` at all, so every BuiltIn job resolved to '' and reported
`builtin_no_ats_url` regardless of whether an application existed.

The live replacement `applyUrl` is site-relative and does not hand off to the
employer for an anonymous caller — it redirects back to BuiltIn with
`applyRequired=true`. All 11 approved BuiltIn jobs behaved that way when
checked, every one `isEasyApply=False`. That is an authentication outcome, not
a missing URL, and these tests pin both halves.

Pure functions and fakes; no network, no browser, no employer contact.
"""
import json

import pytest

from src.blocker_classifier import BlockerClass, classify
from src.sources.builtin import BuiltInScraper

DETAIL = "https://builtin.com/job/director-solution-advisory/10920296"

# Verbatim shape returned by the live fetch.
LIVE_BLOB = {
    "id": 10920296,
    "drupalId": None,
    "isSaved": False,
    "applyUrl": "/job/director-solution-advisory/10920296?handler=ApplyRedirect",
    "applyText": "",
    "companyName": "BlackLine",
    "title": "Director - Solution Advisory",
    "isEasyApply": False,
    "resolvedBidId": None,
}


# ── the regression: the live payload must yield a URL ───────────────────────

def test_live_builtin_blob_yields_an_absolute_apply_url():
    """Pre-fix this returned '' because it only looked for howToApply."""
    got = BuiltInScraper._apply_url_from_blob(LIVE_BLOB, base_url=DETAIL)
    assert got == (
        "https://builtin.com/job/director-solution-advisory/10920296?handler=ApplyRedirect"
    )


def test_relative_apply_url_is_resolved_against_the_detail_page():
    blob = {"applyUrl": "/job/x/1?handler=ApplyRedirect"}
    got = BuiltInScraper._apply_url_from_blob(blob, base_url="https://builtin.com/job/x/1")
    assert got == "https://builtin.com/job/x/1?handler=ApplyRedirect"


def test_absolute_apply_url_is_left_absolute():
    blob = {"applyUrl": "https://boards.greenhouse.io/acme/jobs/123"}
    got = BuiltInScraper._apply_url_from_blob(blob, base_url=DETAIL)
    assert got == "https://boards.greenhouse.io/acme/jobs/123"


def test_legacy_howtoapply_still_works_if_builtin_serves_it_again():
    blob = {"howToApply": "https://boards.greenhouse.io/acme/jobs/123"}
    assert BuiltInScraper._apply_url_from_blob(blob, base_url=DETAIL) == (
        "https://boards.greenhouse.io/acme/jobs/123"
    )


def test_applyurl_wins_over_legacy_key():
    blob = {
        "applyUrl": "https://jobs.lever.co/acme/new",
        "howToApply": "https://stale.example/old",
    }
    assert BuiltInScraper._apply_url_from_blob(blob, base_url=DETAIL) == (
        "https://jobs.lever.co/acme/new"
    )


@pytest.mark.parametrize("blob", [
    {}, {"applyUrl": ""}, {"applyUrl": "   "},
    {"applyUrl": "javascript:alert(1)"},   # non-http scheme must not pass through
    {"applyUrl": "mailto:jobs@acme.com"},
    None, "not-a-dict",
])
def test_missing_or_unusable_values_yield_nothing(blob):
    assert BuiltInScraper._apply_url_from_blob(blob, base_url=DETAIL) == ""


# ── the login wall is an auth outcome, not a missing URL ────────────────────

@pytest.mark.parametrize("url", [
    "https://builtin.com/job/director-solution-advisory/10920296?applyRequired=true",
    "https://builtin.com/job/x/1?handler=ApplyRedirect",
    "https://www.builtin.com/job/x/1?applyRequired=true",
])
def test_bounce_back_to_builtin_is_detected_as_a_login_wall(url):
    assert BuiltInScraper._is_builtin_login_wall(url) is True


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/acme/jobs/123",
    "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123",
    "https://builtin.com/job/x/1",          # BuiltIn, but not the wall
    "", "not-a-url",
])
def test_real_employer_urls_are_not_mistaken_for_the_wall(url):
    assert BuiltInScraper._is_builtin_login_wall(url) is False


def test_login_required_is_classified_as_auth_not_unknown():
    """It must route to session handling, not be blind-retried as a bad posting."""
    assert classify("builtin_login_required") == BlockerClass.AUTH_REQUIRED
    # the old status stays where it was, for historic rows
    assert classify("builtin_no_ats_url") != BlockerClass.AUTH_REQUIRED


@pytest.mark.asyncio
async def test_apply_reports_login_required_rather_than_no_ats_url():
    """End of the path: a wall URL must not surface as 'could not resolve'."""
    sc = BuiltInScraper.__new__(BuiltInScraper)
    sc.last_apply_status = ""
    sc.last_apply_detail = ""

    wall = "https://builtin.com/job/director-solution-advisory/10920296?applyRequired=true"
    result = await sc._apply_via_ats({"url": DETAIL}, wall, auto_submit=False)

    assert result is False
    assert sc.last_apply_status == "builtin_login_required"
    assert "signed-in session" in sc.last_apply_detail


@pytest.mark.asyncio
async def test_apply_still_reports_no_ats_url_when_there_really_is_none():
    sc = BuiltInScraper.__new__(BuiltInScraper)
    sc.last_apply_status = ""
    sc.last_apply_detail = ""

    result = await sc._apply_via_ats({"url": DETAIL}, "", auto_submit=False)

    assert result is False
    assert sc.last_apply_status == "builtin_no_ats_url"


# ─── Copilot review findings on PR #151 ─────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://notbuiltin.com/?handler=ApplyRedirect",
    "https://notbuiltin.com/job/x?applyRequired=true",
    "https://evilbuiltin.com.attacker.example/?handler=ApplyRedirect",
])
def test_lookalike_hosts_are_not_the_wall(url):
    """'builtin.com' was a netloc SUBSTRING check — 'notbuiltin.com' contains
    it and was misclassified as the wall (review finding)."""
    assert BuiltInScraper._is_builtin_login_wall(url) is False


@pytest.mark.parametrize("url", [
    "https://builtin.com/job/x?applyRequired=false",
    "https://builtin.com/job/x?applyRequired=0",
    "https://builtin.com/job/x?handler=SomethingElse",
])
def test_wrong_parameter_values_are_not_the_wall(url):
    """Presence of the key was checked, not its value — applyRequired=false
    was misclassified as the wall (review finding)."""
    assert BuiltInScraper._is_builtin_login_wall(url) is False


def test_subdomain_of_builtin_is_still_the_wall():
    assert BuiltInScraper._is_builtin_login_wall(
        "https://www.builtin.com/job/x?applyRequired=true"
    ) is True


@pytest.mark.asyncio
async def test_resolve_ats_url_prefers_a_real_stashed_url_over_a_fresh_wall(monkeypatch):
    """A successful refresh must not discard a usable stashed URL just because
    BuiltIn's applyUrl is (as observed on all 11 approved BuiltIn jobs) always
    its own login-wall redirect for an anonymous caller (review finding)."""
    sc = BuiltInScraper.__new__(BuiltInScraper)

    class _Resp:
        status_code = 200
        headers = {}
        text = "Builtin.jobPostInit(" + json.dumps({
            "job": {"applyUrl": "/job/x/1?handler=ApplyRedirect"},
        }) + ")"
        url = DETAIL

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            return _Resp()

    monkeypatch.setattr(
        "src.sources.builtin.httpx.AsyncClient", lambda *a, **k: _Client()
    )
    job = {
        "url": DETAIL,
        "extra_json": {"ats_url": "https://jobs.smartrecruiters.com/ServiceNow/x"},
    }

    resolved = await sc._resolve_ats_url(job)

    assert resolved == "https://jobs.smartrecruiters.com/ServiceNow/x"


@pytest.mark.asyncio
async def test_resolve_ats_url_still_uses_the_fresh_wall_when_nothing_better_exists(monkeypatch):
    """Control: without a usable stashed URL, the wall is still returned (it
    is at least evidence of *which* job, and downstream classifies it)."""
    sc = BuiltInScraper.__new__(BuiltInScraper)

    class _Resp:
        status_code = 200
        headers = {}
        text = "Builtin.jobPostInit(" + json.dumps({
            "job": {"applyUrl": "/job/x/1?handler=ApplyRedirect"},
        }) + ")"
        url = DETAIL

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            return _Resp()

    monkeypatch.setattr(
        "src.sources.builtin.httpx.AsyncClient", lambda *a, **k: _Client()
    )
    job = {"url": DETAIL, "extra_json": {}}

    resolved = await sc._resolve_ats_url(job)

    assert BuiltInScraper._is_builtin_login_wall(resolved) is True
