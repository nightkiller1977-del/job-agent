"""ACES-428: submit/control discovery must see ATS forms embedded in iframes.

Playwright resolves a selector within one frame. The pre-fix sweep called
``page.wait_for_selector`` on the main frame only, so a Greenhouse
``#grnhse_iframe`` / SmartRecruiters oneclick-ui / Workday embed was invisible
and every such job ended as ``submit_not_found`` — the largest non-session
bucket in state/blocker_intelligence.json (41 of 205 attempts, 0 submissions).

These use local fakes only: no browser, no network, no employer contact.
"""
import asyncio

import pytest

from src.sources.jobright import JobrightScraper

# ─── fakes ──────────────────────────────────────────────────────────────────

class FakeElement:
    def __init__(self, name, visible=True, visible_raises=False):
        self.name = name
        self._visible = visible
        self._visible_raises = visible_raises

    async def is_visible(self):
        if self._visible_raises:
            raise RuntimeError("element detached")
        return self._visible


class FakeFrame:
    """Frame whose ``query_selector`` answers from a {selector: element} map."""

    def __init__(self, url, elements=None, *, query_raises=False, evaluate_result=None,
                 evaluate_raises=False):
        self.url = url
        self._elements = elements or {}
        self._query_raises = query_raises
        self._evaluate_result = evaluate_result if evaluate_result is not None else []
        self._evaluate_raises = evaluate_raises
        self.query_calls = []

    async def query_selector(self, sel):
        self.query_calls.append(sel)
        if self._query_raises:
            raise RuntimeError("frame detached")
        return self._elements.get(sel)

    async def evaluate(self, _js, *args):
        if self._evaluate_raises:
            raise RuntimeError("cross-origin frame")
        return self._evaluate_result


class FakePage:
    def __init__(self, frames):
        self.frames = frames
        self.main_frame = frames[0] if frames else None


def _scraper():
    return JobrightScraper.__new__(JobrightScraper)


# ─── _find_submit_control ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finds_submit_control_inside_child_frame():
    """The ACES-428 regression: form lives in an iframe, main frame has only chrome."""
    btn = FakeElement("greenhouse-submit")
    main = FakeFrame("https://coreweave.com/careers/job")          # marketing page
    embed = FakeFrame("https://boards.greenhouse.io/embed/job_app",
                      {"#submit_app": btn})
    page = FakePage([main, embed])

    found = await _scraper()._find_submit_control(page, ["#submit_app"])

    assert found is btn, "submit control inside the iframe must be discovered"


@pytest.mark.asyncio
async def test_main_frame_only_page_still_works():
    """No iframes: behaviour must be unchanged."""
    btn = FakeElement("plain-submit")
    main = FakeFrame("https://jobs.example.com/apply", {"button#submit": btn})
    page = FakePage([main])

    assert await _scraper()._find_submit_control(page, ["button#submit"]) is btn


@pytest.mark.asyncio
async def test_selector_precedence_beats_frame_order():
    """Vendor-specific selectors are prepended by _confirm_and_submit and must win.

    A generic match sitting in the main frame must not beat the vendor-specific
    match inside the application form's own frame.
    """
    vendor_btn = FakeElement("vendor")
    generic_btn = FakeElement("generic")
    main = FakeFrame("https://host", {'button:text-matches("^Apply$", "i")': generic_btn})
    embed = FakeFrame("https://embed", {"#submit_app": vendor_btn})
    page = FakePage([main, embed])

    # selectors ordered vendor-first, exactly as _confirm_and_submit builds them
    found = await _scraper()._find_submit_control(
        page, ["#submit_app", 'button:text-matches("^Apply$", "i")']
    )

    assert found is vendor_btn


@pytest.mark.asyncio
async def test_invisible_control_is_not_returned():
    hidden = FakeElement("hidden", visible=False)
    page = FakePage([FakeFrame("https://host", {"#submit_app": hidden})])

    found = await _scraper()._find_submit_control(
        page, ["#submit_app"], total_timeout_ms=0
    )
    assert found is None


@pytest.mark.asyncio
async def test_detached_frame_does_not_abort_sweep():
    """A frame that raises mid-sweep must not hide a control in a sibling frame."""
    btn = FakeElement("real-submit")
    broken = FakeFrame("https://ads.example", query_raises=True)
    good = FakeFrame("https://embed", {"#submit_app": btn})
    page = FakePage([broken, good])

    assert await _scraper()._find_submit_control(page, ["#submit_app"]) is btn


@pytest.mark.asyncio
async def test_returns_none_without_paying_a_timeout_per_selector():
    """Pre-fix this loop cost ~36 selectors x 8s. Bounded sweep must be far cheaper."""
    page = FakePage([FakeFrame("https://host", {})])
    selectors = [f"#sel{i}" for i in range(36)]

    loop = asyncio.get_running_loop()
    start = loop.time()
    found = await _scraper()._find_submit_control(
        page, selectors, total_timeout_ms=200, poll_interval_ms=50
    )
    elapsed = loop.time() - start

    assert found is None
    assert elapsed < 2.0, f"bounded sweep took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_late_rendering_form_is_still_caught():
    """A control that only appears on a later poll must still be found."""
    btn = FakeElement("late")
    frame = FakeFrame("https://embed", {})

    async def _appear():
        await asyncio.sleep(0.05)
        frame._elements["#submit_app"] = btn

    asyncio.create_task(_appear())
    found = await _scraper()._find_submit_control(
        FakePage([frame]), ["#submit_app"], total_timeout_ms=2000, poll_interval_ms=20
    )
    assert found is btn


# ─── _visible_controls_snapshot ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_aggregates_controls_across_frames():
    main = FakeFrame("https://host", evaluate_result=[{"tag": "A", "text": "home"}])
    embed = FakeFrame("https://embed",
                      evaluate_result=[{"tag": "BUTTON", "text": "Submit Application"}])
    page = FakePage([main, embed])

    controls = await _scraper()._visible_controls_snapshot(page)
    texts = [c.get("text") for c in controls]

    assert "Submit Application" in texts, "iframe controls must appear in diagnostics"
    assert "home" in texts


@pytest.mark.asyncio
async def test_unreadable_frame_is_reported_not_silently_dropped():
    """'No controls anywhere' must stay distinguishable from 'could not read a frame'."""
    main = FakeFrame("https://host", evaluate_result=[])
    opaque = FakeFrame("https://embed", evaluate_raises=True)
    page = FakePage([main, opaque])

    controls = await _scraper()._visible_controls_snapshot(page)

    assert any(c.get("tag") == "FRAME" and "unreadable frame" in c.get("text", "")
               for c in controls), controls


@pytest.mark.asyncio
async def test_snapshot_respects_limit():
    many = [{"tag": "A", "text": f"link{i}"} for i in range(30)]
    page = FakePage([FakeFrame("https://a", evaluate_result=many),
                     FakeFrame("https://b", evaluate_result=many)])

    controls = await _scraper()._visible_controls_snapshot(page, limit=40)
    assert len(controls) == 40
