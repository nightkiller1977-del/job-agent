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
        evaluate_raises_after_callback=False,
        on_evaluate=None,
    ):
        self.name = name
        self._visible = visible
        self._visible_raises = visible_raises
        self._evaluate_raises = evaluate_raises
        self._evaluate_raises_after_callback = evaluate_raises_after_callback
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
        if self._evaluate_raises_after_callback:
            raise RuntimeError("click evaluation failed after dispatch")


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
async def test_fresh_receipt_with_failed_ledger_completion_is_unverified(monkeypatch):
    """Receipt evidence is not success until it is durably recorded."""

    class FailingCompletionLedger:
        def claim(self, _key, _attempt_id, *, job_id=""):
            return None

        def complete(self, _key, _attempt_id, *, verified):
            assert verified is True
            raise OSError("disk unavailable")

    sensitive_url = (
        "https://applicant:super-secret@boards.greenhouse.io/acme/jobs/1"
        "?gh_jid=private-token"
    )
    submit_button = FakeElement("Submit Application")
    frame = FakeFrame(
        sensitive_url,
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    scraper = _scraper()
    scraper._submission_ledger = FailingCompletionLedger()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(receipt_frame):
        return receipt_frame

    async def _fresh_receipt(_frame, **_kwargs):
        return True, "t:application received"

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _fresh_receipt)

    submitted = await scraper._confirm_and_submit(
        FakePage([frame], url=frame.url),
        {"job_id": "job-1", "title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submit_button.evaluate_calls == 1
    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"
    assert "durably record" in scraper.last_apply_detail
    assert "boards.greenhouse.io" in scraper.last_apply_detail
    assert "applicant" not in scraper.last_apply_detail
    assert "super-secret" not in scraper.last_apply_detail
    assert "private-token" not in scraper.last_apply_detail
    assert not getattr(scraper, "_apply_analytics", {}).get("submitted", False)


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
async def test_receipt_poll_caps_same_origin_replacement_frames(monkeypatch):
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        evaluate_result=True,
    )
    replacements = [
        FakeFrame(
            f"https://boards.greenhouse.io/embed/replacement-{index}",
            evaluate_result=True,
        )
        for index in range(20)
    ]
    checked = []

    async def _no_receipt(frame, **_kwargs):
        checked.append(frame)
        return False, ""

    monkeypatch.setattr(jobright_module, "verify_receipt", _no_receipt)
    scraper = _scraper()
    scraper._delay = _no_delay

    signal = await scraper._verify_submit_receipt(
        FakePage(replacements),
        submit_frame=application,
        submit_origin="https://boards.greenhouse.io",
        receipt_baselines=[(application, application)],
        retries=0,
        max_frames=4,
    )

    assert signal == ""
    assert checked == replacements[:4]


@pytest.mark.asyncio
async def test_receipt_baseline_capture_is_bounded_and_timeout_safe(monkeypatch):
    frames = [
        FakeFrame(f"https://boards.greenhouse.io/embed/{index}")
        for index in range(20)
    ]
    checked = []

    async def _capture(frame):
        checked.append(frame)
        if frame is frames[0]:
            await asyncio.sleep(30)
        return frame

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture)
    scraper = _scraper()
    loop = asyncio.get_running_loop()
    started = loop.time()

    baselines = await scraper._capture_receipt_baselines(
        FakePage(frames),
        max_frames=4,
        per_frame_timeout_ms=50,
    )

    assert checked == frames[:4]
    assert baselines == [(frame, frame) for frame in frames[1:4]]
    assert loop.time() - started < 1.0


@pytest.mark.asyncio
async def test_unbaselined_same_origin_frame_cannot_become_fresh_replacement(
    monkeypatch,
):
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        evaluate_result=False,
    )
    stale_confirmation = FakeFrame(
        "https://boards.greenhouse.io/embed/already-confirmed",
        evaluate_result=True,
    )

    async def _capture(frame):
        return frame

    async def _receipt(frame, **_kwargs):
        if frame is stale_confirmation:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt)
    scraper = _scraper()
    scraper._delay = _no_delay
    baselines = await scraper._capture_receipt_baselines(
        FakePage([application, stale_confirmation]),
        required_frame=application,
        max_frames=1,
    )

    signal = await scraper._verify_submit_receipt(
        FakePage([stale_confirmation]),
        submit_frame=application,
        submit_origin="https://boards.greenhouse.io",
        receipt_baselines=baselines,
        retries=0,
    )

    assert signal == ""


