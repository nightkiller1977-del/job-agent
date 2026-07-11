"""GenericAtsAdapter — real form-fill logic, exercised with a fake Playwright page.

No browser needed: FakePage records fill/upload/click and serves configurable
evaluate() results (blocker detection + question labels).
"""
import pytest

from src.sources.adapters.generic import GenericAtsAdapter, detect_vendor
from src.sources.adapters.context import AtsApplyContext


class FakeElement:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    async def fill(self, value):
        self._page.filled[self._selector] = value

    async def click(self):
        self._page.clicked.append(self._selector)


class FakePage:
    def __init__(self, present_selectors, evaluate_result=None):
        self._present = set(present_selectors)
        self.filled = {}
        self.clicked = []
        self.uploaded = {}
        self.url = "https://boards.greenhouse.io/acme/jobs/1"
        self._evaluate_result = evaluate_result

    async def query_selector(self, sel):
        return FakeElement(self, sel) if sel in self._present else None

    async def set_input_files(self, sel, path):
        if sel not in self._present:
            raise RuntimeError("no file input")
        self.uploaded[sel] = path

    async def evaluate(self, script):
        # First evaluate call in apply() is blocker detection; questions call second.
        if "captcha" in script:
            return self._evaluate_result if not isinstance(self._evaluate_result, list) else None
        if "label" in script:
            return self._evaluate_result if isinstance(self._evaluate_result, list) else []
        return None


PROFILE = {
    "personal_info": {"first_name": "Ada", "last_name": "Lovelace",
                      "email": "ada@example.com", "phone": "555-0100"},
    "social_links": {"linkedin": "https://linkedin.com/in/ada"},
    "disclosures": {"authorized_to_work": True, "requires_sponsorship": False},
}

GH = "greenhouse"


def _ctx(page, auto_submit=False, resume="/tmp/r.pdf"):
    return AtsApplyContext(page=page, job={"url": page.url}, profile=PROFILE,
                           resume_path=resume, auto_submit=auto_submit, url=page.url)


def test_detect_vendor():
    assert detect_vendor("https://boards.greenhouse.io/x/jobs/1") == "greenhouse"
    assert detect_vendor("https://jobs.lever.co/x/1") == "lever"
    assert detect_vendor("https://x.ashbyhq.com/1") == "ashby"
    assert detect_vendor("https://x.myworkdayjobs.com/1") == "workday"
    assert detect_vendor("https://acme.com/careers") == "generic"


@pytest.mark.asyncio
async def test_fills_identity_and_withholds_submit_by_default():
    page = FakePage(present_selectors={
        "input[name*='first_name' i], input[id*='first_name' i]",
        "input[name*='last_name' i], input[id*='last_name' i]",
        "input[name*='email' i], input[id*='email' i]",
        "input[name*='phone' i], input[id*='phone' i]",
        "input[type='file'][name*='resume' i], input[type='file'][id*='resume' i]",
        "#submit_app",
    }, evaluate_result=None)
    res = await GenericAtsAdapter().apply(_ctx(page, auto_submit=False))

    assert page.filled  # identity fields filled
    assert any("Ada" == v for v in page.filled.values())
    assert page.uploaded  # resume uploaded
    assert res.submitted is False
    assert res.status == "review_ready"      # submit withheld when auto_submit=False
    assert page.clicked == []                # never clicked submit


@pytest.mark.asyncio
async def test_submits_when_auto_submit_and_present():
    page = FakePage(present_selectors={
        "input[name*='email' i], input[id*='email' i]",
        "#submit_app",
    }, evaluate_result=None)
    res = await GenericAtsAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is True and res.status == "applied"
    assert "#submit_app" in page.clicked


@pytest.mark.asyncio
async def test_submit_not_found_when_no_button():
    page = FakePage(present_selectors={
        "input[name*='email' i], input[id*='email' i]",
    }, evaluate_result=None)
    res = await GenericAtsAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is False and res.status == "submit_not_found"


@pytest.mark.asyncio
async def test_blocker_detected_short_circuits_before_submit():
    page = FakePage(present_selectors={"#submit_app"}, evaluate_result="captcha")
    res = await GenericAtsAdapter().apply(_ctx(page, auto_submit=True))
    assert res.submitted is False and res.status == "captcha"
    assert page.clicked == []  # never attempted submit behind a captcha


@pytest.mark.asyncio
async def test_policy_gate_can_withhold_submit():
    page = FakePage(present_selectors={"#submit_app"}, evaluate_result=None)

    class DenyPolicy:
        async def confirm_submit(self, ctx, evidence):
            return False

    ctx = _ctx(page, auto_submit=True)
    ctx.policy = DenyPolicy()
    res = await GenericAtsAdapter().apply(ctx)
    assert res.status == "submit_denied_by_policy"
    assert page.clicked == []
