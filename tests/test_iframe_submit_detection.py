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

import src.sources.jobright as jobright_module
from src.sources.jobright import JobrightScraper

# ─── fakes ──────────────────────────────────────────────────────────────────

class FakeElement:
    def __init__(
        self,
        name,
        visible=True,
        visible_raises=False,
        evaluate_raises=False,
        on_evaluate=None,
    ):
        self.name = name
        self._visible = visible
        self._visible_raises = visible_raises
        self._evaluate_raises = evaluate_raises
        self._on_evaluate = on_evaluate
        self.clicked = False
        self.evaluate_calls = 0

    async def is_visible(self):
        if self._visible_raises:
            raise RuntimeError("element detached")
        return self._visible

    async def evaluate(self, _js):
        self.evaluate_calls += 1
        if self._evaluate_raises:
            raise RuntimeError("click evaluation failed")
        self.clicked = True
        if self._on_evaluate:
            self._on_evaluate()


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
    def __init__(self, frames, url=None):
        self.frames = frames
        self.main_frame = frames[0] if frames else None
        self.url = url or (self.main_frame.url if self.main_frame else "")

    async def evaluate(self, js, *args):
        if self.main_frame is None:
            return None
        return await self.main_frame.evaluate(js, *args)


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


@pytest.mark.asyncio
async def test_snapshot_budget_cannot_be_exhausted_by_main_frame():
    """Marketing links in the host page must not starve the embedded ATS frame."""
    marketing = [{"tag": "A", "text": f"marketing-link-{i}"} for i in range(40)]
    submit = {"tag": "BUTTON", "text": "Submit Application"}
    page = FakePage([
        FakeFrame("https://host.example/jobs/1", evaluate_result=marketing),
        FakeFrame("https://boards.greenhouse.io/embed/1", evaluate_result=[submit]),
    ])

    controls = await _scraper()._visible_controls_snapshot(page, limit=40)

    assert len(controls) == 40
    assert submit in controls


# ─── frame-aware form gate ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_iframe_only_form_satisfies_application_form_gate():
    """A host page with no fields must not hide a real form in its ATS iframe."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    embed = FakeFrame("https://boards.greenhouse.io/embed/1", evaluate_result=True)

    assert await _scraper()._looks_like_application_form(FakePage([main, embed])) is True


# ─── final-submit safety ──────────────────────────────────────────────────────

async def _no_delay(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_workday_next_control_is_not_treated_as_final_submit():
    """An intermediate Workday Next button must stay outside the submit boundary."""
    next_button = FakeElement("Next")
    frame = FakeFrame(
        "https://acme.myworkdayjobs.com/job/1",
        {
            "[data-automation-id='bottom-navigation-next-button']": next_button,
            "[data-automation-id=\"bottom-navigation-next-button\"]": next_button,
            "button[data-automation-id='nextButton']": next_button,
            "button[type='submit']": next_button,
        },
        evaluate_result=True,
    )
    page = FakePage([frame], url=frame.url)
    scraper = _scraper()
    original_find = scraper._find_submit_control

    async def _find_without_wait(candidate_page, selectors):
        return await original_find(candidate_page, selectors, total_timeout_ms=0)

    async def _empty_snapshot(*_args, **_kwargs):
        return []

    scraper._find_submit_control = _find_without_wait
    scraper._visible_controls_snapshot = _empty_snapshot
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert next_button.clicked is False
    assert scraper.last_apply_status == "workday_submit_not_found"


@pytest.mark.asyncio
async def test_clicked_submit_without_fresh_receipt_is_unverified(monkeypatch):
    """A successful DOM click is ambiguous until fresh ATS acceptance evidence appears."""
    submit_button = FakeElement("Submit Application")
    frame = FakeFrame(
        "https://boards.greenhouse.io/acme/jobs/1",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([frame], url=frame.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(_page):
        return object()

    async def _no_fresh_receipt(_page, **_kwargs):
        return False, ""

    monkeypatch.setattr(
        jobright_module, "capture_receipt_evidence", _capture_baseline, raising=False
    )
    monkeypatch.setattr(jobright_module, "verify_receipt", _no_fresh_receipt, raising=False)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submit_button.clicked is True
    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"
    assert not getattr(scraper, "_apply_analytics", {}).get("submitted", False)


@pytest.mark.asyncio
async def test_fresh_receipt_inside_submit_iframe_allows_success(monkeypatch):
    """Receipt verification must inspect the frame that owns the final submit."""
    submit_button = FakeElement("Submit Application")
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    embed = FakeFrame(
        "https://boards.greenhouse.io/embed/1",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, embed], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _receipt_for_embed_only(frame, **_kwargs):
        if frame is embed:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt_for_embed_only)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is True
    assert scraper._apply_analytics["receiptSignal"] == "t:application received"


@pytest.mark.asyncio
async def test_replaced_submit_iframe_is_reenumerated_for_fresh_receipt(monkeypatch):
    """A newly attached confirmation frame must replace the detached pre-click frame."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    confirmation = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    page = None

    def _replace_iframe():
        page.frames = [main, confirmation]

    submit_button = FakeElement("Submit Application", on_evaluate=_replace_iframe)
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _receipt_only_in_replacement(frame, *, baseline, **_kwargs):
        if frame is confirmation and baseline is application:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt_only_in_replacement)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is True
    assert scraper._apply_analytics["receiptSignal"] == "t:application received"


