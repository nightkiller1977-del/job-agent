"""ACES-436: resolve the vendor's application URL, not the employer's marketing page.

Aggregators and employer career pages hand the agent a page that merely
*references* an ATS posting. Those pages carry no application form, so the apply
run correctly reported `form_not_reached` / `submit_not_found` having never
reached an application. Both URLs below were taken from the live database and
confirmed by read-only browser probes on 2026-09-23:

    valon.ai/about?ashby_jid=<uuid>#careers   -> 40 controls, no submit control
    jobs.ashbyhq.com/valon/<uuid>            -> "Apply for this Job" found

Safety note grounding `test_wrong_slug_cannot_reach_another_employer`: Ashby was
probed with deliberately wrong org slugs (stripe, notion, definitely-not-real-org)
carrying Valon's job UUID. Every one returned HTTP 200 — so status is not proof —
but the body was a generic `<title>Jobs</title>` shell, never another employer's
posting. The worst case of a derived slug is therefore "no form found", i.e.
today's behaviour, not a misdirected application.

Pure-function tests; no network, no browser, no employer contact.
"""
import pytest

from src.sources.adapters.auth_routing import external_ats_url
from src.url_utils import canonical_ats_url

# Verbatim from state/jobs.db.
VALON_MARKETING = (
    "https://www.valon.ai/about?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40"
    "&utm_source=BMXoVpKAGY#careers"
)
VALON_CANONICAL = "https://jobs.ashbyhq.com/valon/32fc3fab-87e3-4fed-9b92-db050da6fa40"

COREWEAVE_MARKETING = (
    "https://coreweave.com/careers/job?4709378006&board=coreweave"
    "&gh_jid=4709378006&gh_src=9bd3aefb6us"
)
COREWEAVE_CANONICAL = "https://boards.greenhouse.io/coreweave/jobs/4709378006"


# ── the two jobs that actually failed in production ─────────────────────────

def test_valon_marketing_page_resolves_to_ashby_application():
    assert canonical_ats_url(VALON_MARKETING) == VALON_CANONICAL


def test_coreweave_marketing_page_resolves_to_greenhouse_application():
    assert canonical_ats_url(COREWEAVE_MARKETING) == COREWEAVE_CANONICAL


# ── must be left alone ──────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    VALON_CANONICAL,                                   # already canonical
    COREWEAVE_CANONICAL,                               # already canonical
    "https://boards.greenhouse.io/embed/job_app?for=coreweave&token=4709378006",
    "https://www.usajobs.gov/job/1234",                # unrelated ATS
    "https://acme.com/careers",                        # no vendor identifier
    "",
    None,
])
def test_leaves_urls_alone_when_no_confident_rewrite(url):
    assert canonical_ats_url(url) == ""


def test_aggregator_host_never_yields_a_slug():
    """A LinkedIn URL carrying an ashby_jid must not become jobs.ashbyhq.com/linkedin/..."""
    url = ("https://www.linkedin.com/jobs/view/4366516601/"
           "?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40")
    assert canonical_ats_url(url) == ""


@pytest.mark.parametrize("url", [
    "https://acme.com/careers?ashby_jid=not-a-uuid",
    "https://acme.com/careers?ashby_jid=",
    "https://acme.com/careers?gh_jid=12",        # too short to be a real posting id
    "https://acme.com/careers?gh_jid=abc",
    "ftp://acme.com/careers?gh_jid=4709378006",  # non-http scheme
    "not-a-url-at-all?gh_jid=4709378006",
])
def test_malformed_identifiers_are_refused(url):
    assert canonical_ats_url(url) == ""


def test_wrong_slug_cannot_reach_another_employer():
    """The slug is derived only from the employer's own host.

    Two different employers therefore never collapse onto one another's board —
    the property that makes the derived-slug approach safe (see module docstring
    for the live probe that established the failure mode is an empty page).
    """
    a = canonical_ats_url("https://acme.com/careers?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40")
    b = canonical_ats_url("https://globex.com/careers?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40")
    assert a == "https://jobs.ashbyhq.com/acme/32fc3fab-87e3-4fed-9b92-db050da6fa40"
    assert b == "https://jobs.ashbyhq.com/globex/32fc3fab-87e3-4fed-9b92-db050da6fa40"
    assert a != b


def test_explicit_board_param_wins_over_host():
    """`board` states the real Greenhouse token; the host may differ legitimately."""
    url = "https://careers.acme-holdings.com/x?gh_jid=4709378006&board=acmelabs"
    assert canonical_ats_url(url) == "https://boards.greenhouse.io/acmelabs/jobs/4709378006"


def test_host_is_used_when_no_board_param():
    url = "https://acme.com/careers/job?gh_jid=4709378006"
    assert canonical_ats_url(url) == "https://boards.greenhouse.io/acme/jobs/4709378006"


def test_subdomain_resolves_to_registrable_label():
    url = "https://careers.acme.com/job?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40"
    assert canonical_ats_url(url) == "https://jobs.ashbyhq.com/acme/32fc3fab-87e3-4fed-9b92-db050da6fa40"


# ── wired into the shared resolver ──────────────────────────────────────────

