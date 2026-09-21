"""ACES-399 — forensic evidence capture: phase ordering, the strict allowlist
sanitizer, bounded page probes, and the "capture must never alter an attempt"
guarantee.

No browser needed: FakePage/FakeRunLog stand in for Playwright/RunLog exactly
like the existing adapter tests (see tests/test_ats_generic_adapter.py).
"""
from __future__ import annotations

import pytest

from src.sources.adapters.attempt import AttemptPhase, rank
from src.sources.adapters import forensics
from src.sources.adapters.context import AtsApplyContext
from src.sources.adapters.generic import GenericAtsAdapter
from src.events import RunLog, read_run


# --------------------------------------------------------------------------- #
# AttemptPhase — ENTRY_CTA_FOUND rank/order
# --------------------------------------------------------------------------- #

def test_entry_cta_found_exists_between_started_and_form_reached():
    assert rank(AttemptPhase.STARTED) < rank(AttemptPhase.ENTRY_CTA_FOUND)
    assert rank(AttemptPhase.ENTRY_CTA_FOUND) < rank(AttemptPhase.FORM_REACHED)


def test_full_success_path_ranks_strictly_increasing():
    ordered = [
        AttemptPhase.STARTED,
        AttemptPhase.ENTRY_CTA_FOUND,
        AttemptPhase.FORM_REACHED,
        AttemptPhase.FIELDS_FILLED,
        AttemptPhase.SUBMIT_AUTHORIZED,
        AttemptPhase.SUBMIT_CLICKED,
        AttemptPhase.RECEIPT_VERIFIED,
    ]
    ranks = [rank(p) for p in ordered]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # no two phases share a rank


def test_terminal_phases_are_not_forward_progress():
    assert rank(AttemptPhase.FAILED) < rank(AttemptPhase.STARTED)
    assert rank(AttemptPhase.UNKNOWN) < rank(AttemptPhase.STARTED)


def test_submit_control_found_was_not_added_as_a_phase():
    # ACES-399 explicitly keeps this as evidence on an existing phase event,
    # never a new AttemptPhase member.
    assert not hasattr(AttemptPhase, "SUBMIT_CONTROL_FOUND")
    assert "submit_control_found" not in {p.value for p in AttemptPhase}


# --------------------------------------------------------------------------- #
# sanitize_forensic_fields — strict allowlist
# --------------------------------------------------------------------------- #

def test_only_allowlisted_keys_survive():
    out = forensics.sanitize_forensic_fields({
        "attempt_id": "abc123",
        "phase": "form_reached",
        "raw_html": "<form>...</form>",
        "response_body": "{'secret': 1}",
        "some_unexpected_field": "x",
    })
    assert set(out) <= forensics.ALLOWED_FORENSIC_FIELDS
    assert "raw_html" not in out
    assert "response_body" not in out
    assert "some_unexpected_field" not in out
    assert out["attempt_id"] == "abc123"
    assert out["phase"] == "form_reached"


@pytest.mark.parametrize("field", ["attempt_id", "job_id"])
def test_email_cannot_enter_an_id_field(field):
    out = forensics.sanitize_forensic_fields({field: "alice@example.com"})
    assert field not in out


@pytest.mark.parametrize("field", ["attempt_id", "job_id"])
def test_phone_like_string_cannot_enter_an_id_field(field):
    for phone in ("+1 (555) 123-4567", "555.123.4567 x2", "(555) 123-4567"):
        out = forensics.sanitize_forensic_fields({field: phone})
        assert field not in out, f"{phone!r} should have been rejected"


@pytest.mark.parametrize("field", ["attempt_id", "job_id", "source", "vendor", "adapter"])
def test_url_with_query_string_cannot_enter_an_id_or_token_field(field):
    out = forensics.sanitize_forensic_fields({
        field: "https://boards.greenhouse.io/acme/jobs/1?utm_source=alice@example.com",
    })
    assert field not in out


def test_host_field_rejects_a_full_url_with_query_string():
    out = forensics.sanitize_forensic_fields({
        "host": "https://boards.greenhouse.io/apply?email=alice@example.com",
    })
    assert "host" not in out