@pytest.mark.asyncio
async def test_unrelated_new_frame_cannot_validate_receipt(monkeypatch):
    """An injected non-ATS iframe must not prove that the submit click succeeded."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    unrelated = FakeFrame(
        "https://analytics.example/confirmation",
        evaluate_result=True,
    )
    page = None

    def _inject_unrelated_frame():
        page.frames.append(unrelated)

    submit_button = FakeElement("Submit Application", on_evaluate=_inject_unrelated_frame)
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _receipt_only_in_unrelated_frame(frame, **_kwargs):
        if frame is unrelated:
            return True, "url:https://analytics.example/confirmation"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt_only_in_unrelated_frame)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"


@pytest.mark.asyncio
async def test_replacement_frame_reuses_submit_context_baseline(monkeypatch):
    """A remounted stale receipt is compared with the pre-click ATS state."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    replacement = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    page = None

    def _replace_iframe():
        page.frames = [main, replacement]

    submit_button = FakeElement("Submit Application", on_evaluate=_replace_iframe)
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay
    seen_baselines = []

    async def _capture_baseline(frame):
        return frame

    async def _stale_receipt(frame, *, baseline, **_kwargs):
        if frame is replacement:
            seen_baselines.append(baseline)
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _stale_receipt)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert seen_baselines
    assert all(baseline is application for baseline in seen_baselines)


@pytest.mark.asyncio
async def test_delayed_replacement_frame_is_reenumerated_during_receipt_polling(monkeypatch):
    """A replacement that appears after the first poll must still be inspected."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    confirmation = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    submit_button = FakeElement("Submit Application")
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay
    application_checks = 0

    async def _capture_baseline(frame):
        return frame

    async def _delayed_receipt(frame, *, baseline, **_kwargs):
        nonlocal application_checks
        if frame is application:
            application_checks += 1
            if application_checks == 1:
                page.frames = [main, confirmation]
            return False, ""
        if frame is confirmation and baseline is application:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _delayed_receipt)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is True
    assert scraper._apply_analytics["receiptSignal"] == "t:application received"


@pytest.mark.asyncio
async def test_both_click_evaluations_failing_never_records_submission(monkeypatch):
    """Both click paths failing must stop before receipt or success bookkeeping."""
    submit_button = FakeElement("Submit Application", evaluate_raises=True)
    frame = FakeFrame(
        "https://boards.greenhouse.io/acme/jobs/1",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(receipt_frame):
        return receipt_frame

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)

    submitted = await scraper._confirm_and_submit(
        FakePage([frame], url=frame.url),
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submit_button.evaluate_calls == 2
    assert submitted is False
    assert scraper.last_apply_status == "submit_click_failed"
    assert not getattr(scraper, "_apply_analytics", {}).get("submitted", False)