@pytest.mark.asyncio
async def test_opaque_incomplete_frame_cannot_later_validate_same_origin_receipt(
    monkeypatch,
):
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        evaluate_result=False,
    )
    opaque_stale_confirmation = FakeFrame("about:blank", evaluate_result=True)

    async def _capture(frame):
        return frame

    async def _receipt(frame, **_kwargs):
        if frame is opaque_stale_confirmation:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt)
    scraper = _scraper()
    scraper._delay = _no_delay
    baselines = await scraper._capture_receipt_baselines(
        FakePage([application, opaque_stale_confirmation]),
        required_frame=application,
        max_frames=1,
    )

    opaque_stale_confirmation.url = (
        "https://boards.greenhouse.io/embed/already-confirmed"
    )
    signal = await scraper._verify_submit_receipt(
        FakePage([opaque_stale_confirmation]),
        submit_frame=application,
        submit_origin="https://boards.greenhouse.io",
        receipt_baselines=baselines,
        retries=0,
    )

    assert signal == ""


@pytest.mark.parametrize("dom_available", [False, True])
@pytest.mark.asyncio
async def test_incomplete_receipt_evidence_marks_frame_incomplete(
    monkeypatch, dom_available
):
    from src.sources.adapters.receipt import ReceiptEvidence

    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        evaluate_result=False,
    )
    opaque_stale_confirmation = FakeFrame("about:blank", evaluate_result=True)

    async def _capture(frame):
        if frame is opaque_stale_confirmation:
            # This is the real shape returned when capture_receipt_evidence()
            # absorbs an evaluator failure: either the DOM is unavailable or
            # its independent match-count snapshot is incomplete.
            return ReceiptEvidence(None, None, None, dom_available)
        return ReceiptEvidence(None, None, 0, True)

    async def _receipt(frame, **_kwargs):
        if frame is opaque_stale_confirmation:
            return True, "url:https://boards.greenhouse.io/confirmation"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt)
    scraper = _scraper()
    scraper._delay = _no_delay
    baselines = await scraper._capture_receipt_baselines(
        FakePage([application, opaque_stale_confirmation]),
        required_frame=application,
        max_frames=2,
    )

    opaque_stale_confirmation.url = (
        "https://boards.greenhouse.io/embed/already-confirmed"
    )
    signal = await scraper._verify_submit_receipt(
        FakePage([opaque_stale_confirmation]),
        submit_frame=application,
        submit_origin="https://boards.greenhouse.io",
        receipt_baselines=baselines,
        retries=0,
    )

    assert signal == ""
    assert all(
        frame is not opaque_stale_confirmation for frame, _baseline in baselines
    )
    assert any(
        frame is opaque_stale_confirmation
        for frame in baselines.incomplete_frames
    )


@pytest.mark.asyncio
async def test_receipt_poll_times_out_one_frame_without_hiding_healthy_sibling(
    monkeypatch,
):
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        evaluate_result=True,
    )
    hanging = FakeFrame(
        "https://boards.greenhouse.io/embed/hanging",
        evaluate_result=True,
    )
    healthy = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )

    async def _receipt(frame, **_kwargs):
        if frame is hanging:
            await asyncio.sleep(30)
        if frame is healthy:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt)
    scraper = _scraper()
    scraper._delay = _no_delay

    loop = asyncio.get_running_loop()
    started = loop.time()
    signal = await scraper._verify_submit_receipt(
        FakePage([hanging, healthy]),
        submit_frame=application,
        submit_origin="https://boards.greenhouse.io",
        receipt_baselines=[(application, application)],
        retries=0,
        per_frame_timeout_ms=50,
    )

    assert signal == "t:application received"
    assert loop.time() - started < 1.0


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
async def test_submit_frame_redirected_off_origin_cannot_validate_receipt(monkeypatch):
    """A surviving Frame object is untrusted after it leaves the ATS origin."""
    application = None

    def _redirect_off_origin():
        application.url = "https://analytics.example/confirmation"

    submit_button = FakeElement(
        "Submit Application", on_evaluate=_redirect_off_origin
    )
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([application], url=application.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _redirect_receipt(frame, **_kwargs):
        if frame is application:
            return True, "url:https://analytics.example/confirmation"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _redirect_receipt)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"


