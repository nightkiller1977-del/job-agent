"""Phase 3 — CTA-entry vendor adapters (Microsoft, BrassRing, SmartRecruiters, Teamtailor)."""
import pytest

from src.sources.adapters.context import AtsApplyContext
from src.sources.adapters.policy import AutoSubmitPolicy
from src.sources.adapters.registry import AtsAdapterRegistry
from src.sources.adapters.session import ExternalApplySession
from src.sources.adapters.generic import detect_vendor
from src.sources.adapters.vendor_cta import (
    MicrosoftAdapter, BrassRingAdapter, SmartRecruitersAdapter, TeamtailorAdapter,
)

URLS = {
    "microsoft": "https://careers.microsoft.com/us/en/job/123/SWE",
    "brassring": "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=1",
    "smartrecruiters": "https://jobs.smartrecruiters.com/acme/74000-swe",
    "teamtailor": "https://acme.teamtailor.com/jobs/123-swe",
}


class FakePage:
    def __init__(self, url, login_wall=False, cta_clicked=False, has_form=False, receipt=None):
        self.url = url
        self.login_wall = login_wall
        self.cta_clicked = cta_clicked
        self.has_form = has_form
        self.receipt = receipt
        self.goto_urls = []

    async def goto(self, url, **kw):
        self.goto_urls.append(url)
        self.url = url

    async def evaluate(self, script, *args):
        if "forgot password" in script:   # login wall
            return self.login_wall
        if "regexes" in script:            # CTA click
            return self.cta_clicked
        if "applicant fields" in script:   # form probe (requires real fields)
            return self.has_form
        if "captcha" in script:            # generic blocker probe
            return None
        if "label" in script:              # generic questions
            return []
        if "thank you for" in script:      # receipt
            return self.receipt
        return None

    async def query_selector(self, sel):
        return None

    async def set_input_files(self, sel, path):
        pass


def _ctx(page, vendor, auto_submit=False):
    return AtsApplyContext(page=page, job={"url": URLS[vendor]}, profile={},
                           resume_path=None, auto_submit=auto_submit, url=page.url,
                           attempt_id="v1", extra={}, policy=AutoSubmitPolicy(allow=True))


ADAPTERS = {
    "microsoft": MicrosoftAdapter,
    "brassring": BrassRingAdapter,
    "smartrecruiters": SmartRecruitersAdapter,
    "teamtailor": TeamtailorAdapter,
}


@pytest.mark.parametrize("vendor", list(ADAPTERS))
def test_detect_vendor_recognizes(vendor):
    assert detect_vendor(URLS[vendor]) == vendor


def test_detect_vendor_microsoft_careers_path():
    # the /careers path form (not just careers.microsoft.com host) must still map
    assert detect_vendor("https://www.microsoft.com/en-us/careers/job/123") == "microsoft"


def test_smartrecruiters_cta_allows_curly_apostrophe():
    import re
    pat = SmartRecruitersAdapter.cta_patterns[0]
    assert re.search(pat, "I’m interested", re.I) and re.search(pat, "I'm interested", re.I)


@pytest.mark.parametrize("vendor,cls", list(ADAPTERS.items()))
@pytest.mark.asyncio
async def test_can_handle(vendor, cls):
    page = FakePage(URLS[vendor])
    assert await cls().can_handle(_ctx(page, vendor)) == 0.9
    other = _ctx(page, vendor)
    other.url = "https://acme.com/careers"
    assert await cls().can_handle(other) == 0.0


@pytest.mark.parametrize("vendor,cls", list(ADAPTERS.items()))
@pytest.mark.asyncio
async def test_login_wall_reports_auth_required(vendor, cls):
    page = FakePage(URLS[vendor], login_wall=True)
    res = await cls().apply(_ctx(page, vendor, auto_submit=True))
    assert res.submitted is False
    assert res.status == f"{vendor}_login_required"


@pytest.mark.parametrize("vendor,cls", list(ADAPTERS.items()))
@pytest.mark.asyncio
async def test_apply_not_reached_when_no_cta_and_no_form(vendor, cls):
    page = FakePage(URLS[vendor], cta_clicked=False, has_form=False)
    res = await cls().apply(_ctx(page, vendor, auto_submit=True))
    assert res.status == f"{vendor}_apply_not_reached"


@pytest.mark.parametrize("vendor,cls", list(ADAPTERS.items()))
@pytest.mark.asyncio
async def test_reaches_form_then_defers_to_generic(vendor, cls):
    # entered the form, auto_submit off -> generic withholds submit for review
    page = FakePage(URLS[vendor], cta_clicked=True)
    res = await cls().apply(_ctx(page, vendor, auto_submit=False))
    assert res.status == "review_ready"


@pytest.mark.asyncio
async def test_teamtailor_rewrites_to_application_form():
    page = FakePage(URLS["teamtailor"], cta_clicked=True)
    await TeamtailorAdapter().apply(_ctx(page, "teamtailor", auto_submit=False))
    assert page.goto_urls and page.goto_urls[-1].endswith("/applications/new")


@pytest.mark.parametrize("vendor", list(ADAPTERS))
@pytest.mark.asyncio
async def test_registered_and_selected(vendor):
    sess = ExternalApplySession({})
    ctx = AtsApplyContext(page=FakePage(URLS[vendor]), job={"url": URLS[vendor]},
                          profile={}, url=URLS[vendor])
    adapter = await sess.registry.pick(ctx)
    assert adapter.name == vendor