def test_host_field_accepts_a_bare_hostname():
    out = forensics.sanitize_forensic_fields({"host": "boards.greenhouse.io"})
    assert out["host"] == "boards.greenhouse.io"


def test_host_field_rejects_embedded_credentials():
    out = forensics.sanitize_forensic_fields({"host": "user:pass@evil.example.com"})
    assert "host" not in out


def test_control_kinds_drops_arbitrary_labels_and_caps_at_ten():
    out = forensics.sanitize_forensic_fields({
        "control_kinds": [
            "text_input", "First Name", "alice@example.com", "select",
            "checkbox", "radio", "file_input", "password_input", "textarea",
            "text_input",  # duplicate — deduped
            "<script>alert(1)</script>", "arbitrary label text",
        ] * 3,
    })
    assert out["control_kinds"] == [
        "text_input", "select", "checkbox", "radio", "file_input",
        "password_input", "textarea",
    ]
    assert len(out["control_kinds"]) <= 10
    assert all(k in forensics._CONTROL_KIND_VALUES for k in out["control_kinds"])
    assert "First Name" not in out["control_kinds"]
    assert "alice@example.com" not in out["control_kinds"]
    assert "<script>alert(1)</script>" not in out["control_kinds"]


@pytest.mark.parametrize("field,bad_value", [
    ("path_class", "/etc/passwd"),
    ("auth_state", "definitely_authenticated"),
    ("http_class", "200 OK <script>"),
    ("failure_reason_code", "the user's resume was rejected because ..."),
])
def test_enum_fields_reject_out_of_vocabulary_values(field, bad_value):
    out = forensics.sanitize_forensic_fields({field: bad_value})
    assert field not in out


def test_boolean_fields_never_pass_through_arbitrary_strings():
    out = forensics.sanitize_forensic_fields({
        "form_present": "yes definitely, and by the way my email is a@b.com",
        "capture_incomplete": "true",
    })
    # Non-bool/int input coerces to a safe default, never the raw string.
    assert out["form_present"] is False
    assert out["capture_incomplete"] is False


def test_redirect_count_is_clamped_to_a_bounded_range():
    assert forensics.sanitize_forensic_fields({"redirect_count": 999})["redirect_count"] == 50
    assert forensics.sanitize_forensic_fields({"redirect_count": -5})["redirect_count"] == 0
    assert forensics.sanitize_forensic_fields({"redirect_count": "not a number"})["redirect_count"] == 0


def test_sanitizer_never_raises_on_hostile_input():
    hostile = {
        "attempt_id": object(),
        "control_kinds": "not-a-list",
        "form_present": {"nested": "dict"},
        "phase": None,
        "redirect_count": [1, 2, 3],
        "capture_error_class": "a" * 500,
    }
    out = forensics.sanitize_forensic_fields(hostile)  # must not raise
    assert isinstance(out, dict)
    assert set(out) <= forensics.ALLOWED_FORENSIC_FIELDS


def test_sanitizer_rejects_non_dict_input_without_raising():
    assert forensics.sanitize_forensic_fields("not a dict") == {}
    assert forensics.sanitize_forensic_fields(None) == {}


# --------------------------------------------------------------------------- #
# URL/path/http helpers
# --------------------------------------------------------------------------- #

def test_host_of_strips_credentials_path_and_query():
    assert forensics.host_of("https://user:pass@boards.greenhouse.io/acme/jobs/1?x=1") == "boards.greenhouse.io"
    assert forensics.host_of("not a url") == ""
    assert forensics.host_of("") == ""


def test_path_class_of_recognizes_the_bounded_enum():
    assert forensics.path_class_of("https://x.com/login") == "login"
    assert forensics.path_class_of("https://x.com/apply/step1") == "apply"
    assert forensics.path_class_of("https://x.com/jobs/123") == "job"
    assert forensics.path_class_of("https://x.com/thank-you") == "confirmation"
    assert forensics.path_class_of("https://x.com/whatever") == "unknown"