def test_external_ats_url_returns_the_application_not_the_marketing_page():
    job = {"extra_json": {"ats_url": VALON_MARKETING}}
    assert external_ats_url(job) == VALON_CANONICAL


def test_external_ats_url_preserves_a_url_it_cannot_canonicalize():
    job = {"extra_json": {"ats_url": "https://acme.com/careers/apply"}}
    assert external_ats_url(job) == "https://acme.com/careers/apply"


def test_external_ats_url_still_rejects_non_http():
    assert external_ats_url({"ats_url": "javascript:alert(1)"}) == ""
    assert external_ats_url({}) == ""


def test_external_ats_url_survives_a_canonicalizer_failure(monkeypatch):
    """Canonicalization is an improvement, never a dependency."""
    import src.url_utils as uu

    def boom(_url):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(uu, "canonical_ats_url", boom)
    job = {"extra_json": {"ats_url": VALON_MARKETING}}
    assert external_ats_url(job) == VALON_MARKETING


def test_job_identity_is_not_derived_from_ats_url():
    """Canonicalizing ats_url must not re-ingest an existing job as new.

    The previous version of this test compared ``make(None, url) ==
    make(None, url)`` — the identical call on both sides, so it could not fail
    regardless of what _make_job_id does with ats_url (review finding).
    _make_job_id's signature is inspected directly: it structurally cannot see
    ats_url at all, since it is not one of its parameters. That is a stronger
    guarantee than exercising two job dicts with the same discovery url and
    different ats_url values would be, since such a test still only proves
    today's call sites happen not to pass ats_url in — it would not catch a
    future call site that starts doing so.
    """
    import inspect

    from src.sources.base import BaseScraper

    params = list(inspect.signature(BaseScraper._make_job_id).parameters)
    assert params == ["self", "url"], (
        f"_make_job_id's signature changed to {params} — if it now accepts "
        "ats_url, job_id would change whenever canonicalization rewrites the "
        "URL, silently re-ingesting every existing row as a new job."
    )


# ─── Copilot review findings on PR #150 ─────────────────────────────────────

def test_two_label_public_suffix_uk():
    """careers.acme.co.uk previously selected 'co' as the org slug, not 'acme'."""
    url = "https://careers.acme.co.uk/job?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40"
    assert canonical_ats_url(url) == (
        "https://jobs.ashbyhq.com/acme/32fc3fab-87e3-4fed-9b92-db050da6fa40"
    )


def test_two_label_public_suffix_au():
    """careers.example.com.au previously selected 'com', not 'example'."""
    url = "https://careers.example.com.au/job?gh_jid=4709378006"
    assert canonical_ats_url(url) == "https://boards.greenhouse.io/example/jobs/4709378006"


@pytest.mark.parametrize("host", ["co.uk", "com.au", "co.jp"])
def test_bare_two_label_suffix_has_no_employer_label(host):
    """The host IS the suffix, with nothing registrable under it."""
    url = f"https://{host}/job?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40"
    assert canonical_ats_url(url) == ""


def test_ordinary_single_label_suffix_still_works_with_a_subdomain():
    """Regression guard: the two-label-suffix branch must not misfire on a
    plain three-label .com host."""
    url = "https://careers.acme.com/job?gh_jid=4709378006"
    assert canonical_ats_url(url) == "https://boards.greenhouse.io/acme/jobs/4709378006"


# ─── Codex P1 finding on PR #150: wired into the actual dispatch seam ───────

@pytest.mark.asyncio
async def test_apply_external_ats_job_canonicalizes_before_dispatch(monkeypatch):
    """The seam every external caller (LinkedIn/Indeed/TheMuse/BuiltIn) funnels
    through must itself canonicalize — not just auth_routing.external_ats_url,
    which only session-prep/diagnostics consulted. Before this fix, a
    marketing URL freshly extracted mid-run by e.g. Indeed's own browser
    extraction reached the ATS navigation completely unchanged (Codex P1
    review finding on PR #150), reproducing form_not_reached/submit_not_found
    even though ACES-436 had "fixed" this.

    Stops the real function at its earliest reachable seam (get_run_log, the
    first call after canonicalization) rather than mocking deep into
    ExternalApplySession/the submission ledger/a browser.
    """
    import src.sources.jobright as jobright_module

    class _Sentinel(Exception):
        pass

    def _boom():
        raise _Sentinel("stop here — this is as far as the test needs to go")

    monkeypatch.setattr(
        jobright_module.JobrightScraper, "__init__", lambda self, *a, **k: None
    )
    scraper = jobright_module.JobrightScraper()
    scraper.last_apply_ats_url = ""
    scraper.config = {}

    monkeypatch.setattr("src.sources.adapters.runtime.get_run_log", _boom)
    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "1")

    marketing_url = (
        "https://www.valon.ai/about?ashby_jid=32fc3fab-87e3-4fed-9b92-db050da6fa40#careers"
    )
    with pytest.raises(_Sentinel):
        await scraper.apply_external_ats_job({"job_id": "j1"}, marketing_url)

    assert scraper.last_apply_ats_url == (
        "https://jobs.ashbyhq.com/valon/32fc3fab-87e3-4fed-9b92-db050da6fa40"
    ), "external_url must be canonicalized before it reaches the dispatch seam"
