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
        # The real snapshot JS applies `.slice(0, limit)`; the fake must too,
        # or a per-frame budget looks like it is being ignored.
        limit = args[0] if args else None
        if isinstance(limit, int):
            return self._evaluate_result[:limit]
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


# ─── ACES-428 follow-up: defects found by a live probe, not by the fakes ─────
#
# A real CoreWeave/Greenhouse page exposed 9 frames (host, the Greenhouse
# job_app embed, a googleapis proxy, a reCAPTCHA frame, several about:blank).
# Two problems the instant-returning fakes above could never surface:
#   1. the deadline was only checked BETWEEN passes, so one slow frame let an
#      8s budget overrun by more than 10x;
#   2. the host page's ~40 marketing links consumed the whole snapshot limit,
#      so the embedded form's controls never reached the failure detail.


class SlowFrame(FakeFrame):
    """Frame that never answers — models an ad/reCAPTCHA frame wedging the sweep."""

    def __init__(self, url, delay=30.0):
        super().__init__(url)
        self._delay = delay

    async def query_selector(self, sel):
        await asyncio.sleep(self._delay)


@pytest.mark.asyncio
async def test_unresponsive_frame_cannot_blow_the_total_budget():
    """One wedged frame must not extend the sweep past total_timeout_ms."""
    page = FakePage([SlowFrame("https://recaptcha.example"),
                     FakeFrame("https://host", {})])
    selectors = [f"#sel{i}" for i in range(36)]

    loop = asyncio.get_running_loop()
    start = loop.time()
    found = await _scraper()._find_submit_control(
        page, selectors,
        total_timeout_ms=1000, poll_interval_ms=50, per_query_timeout_ms=100,
    )
    elapsed = loop.time() - start

    assert found is None
    assert elapsed < 5.0, f"budget overrun: {elapsed:.1f}s for a 1s budget"


@pytest.mark.asyncio
async def test_slow_frame_does_not_hide_a_control_in_a_healthy_frame():
    btn = FakeElement("real-submit")
    page = FakePage([SlowFrame("https://ads.example"),
                     FakeFrame("https://embed", {"#submit_app": btn})])

    found = await _scraper()._find_submit_control(
        page, ["#submit_app"],
        total_timeout_ms=3000, poll_interval_ms=50, per_query_timeout_ms=100,
    )
    assert found is btn


@pytest.mark.asyncio
async def test_iframe_controls_survive_a_chatty_host_page():
    """The live regression: host nav must not crowd the embedded form out."""
    host_nav = [{"tag": "A", "text": f"nav{i}"} for i in range(60)]
    page = FakePage([
        FakeFrame("https://coreweave.com/careers/job", evaluate_result=host_nav),
        FakeFrame("https://job-boards.greenhouse.io/embed/job_app",
                  evaluate_result=[{"tag": "BUTTON", "text": "Submit Application"}]),
    ])

    controls = await _scraper()._visible_controls_snapshot(page, limit=40)
    texts = [c.get("text") for c in controls]

    assert "Submit Application" in texts, (
        "embedded form controls were crowded out by host-page navigation"
    )
    assert len(controls) <= 40


class HangingEvaluateFrame(FakeFrame):
    """Frame whose evaluate never returns — models the live reCAPTCHA frame.

    Per-frame budgeting made this reachable: the old fill-to-limit loop usually
    broke after the main frame and never evaluated the rest.
    """

    def __init__(self, url, delay=30.0):
        super().__init__(url)
        self._delay = delay

    async def evaluate(self, _js, *args):
        await asyncio.sleep(self._delay)
        return []


@pytest.mark.asyncio
async def test_snapshot_cannot_hang_on_an_unresponsive_frame():
    """This runs on the FAILURE path — hanging here stalls the whole apply run."""
    page = FakePage([
        FakeFrame("https://host", evaluate_result=[{"tag": "A", "text": "home"}]),
        HangingEvaluateFrame("https://www.recaptcha.net/recaptcha/enterprise/anchor"),
        FakeFrame("https://job-boards.greenhouse.io/embed/job_app",
                  evaluate_result=[{"tag": "BUTTON", "text": "Submit Application"}]),
    ])

    loop = asyncio.get_running_loop()
    start = loop.time()
    controls = await _scraper()._visible_controls_snapshot(page, frame_timeout_ms=200)
    elapsed = loop.time() - start

    assert elapsed < 5.0, f"snapshot hung for {elapsed:.1f}s"
    texts = [c.get("text") for c in controls]
    # the wedged frame is reported, not silently dropped...
    assert any("unresponsive frame" in (t or "") for t in texts), texts
    # ...and it must not prevent the real form's controls being collected
    assert "Submit Application" in texts, texts


# ─── Copilot review findings on PR #149 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_last_frame_is_not_crowded_out_by_earlier_frames():
    """9 chatty frames + limit 40: a floor of 5 allocated 45 and dropped frame 9.

    The embedded form is frequently enumerated last, so losing the final frame
    loses exactly what the budgeting exists to preserve.
    """
    chatty = [{"tag": "A", "text": f"nav{i}"} for i in range(30)]
    frames = [FakeFrame(f"https://noise{i}", evaluate_result=chatty) for i in range(8)]
    frames.append(FakeFrame("https://job-boards.greenhouse.io/embed/job_app",
                            evaluate_result=[{"tag": "BUTTON", "text": "Submit Application"}]))

    controls = await _scraper()._visible_controls_snapshot(FakePage(frames), limit=40)
    texts = [c.get("text") for c in controls]

    assert "Submit Application" in texts, "the LAST frame must still get a slot"
    assert len(controls) <= 40


@pytest.mark.asyncio
async def test_poll_sleep_never_overshoots_the_deadline():
    """100ms remaining with a 500ms poll interval must not overshoot."""
    page = FakePage([FakeFrame("https://host", {})])

    loop = asyncio.get_running_loop()
    start = loop.time()
    found = await _scraper()._find_submit_control(
        page, ["#nope"],
        total_timeout_ms=100, poll_interval_ms=500, per_query_timeout_ms=50,
    )
    elapsed = loop.time() - start

    assert found is None
    assert elapsed < 0.45, f"overshot a 100ms budget by sleeping a full poll: {elapsed:.2f}s"
