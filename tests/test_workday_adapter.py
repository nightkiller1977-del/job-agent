"""Phase 3 — WorkdayAdapter: wizard entry, session-expiry gate, stepping, gated submit."""
import pytest

from src.sources.adapters.context import AtsApplyContext
from src.sources.adapters.workday import (
    WorkdayAdapter, _NEXT_SELECTORS, _SUBMIT_SELECTORS,
)
from src.sources.adapters.policy import AutoSubmitPolicy
from src.sources.adapters.registry import AtsAdapterRegistry
from src.sources.adapters.session import ExternalApplySession

WD_URL = "https://acme.wd5.myworkdayjobs.com/careers/job/R123"


class _El:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    async def fill(self, v):
        self.page.filled[self.sel] = v

    async def click(self):
        self.page.clicks.append(self.sel)
        if self.sel in _NEXT_SELECTORS:
            self.page.step += 1


class FakeWorkdayPage:
    def __init__(self, url=WD_URL, final_step=0, login_gate=False, receipt=None, stuck=False):
        self.url = url
        self.step = 0
        self.final_step = final_step
        self.login_gate = login_gate
        self.receipt = receipt
        self.stuck = stuck
        self.filled, self.clicks, self.uploaded = {}, [], {}

    async def goto(self, url, **kw):
        self.url = url

    async def evaluate(self, script, *args):
        if "texts.some" in script:      # chooser click
            return True
        if "create account" in script:  # login-gate probe
            return self.login_gate
        if "thank you for" in script:    # receipt probe
            return self.receipt
        return None

    async def query_selector(self, sel):
        if self.stuck:
            return None
        if sel in _SUBMIT_SELECTORS:
            return _El(self, sel) if self.step >= self.final_step else None
        if sel in _NEXT_SELECTORS:
            return _El(self, sel) if self.step < self.final_step else None
        return None

    async def set_input_files(self, sel, path):
        self.uploaded[sel] = path


def _ctx(page, auto_submit=True):
    return AtsApplyContext(page=page, job={"url": WD_URL}, profile={}, resume_path="/tmp/r.pdf",
                           auto_submit=auto_submit, url=WD_URL, attempt_id="w1", extra={},
                           policy=AutoSubmitPolicy(allow=True))


@pytest.mark.asyncio
async def test_can_handle_workday():
    a = WorkdayAdapter()
    assert await a.can_handle(_ctx(FakeWorkdayPage())) == 0.9
    ctx = _ctx(FakeWorkdayPage())
    ctx.url = "https://boards.greenhouse.io/x/jobs/1"
    assert await a.can_handle(ctx) == 0.0


@pytest.mark.asyncio
async def test_session_expiry_returns_workday_session_expired_without_submitting():
    page = FakeWorkdayPage(login_gate=True)
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is False
    assert res.status == "workday_session_expired"
    assert page.clicks == []            # never attempted a submit behind the gate


@pytest.mark.asyncio
async def test_applied_only_with_receipt():
    page = FakeWorkdayPage(final_step=0, receipt="t:thank you for applying")
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is True and res.verified is True and res.status == "applied"
    assert page.uploaded                 # resume uploaded for autofill


@pytest.mark.asyncio
async def test_click_without_receipt_is_unverified():
    page = FakeWorkdayPage(final_step=0, receipt=None)
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is False and res.status == "submission_unverified"


@pytest.mark.asyncio
async def test_withholds_submit_when_not_auto_submit():
    page = FakeWorkdayPage(final_step=0)
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=False))
    assert res.status == "review_ready" and page.clicks == []


@pytest.mark.asyncio
async def test_steps_through_multi_page_wizard():
    page = FakeWorkdayPage(final_step=2, receipt="t:application received")
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=True))
    assert res.status == "applied"
    assert page.clicks.count(_NEXT_SELECTORS[0]) == 2   # advanced two pages


@pytest.mark.asyncio
async def test_stuck_wizard_returns_form_not_reached():
    page = FakeWorkdayPage(stuck=True)
    res = await WorkdayAdapter().apply(_ctx(page, auto_submit=True))
    assert res.status == "form_not_reached"


@pytest.mark.asyncio
async def test_registered_and_selected_for_workday_urls():
    sess = ExternalApplySession({})
    ctx = AtsApplyContext(page=FakeWorkdayPage(), job={"url": WD_URL}, profile={}, url=WD_URL)
    adapter = await sess.registry.pick(ctx)
    assert adapter.name == "workday"