def test_http_class_of_maps_status_codes():
    assert forensics.http_class_of(200) == "2xx"
    assert forensics.http_class_of(302) == "3xx"
    assert forensics.http_class_of(404) == "4xx"
    assert forensics.http_class_of(500) == "5xx"
    assert forensics.http_class_of(None) == "none"
    assert forensics.http_class_of("nope") == "unknown"


def test_redirect_count_of_handles_none_response():
    assert forensics.redirect_count_of(None) == 0


class _FakeRequest:
    def __init__(self, prev=None):
        self.redirected_from = prev


class _FakeResponse:
    def __init__(self, request):
        self.request = request


def test_redirect_count_of_walks_the_chain():
    # 4 chained requests = 3 redirect hops before the final response.
    chain = None
    for _ in range(4):
        chain = _FakeRequest(chain)
    resp = _FakeResponse(chain)
    assert forensics.redirect_count_of(resp) == 3


# --------------------------------------------------------------------------- #
# probe_page_evidence — bounded, read-only, timeout-safe
# --------------------------------------------------------------------------- #

class _EvalPage:
    def __init__(self, result=None, raise_exc=None, hang=False):
        self._result = result
        self._raise = raise_exc
        self._hang = hang

    async def evaluate(self, script, *args):
        if self._hang:
            import asyncio
            await asyncio.sleep(10)
        if self._raise:
            raise self._raise
        return self._result


@pytest.mark.asyncio
async def test_probe_page_evidence_normal_result():
    page = _EvalPage(result={
        "captcha": False, "password": False, "loginText": False,
        "kinds": ["text", "email", "select-one", "checkbox", "weirdo-type"],
        "formPresent": True, "submitPresent": True, "invalidPresent": False,
    })
    out = await forensics.probe_page_evidence(page)
    assert out["form_present"] is True
    assert out["submit_control_present"] is True
    assert out["validation_errors_present"] is False
    assert out["auth_state"] == "logged_in"
    assert "weirdo-type" not in out["control_kinds"]
    assert set(out["control_kinds"]) <= forensics._CONTROL_KIND_VALUES


@pytest.mark.asyncio
async def test_probe_page_evidence_detects_bot_challenge():
    page = _EvalPage(result={"captcha": True, "password": False, "loginText": False,
                             "kinds": [], "formPresent": False, "submitPresent": False,
                             "invalidPresent": False})
    out = await forensics.probe_page_evidence(page)
    assert out["auth_state"] == "bot_challenge"


@pytest.mark.asyncio
async def test_probe_page_evidence_detects_signin_redirect():
    page = _EvalPage(result={"captcha": False, "password": True, "loginText": True,
                             "kinds": [], "formPresent": False, "submitPresent": False,
                             "invalidPresent": False})
    out = await forensics.probe_page_evidence(page)
    assert out["auth_state"] == "redirected_to_signin"


@pytest.mark.asyncio
async def test_probe_page_evidence_swallows_exceptions():
    out = await forensics.probe_page_evidence(_EvalPage(raise_exc=RuntimeError("boom")))
    assert out["capture_incomplete"] is True
    assert out["capture_error_class"] == "RuntimeError"
    assert out["form_present"] is False


@pytest.mark.asyncio
async def test_probe_page_evidence_times_out_gracefully():
    out = await forensics.probe_page_evidence(_EvalPage(hang=True), timeout=0.05)
    assert out["capture_incomplete"] is True


@pytest.mark.asyncio
async def test_probe_page_evidence_handles_none_result():
    out = await forensics.probe_page_evidence(_EvalPage(result=None))
    assert out["capture_incomplete"] is True


# --------------------------------------------------------------------------- #
# emit_forensic_phase — writes through RunLog, never raises
# --------------------------------------------------------------------------- #