@pytest.mark.asyncio
async def test_off_origin_submit_owner_cannot_fall_back_to_same_origin_sibling(
    monkeypatch,
):
    """A still-attached owner that leaves the ATS origin poisons the poll.

    A same-origin sibling appearing after dispatch cannot stand in for the
    redirected owner; replacement-frame recovery is allowed only after the
    original owner actually detaches.
    """
    application = None
    page = None
    sibling = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )

    def _redirect_owner_and_add_sibling():
        application.url = "https://analytics.example/confirmation"
        page.frames = [application, sibling]

    submit_button = FakeElement(
        "Submit Application", on_evaluate=_redirect_owner_and_add_sibling
    )
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([application], url=application.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _sibling_receipt(frame, **_kwargs):
        if frame is sibling:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _sibling_receipt)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"


@pytest.mark.asyncio
async def test_opaque_submit_owner_cannot_validate_receipt(monkeypatch):
    """An about:blank owner has no trustworthy ATS origin attribution."""
    submit_button = FakeElement("Submit Application")
    application = FakeFrame(
        "about:blank",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([application], url=application.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _capture_baseline(frame):
        return frame

    async def _opaque_receipt(frame, **_kwargs):
        if frame is application:
            return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _opaque_receipt)

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
async def test_remounted_same_origin_sibling_keeps_stale_evidence(monkeypatch):
    """A remounted stale sibling must be checked against every origin baseline."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    stale_sibling = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    remounted_stale = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    page = None

    def _remount_stale_sibling():
        page.frames = [main, remounted_stale]

    submit_button = FakeElement("Submit Application", on_evaluate=_remount_stale_sibling)
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application, stale_sibling], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay
    seen_baselines = []

    async def _capture_baseline(frame):
        return frame

    async def _receipt_against_each_baseline(frame, *, baseline, **_kwargs):
        if frame is remounted_stale:
            seen_baselines.append(baseline)
            if baseline is application:
                return True, "t:application received"
            if baseline is stale_sibling:
                return False, ""
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _receipt_against_each_baseline)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert application in seen_baselines
    assert stale_sibling in seen_baselines


@pytest.mark.asyncio
async def test_surviving_same_origin_frame_keeps_its_own_baseline(monkeypatch):
    """A stale sibling receipt must not inherit the detached submit baseline."""
    main = FakeFrame("https://host.example/jobs/1", evaluate_result=False)
    stale_receipt = FakeFrame(
        "https://boards.greenhouse.io/embed/confirmation",
        evaluate_result=True,
    )
    page = None

    def _detach_application():
        page.frames = [main, stale_receipt]

    submit_button = FakeElement("Submit Application", on_evaluate=_detach_application)
    application = FakeFrame(
        "https://boards.greenhouse.io/embed/application",
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    page = FakePage([main, application, stale_receipt], url=main.url)
    scraper = _scraper()
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay
    seen_baselines = []

    async def _capture_baseline(frame):
        return frame

    async def _stale_sibling_receipt(frame, *, baseline, **_kwargs):
        if frame is stale_receipt:
            seen_baselines.append(baseline)
            if baseline is application:
                return True, "t:application received"
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _stale_sibling_receipt)

    submitted = await scraper._confirm_and_submit(
        page,
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert submitted is False
    assert seen_baselines
    assert all(baseline is stale_receipt for baseline in seen_baselines)


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
async def test_click_exception_after_dispatch_is_not_retried(monkeypatch):
    """An uncertain first dispatch must never trigger a blind second click."""
    dispatches = 0

    def _record_dispatch():
        nonlocal dispatches
        dispatches += 1

    submit_button = FakeElement(
        "Submit Application",
        evaluate_raises_after_callback=True,
        on_evaluate=_record_dispatch,
    )
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

    assert submit_button.evaluate_calls == 1
    assert dispatches == 1
    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"
    assert not getattr(scraper, "_apply_analytics", {}).get("submitted", False)


@pytest.mark.asyncio
async def test_unverified_dispatch_is_durably_blocked_from_retry(monkeypatch, tmp_path):
    """Legacy submission ambiguity must survive process-level scraper recreation."""
    from src.sources.adapters.idempotency import SubmissionLedger, canonical_key

    ledger = SubmissionLedger(path=tmp_path / "apply-ledger.json")

    async def _capture_baseline(frame):
        return frame

    async def _no_receipt(_frame, **_kwargs):
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _no_receipt)

    first_button = FakeElement("Submit Application")
    first_frame = FakeFrame(
        "https://boards.greenhouse.io/acme/jobs/1",
        {"#submit_app": first_button},
        evaluate_result=True,
    )
    first = _scraper()
    first._submission_ledger = ledger
    first._delay = _no_delay
    first._run_pre_submission_validation = _no_delay

    first_result = await first._confirm_and_submit(
        FakePage([first_frame], url=first_frame.url),
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    key = canonical_key({"url": first_frame.url})
    assert first_result is False
    assert first_button.evaluate_calls == 1
    assert ledger.needs_reconciliation(key)

    second_button = FakeElement("Submit Application")
    second_frame = FakeFrame(
        first_frame.url,
        {"#submit_app": second_button},
        evaluate_result=True,
    )
    second = _scraper()
    second._submission_ledger = ledger
    second._delay = _no_delay
    second._run_pre_submission_validation = _no_delay

    second_result = await second._confirm_and_submit(
        FakePage([second_frame], url=second_frame.url),
        {"title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    assert second_result is False
    assert second.last_apply_status == "submit_unverified_unresolved"
    assert second_button.evaluate_calls == 0


@pytest.mark.asyncio
async def test_legacy_ledger_outcome_redacts_sensitive_canonical_key(
    monkeypatch, tmp_path
):
    """Cloud-bound legacy details must never expose credentials or URL tokens."""
    from src.sources.adapters.idempotency import SubmissionLedger, canonical_key

    sensitive_url = (
        "https://applicant:super-secret@boards.greenhouse.io/acme/jobs/1"
        "?gh_jid=private-token"
    )
    job = {
        "job_id": "job-sensitive-url",
        "title": "Engineer",
        "company": "Acme",
        "url": sensitive_url,
    }
    ledger = SubmissionLedger(path=tmp_path / "apply-ledger.json")
    key = canonical_key(job)
    ledger.claim(key, "previous-attempt", job_id="another-job")
    ledger.complete(key, "previous-attempt", verified=True)

    async def _capture_baseline(frame):
        return frame

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)

    submit_button = FakeElement("Submit Application")
    frame = FakeFrame(
        sensitive_url,
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    scraper = _scraper()
    scraper._submission_ledger = ledger
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    submitted = await scraper._confirm_and_submit(
        FakePage([frame], url=sensitive_url), job, auto_submit=True
    )

    detail = scraper.last_apply_detail
    assert submitted is False
    assert scraper.last_apply_status == "duplicate_application_prevented"
    assert submit_button.evaluate_calls == 0
    assert key not in detail
    assert "applicant" not in detail
    assert "super-secret" not in detail
    assert "private-token" not in detail


@pytest.mark.asyncio
async def test_ambiguous_submit_detail_redacts_sensitive_portal_url(
    monkeypatch, tmp_path
):
    from src.sources.adapters.idempotency import SubmissionLedger

    sensitive_url = (
        "https://applicant:super-secret@boards.greenhouse.io/acme/jobs/2"
        "?gh_jid=private-token"
    )

    async def _capture_baseline(frame):
        return frame

    async def _no_receipt(_frame, **_kwargs):
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _no_receipt)
    submit_button = FakeElement("Submit Application")
    frame = FakeFrame(
        sensitive_url,
        {"#submit_app": submit_button},
        evaluate_result=True,
    )
    scraper = _scraper()
    scraper._submission_ledger = SubmissionLedger(tmp_path / "ledger.json")
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    submitted = await scraper._confirm_and_submit(
        FakePage([frame], url=sensitive_url),
        {"job_id": "job-2", "title": "Engineer", "company": "Acme"},
        auto_submit=True,
    )

    detail = scraper.last_apply_detail
    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"
    assert "boards.greenhouse.io" in detail
    assert "applicant" not in detail
    assert "super-secret" not in detail
    assert "private-token" not in detail


@pytest.mark.asyncio
async def test_concurrent_legacy_attempts_dispatch_only_once(monkeypatch, tmp_path):
    """Two legacy workers racing the same job must share one atomic ledger claim."""
    from src.sources.adapters.idempotency import SubmissionLedger

    ledger_path = tmp_path / "apply-ledger.json"

    baseline_count = 0
    both_at_boundary = asyncio.Event()

    async def _capture_baseline(frame):
        nonlocal baseline_count
        baseline_count += 1
        if baseline_count == 2:
            both_at_boundary.set()
        await both_at_boundary.wait()
        return frame

    async def _no_receipt(_frame, **_kwargs):
        return False, ""

    monkeypatch.setattr(jobright_module, "capture_receipt_evidence", _capture_baseline)
    monkeypatch.setattr(jobright_module, "verify_receipt", _no_receipt)

    buttons = [FakeElement("Submit Application"), FakeElement("Submit Application")]
    scrapers = []
    pages = []
    for button in buttons:
        frame = FakeFrame(
            "https://boards.greenhouse.io/acme/jobs/1",
            {"#submit_app": button},
            evaluate_result=True,
        )
        scraper = _scraper()
        scraper._submission_ledger = SubmissionLedger(path=ledger_path)
        scraper._delay = _no_delay
        scraper._run_pre_submission_validation = _no_delay
        scrapers.append(scraper)
        pages.append(FakePage([frame], url=frame.url))

    await asyncio.gather(
        *(
            scraper._confirm_and_submit(
                page,
                {"title": "Engineer", "company": "Acme"},
                auto_submit=True,
            )
            for scraper, page in zip(scrapers, pages)
        )
    )

    assert sum(button.evaluate_calls for button in buttons) == 1
    statuses = {scraper.last_apply_status for scraper in scrapers}
    assert "submission_unverified" in statuses
    assert statuses & {"submit_in_progress", "submit_unverified_unresolved"}


@pytest.mark.asyncio
async def test_manual_submission_is_durably_parked_not_reported_success(
    monkeypatch, tmp_path
):
    """A user-reported click without receipt evidence remains ambiguous."""
    from src.sources.adapters.idempotency import SubmissionLedger, canonical_key

    class _TTY:
        @staticmethod
        def isatty():
            return True

    def _answer(prompt=""):
        if "Submit this application" in prompt:
            return "y"
        if "Press Enter" in prompt:
            return ""
        if "Did you click Submit" in prompt:
            return "y"
        if "successfully submit" in prompt:
            return "n"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr("builtins.input", _answer)
    monkeypatch.setattr(jobright_module.sys, "stdin", _TTY())

    frame = FakeFrame(
        "https://boards.greenhouse.io/acme/jobs/manual",
        evaluate_result=True,
    )
    page = FakePage([frame], url=frame.url)
    ledger = SubmissionLedger(tmp_path / "apply-ledger.json")
    scraper = _scraper()
    scraper._submission_ledger = ledger
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _no_submit_control(_page, _selectors):
        return None

    scraper._find_submit_control = _no_submit_control

    job = {
        "job_id": "job-manual",
        "title": "Engineer",
        "company": "Acme",
        "url": frame.url,
    }
    submitted = await scraper._confirm_and_submit(page, job, auto_submit=False)

    assert submitted is False
    assert scraper.last_apply_status == "submission_unverified"
    assert ledger.needs_reconciliation(canonical_key(job))


@pytest.mark.asyncio
async def test_manual_explicit_no_click_clears_submission_claim(monkeypatch, tmp_path):
    """Only an explicit report that no click occurred releases the key."""
    from src.sources.adapters.idempotency import SubmissionLedger, canonical_key

    class _TTY:
        @staticmethod
        def isatty():
            return True

    def _answer(prompt=""):
        if "Submit this application" in prompt:
            return "y"
        if "Press Enter" in prompt:
            return ""
        if "Did you click Submit" in prompt:
            return "n"
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr("builtins.input", _answer)
    monkeypatch.setattr(jobright_module.sys, "stdin", _TTY())
    frame = FakeFrame(
        "https://boards.greenhouse.io/acme/jobs/manual-no-click",
        evaluate_result=True,
    )
    ledger = SubmissionLedger(tmp_path / "apply-ledger.json")
    scraper = _scraper()
    scraper._submission_ledger = ledger
    scraper._delay = _no_delay
    scraper._run_pre_submission_validation = _no_delay

    async def _no_submit_control(_page, _selectors):
        return None

    scraper._find_submit_control = _no_submit_control
    job = {
        "job_id": "job-manual-no-click",
        "title": "Engineer",
        "company": "Acme",
        "url": frame.url,
    }

    submitted = await scraper._confirm_and_submit(
        FakePage([frame], url=frame.url), job, auto_submit=False
    )

    assert submitted is False
    assert scraper.last_apply_status == "submission_cancelled"
    assert ledger.record(canonical_key(job)) is None
