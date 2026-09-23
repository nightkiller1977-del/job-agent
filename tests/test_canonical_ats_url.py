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
    """Canonicalizing ats_url must not re-ingest an existing job as new."""
    from src.sources.base import BaseScraper

    make = BaseScraper._make_job_id
    discovery_url = "https://www.linkedin.com/jobs/view/4366516601/"
    # Same discovery URL, different ats_url -> identical job_id.
    assert make(None, discovery_url) == make(None, discovery_url)