def test_emit_forensic_phase_writes_only_allowlisted_fields(tmp_path):
    log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    forensics.emit_forensic_phase(
        log, attempt_id="a1", job_id="j1", phase="form_reached",
        source="linkedin", vendor="greenhouse", adapter="greenhouse",
        host="boards.greenhouse.io", path_class="apply", auth_state="logged_in",
        form_present=True, submit_control_present=False,
        validation_errors_present=False, control_kinds=["text_input"],
        http_class="2xx", redirect_count=0, capture_incomplete=False,
        # adversarial extras that must never make it into the JSONL line:
        raw_html="<form>secret</form>", cookie="session=xyz",
    )
    events = read_run(log.run_id, runs_dir=log.dir)
    assert len(events) == 1
    rec = events[0]
    assert rec["event"] == "forensic_phase"
    known_envelope = {"schema_version", "run_id", "ts", "agent", "event"}
    assert set(rec) - known_envelope <= forensics.ALLOWED_FORENSIC_FIELDS
    assert "raw_html" not in rec
    assert "cookie" not in rec


def test_emit_forensic_phase_no_ops_on_none_run_log():
    assert forensics.emit_forensic_phase(None, attempt_id="a1", phase="started") is None


class _BoomRunLog:
    def emit(self, *a, **kw):
        raise RuntimeError("RunLog is on fire")


def test_emit_forensic_phase_never_raises_even_if_runlog_emit_raises():
    # Must swallow, not propagate — capture can never break the caller.
    assert forensics.emit_forensic_phase(_BoomRunLog(), attempt_id="a1", phase="started") is None


class _BoomOnAccessDict(dict):
    def items(self):
        raise RuntimeError("hostile mapping")


def test_emit_forensic_phase_never_raises_on_hostile_fields():
    log = RunLog(agent="test")
    # Passing kwargs means `fields` is always a real dict in practice, but the
    # sanitizer itself must not raise on a hostile-shaped value reaching it.
    out = forensics.sanitize_forensic_fields(_BoomOnAccessDict(attempt_id="a1"))
    assert isinstance(out, dict)


def test_emit_universal_attempt_events_never_raise():
    forensics.emit_universal_attempt_started(_BoomRunLog(), attempt_id="a", job_id="j", source="s")
    forensics.emit_universal_attempt_finished(
        _BoomRunLog(), attempt_id="a", job_id="j", source="s", status="applied", applied=True,
    )
    # no exception means the test passed; also confirm None is a safe no-op
    forensics.emit_universal_attempt_started(None, attempt_id="a", job_id="j", source="s")
    forensics.emit_universal_attempt_finished(None, attempt_id="a", job_id="j", source="s",
                                              status="applied", applied=True)


def test_emit_universal_attempt_events_round_trip(tmp_path):
    log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    forensics.emit_universal_attempt_started(log, attempt_id="a1", job_id="j1", source="usajobs")
    forensics.emit_universal_attempt_finished(
        log, attempt_id="a1", job_id="j1", source="usajobs", status="applied", applied=True,
    )
    events = read_run(log.run_id, runs_dir=log.dir)
    started = [e for e in events if e["event"] == "apply_attempt_started"]
    finished = [e for e in events if e["event"] == "apply_attempt_finished"]
    assert len(started) == 1 and len(finished) == 1
    assert started[0]["attempt_id"] == finished[0]["attempt_id"] == "a1"
    assert started[0]["rich_evidence_available"] is False
    assert finished[0]["rich_evidence_available"] is False
    assert finished[0]["status"] == "applied"
    assert finished[0]["applied"] is True


# --------------------------------------------------------------------------- #
# Capture failure must never alter the adapter's real result
# --------------------------------------------------------------------------- #

class _FakeElement:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    async def fill(self, value):
        self._page.filled[self._selector] = value

    async def click(self):
        self._page.clicked.append(self._selector)
        self._page._submit_dispatched = True


class _FakeAtsPage:
    """Mirrors tests/test_ats_generic_adapter.py's FakePage, plus a
    always-raising evaluate() branch for the ACES-399 probe sentinel so this
    test can prove a broken probe never changes the apply outcome."""

    def __init__(self, present_selectors, receipt_result=None, blow_up_probe=False):
        self._present = set(present_selectors)
        self.filled = {}
        self.clicked = []
        self.uploaded = {}
        self.url = "https://boards.greenhouse.io/acme/jobs/1"
        self._receipt_result = receipt_result
        self._submit_dispatched = False
        self._blow_up_probe = blow_up_probe

    async def query_selector(self, sel):
        return _FakeElement(self, sel) if sel in self._present else None

    async def set_input_files(self, sel, path):
        if sel not in self._present:
            raise RuntimeError("no file input")
        self.uploaded[sel] = path

    async def evaluate(self, script, *args):
        if "aces-399 forensic-probe harness" in script and self._blow_up_probe:
            raise RuntimeError("probe boom")
        if "captcha" in script:
            return None
        if "label" in script:
            return []
        visible_receipt = self._receipt_result if self._submit_dispatched else None
        if "sentinel: acceptance-matcher harness" in script:
            return visible_receipt
        if "thank you for" in script:
            return visible_receipt
        if "sentinel: acceptance-count harness" in script:
            return 1 if visible_receipt else 0
        return None


class _BoomOnEmitRunLog:
    """A run_log whose emit() always raises — proves a forensic-capture crash
    cannot change what the adapter returns."""

    def emit(self, *a, **kw):
        raise RuntimeError("emit boom")


@pytest.mark.asyncio
async def test_capture_failure_does_not_alter_adapter_result():
    selectors = {
        "input[name*='email' i], input[id*='email' i]",
        "#submit_app",
    }

    def _ctx(page, run_log=None):
        return AtsApplyContext(
            page=page, job={"url": page.url, "job_id": "j1", "source": "linkedin"},
            profile={"personal_info": {"email": "ada@example.com"}},
            auto_submit=True, url=page.url, attempt_id="a1", run_log=run_log,
        )

    baseline_page = _FakeAtsPage(selectors, receipt_result="t:thank you for applying")
    baseline = await GenericAtsAdapter().apply(_ctx(baseline_page, run_log=None))

    boom_page = _FakeAtsPage(selectors, receipt_result="t:thank you for applying",
                             blow_up_probe=True)
    with_broken_capture = await GenericAtsAdapter().apply(
        _ctx(boom_page, run_log=_BoomOnEmitRunLog())
    )

    assert baseline.submitted == with_broken_capture.submitted is True
    assert baseline.verified == with_broken_capture.verified is True
    assert baseline.status == with_broken_capture.status == "applied"
    assert boom_page.clicked == baseline_page.clicked == ["#submit_app"]


# --------------------------------------------------------------------------- #
# Copilot review (PR #138) regression: _emit_forensic must use the LIVE page
# URL, not the stale ctx.url snapshot taken before a CTA click / vendor
# rewrite navigates further (context.py's own docstring calls ctx.url "a
# convenience mirror of page.url at pick time" — it is not kept in sync).
# --------------------------------------------------------------------------- #

class _StaleUrlPage:
    """Simulates a page that has navigated past the ctx.url snapshot."""
    url = "https://jobs.example.com/apply/after-cta-handoff"


def test_emit_forensic_prefers_live_page_url_over_stale_ctx_snapshot(tmp_path):
    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    ctx = AtsApplyContext(
        page=_StaleUrlPage(), job={"job_id": "j1", "source": "test"}, profile=None,
        auto_submit=True, url="https://careers.example.com/job/123",  # stale snapshot
        attempt_id="a1", run_log=run_log,
    )
    GenericAtsAdapter()._emit_forensic(ctx, AttemptPhase.FORM_REACHED, "example")

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    evt = next(e for e in events if e["event"] == "forensic_phase")
    assert evt["host"] == "jobs.example.com"
    assert evt["host"] != "careers.example.com"


def test_emit_forensic_falls_back_to_ctx_url_when_page_has_none(tmp_path):
    class _NoUrlPage:
        pass

    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    ctx = AtsApplyContext(
        page=_NoUrlPage(), job={"job_id": "j1", "source": "test"}, profile=None,
        auto_submit=True, url="https://careers.example.com/job/123",
        attempt_id="a1", run_log=run_log,
    )
    GenericAtsAdapter()._emit_forensic(ctx, AttemptPhase.FORM_REACHED, "example")

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    evt = next(e for e in events if e["event"] == "forensic_phase")
    assert evt["host"] == "careers.example.com"
