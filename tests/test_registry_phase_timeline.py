"""ACES-399 — registry-path (ExternalApplySession) rich forensic evidence.

End-to-end through ExternalApplySession.apply() -> AtsAdapterRegistry ->
MicrosoftAdapter (a CtaApplyAdapter, so this also exercises ENTRY_CTA_FOUND) ->
GenericAtsAdapter's shared _gated_submit -> receipt verification. No browser:
FakePage is modeled directly on tests/test_vendor_cta_adapters.py's FakePage
(same evaluate() dispatch-by-script-signature approach), with the ACES-399
probe's sentinel checked first since its script text also contains the
substring "captcha" that the generic blocker probe already claims.
"""
from __future__ import annotations

import pytest

from src.events import RunLog, read_run
from src.sources.adapters.context import AtsApplyContext
from src.sources.adapters.idempotency import SubmissionLedger
from src.sources.adapters.registry import AtsAdapterRegistry
from src.sources.adapters.session import ExternalApplySession
from src.sources.adapters.vendor_cta import MicrosoftAdapter

URL = "https://careers.microsoft.com/us/en/job/123/SWE"


class _El:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    async def fill(self, v):
        self.page.filled[self.sel] = v

    async def click(self):
        self.page.clicks.append(self.sel)
        self.page._submit_dispatched = True


class FakePage:
    def __init__(self, url, cta_clicked=True, has_form_after_cta=True,
                 receipt=None, present=()):
        self.url = url
        self.cta_clicked = cta_clicked
        self.has_form_after_cta = has_form_after_cta
        self.receipt = receipt
        self.present = set(present)
        self.goto_urls = []
        self.filled, self.clicks = {}, []
        self._submit_dispatched = False
        self._form_probe_calls = 0

    async def goto(self, url, **kw):
        self.goto_urls.append(url)
        self.url = url

    async def evaluate(self, script, *args):
        # ACES-399 rich-evidence probe — checked FIRST: it shares the word
        # "captcha" with the generic blocker probe below.
        if "aces-399 forensic-probe harness" in script:
            return {
                "captcha": False, "password": False, "loginText": False,
                "kinds": ["text", "email"], "formPresent": False,
                "submitPresent": False, "invalidPresent": False,
            }
        if "forgot password" in script:            # vendor_cta login wall
            return False
        if "regexes" in script:                     # vendor_cta CTA click
            return self.cta_clicked
        if "applicant fields" in script:             # vendor_cta form probe
            # False before the CTA click, True once we've "entered" the flow.
            self._form_probe_calls += 1
            return self.has_form_after_cta if self._form_probe_calls > 1 else False
        if "captcha" in script:                       # generic blocker probe
            return None
        if "label" in script:                         # generic questions
            return []
        visible_receipt = self.receipt if self._submit_dispatched else None
        if "sentinel: acceptance-matcher harness" in script:
            return visible_receipt
        if "thank you for" in script:
            return visible_receipt
        if "sentinel: acceptance-count harness" in script:
            return 1 if visible_receipt else 0
        return None

    async def query_selector(self, sel):
        return _El(self, sel) if sel in self.present else None

    async def set_input_files(self, sel, path):
        pass


def _make_session(tmp_path, monkeypatch, page):
    reg = AtsAdapterRegistry(fallback=MicrosoftAdapter())
    reg.register(MicrosoftAdapter())
    ledger = SubmissionLedger(tmp_path / "ledger.json")
    run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    sess = ExternalApplySession({}, registry=reg, ledger=ledger, run_log=run_log)
    sess._closed = False

    async def _start(load_extensions=False, disable_extensions=False):
        return page

    async def _close(save_session=True):
        sess._closed = True

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", _close)
    prof = tmp_path / "jobright_profile"
    prof.mkdir(exist_ok=True)
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    # Browser Use self-healing recovery (session.py's fallback on
    # submit_not_found/form_not_reached/blocked) is unrelated to ACES-399 and
    # needs a much richer Page API (locator(), query_selector_all(), an LLM
    # client, ...) than this FakePage provides. Replace it with a stub that
    # declines without touching the page, so `res` stays exactly what the
    # adapter under test produced.
    import src.sources.adapters.recovery_browseruse_refactored as _recovery_mod
    from src.sources.adapters.context import AtsApplyResult

    class _NoOpRecovery:
        async def apply(self, ctx):
            return AtsApplyResult.blocked("recovery_disabled_for_test", "skipped in test")

    monkeypatch.setattr(_recovery_mod, "BrowserUseRecoveryRefactored", _NoOpRecovery)
    return sess, run_log


# Must match adapters_patterns/ats_selectors.py's "microsoft" entry exactly —
# GenericAtsAdapter._first_selector() queries these selector strings verbatim.
PRESENT_SELECTORS = {
    "input[type='email'], input[aria-label*='Email' i], input[name*='email' i]",
    "button[aria-label*='Submit' i]",
}


@pytest.mark.asyncio
async def test_registry_path_produces_the_expected_phase_timeline(tmp_path, monkeypatch):
    page = FakePage(URL, cta_clicked=True, has_form_after_cta=True,
                    receipt="t:thank you for applying", present=PRESENT_SELECTORS)
    sess, run_log = _make_session(tmp_path, monkeypatch, page)

    job = {"job_id": "j1", "source": "linkedin", "url": URL}
    res = await sess.apply(job, auto_submit=True)

    assert res.submitted is True and res.verified is True and res.status == "applied"

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    plain_phase_order = [e["phase"] for e in events if e.get("event") in
                         ("attempt_started", "form_reached", "adapter_selected", "attempt_finished")]
    assert plain_phase_order[0] == "started"
    assert "form_reached" in plain_phase_order

    forensic = [e for e in events if e["event"] == "forensic_phase"]
    forensic_phases = [e["phase"] for e in forensic]
    # Every event must carry the SAME attempt_id as the plain attempt_started event.
    attempt_id = next(e["attempt_id"] for e in events if e["event"] == "attempt_started")
    assert all(e["attempt_id"] == attempt_id for e in forensic)

    assert "started" in forensic_phases  # the pre-CTA landing-page snapshot
    assert "form_reached" in forensic_phases
    assert "entry_cta_found" in forensic_phases
    assert "submit_clicked" in forensic_phases
    assert "receipt_verified" in forensic_phases
    # Copilot review (PR #138): ENTRY_CTA_FOUND must be observed before
    # FORM_REACHED too, not just before SUBMIT_CLICKED/RECEIPT_VERIFIED — the
    # confirmed-form probe now lives in GenericAtsAdapter.apply(), reached
    # only via CtaApplyAdapter's super().apply(ctx) AFTER its CTA click
    # confirms a form, so it can no longer race ahead of ENTRY_CTA_FOUND like
    # the old pre-CTA landing-page probe (now tagged STARTED, not FORM_REACHED).
    assert forensic_phases.index("entry_cta_found") < forensic_phases.index("form_reached")
    assert forensic_phases.index("entry_cta_found") < forensic_phases.index("submit_clicked")
    assert forensic_phases.index("submit_clicked") < forensic_phases.index("receipt_verified")

    submit_evt = next(e for e in forensic if e["phase"] == "submit_clicked")
    assert submit_evt["submit_control_present"] is True
    assert submit_evt["vendor"] == "microsoft"
    assert submit_evt["adapter"] == "microsoft"
    assert submit_evt["host"] == "careers.microsoft.com"

    # Every forensic event's keys are a subset of the allowlist + envelope.
    from src.sources.adapters.forensics import ALLOWED_FORENSIC_FIELDS
    envelope = {"schema_version", "run_id", "ts", "agent", "event"}
    for e in forensic:
        assert set(e) - envelope <= ALLOWED_FORENSIC_FIELDS

    # The observational classifier ran and recorded a verdict for this attempt.
    classification = [e for e in events if e["event"] == "forensic_classification"
                      and e["attempt_id"] == attempt_id]
    assert len(classification) == 1
    assert classification[0]["candidate"] == "receipt_reconciliation_candidate" \
        or classification[0]["candidate"] in {
            "browser_environment_candidate", "session_auth_candidate",
            "url_handoff_candidate", "navigation_adapter_candidate",
            "required_field_candidate", "unknown",
        }


@pytest.mark.asyncio
async def test_submit_control_absent_records_form_reached_not_submit_clicked(tmp_path, monkeypatch):
    """No submit selector ever resolves -> the phase we actually reached stays
    FORM_REACHED, with submit_control_present=False as the explaining evidence
    (ACES-399: submit_control_found is NOT its own AttemptPhase)."""
    page = FakePage(URL, cta_clicked=True, has_form_after_cta=True,
                    receipt=None, present=set())  # no submit selector present
    sess, run_log = _make_session(tmp_path, monkeypatch, page)

    job = {"job_id": "j2", "source": "linkedin", "url": URL}
    res = await sess.apply(job, auto_submit=True)
    assert res.status == "submit_not_found"

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    forensic = [e for e in events if e["event"] == "forensic_phase"]
    forensic_phases = {e["phase"] for e in forensic}
    assert "submit_clicked" not in forensic_phases
    # Two "form_reached"-phase events legitimately exist here: the confirmed-
    # form probe at the top of GenericAtsAdapter.apply() (no failure_reason_code
    # — nothing failed yet at that point) and _gated_submit's "submit control
    # was never found" evidence (which does carry it). Match the latter. (The
    # pre-CTA landing-page probe in session.py is tagged "started", not
    # "form_reached" — see the ordering fix in test_registry_path_produces_...)
    absent_evt = next(e for e in forensic if e["phase"] == "form_reached"
                      and e.get("failure_reason_code") == "submit_not_found")
    assert absent_evt["submit_control_present"] is False


@pytest.mark.asyncio
async def test_unverified_submit_yields_receipt_reconciliation_candidate(tmp_path, monkeypatch):
    page = FakePage(URL, cta_clicked=True, has_form_after_cta=True,
                    receipt=None, present=PRESENT_SELECTORS)  # click but no receipt
    sess, run_log = _make_session(tmp_path, monkeypatch, page)

    job = {"job_id": "j3", "source": "linkedin", "url": URL}
    res = await sess.apply(job, auto_submit=True)
    assert res.status == "submission_unverified"

    events = read_run(run_log.run_id, runs_dir=run_log.dir)
    forensic_phases = {e["phase"] for e in events if e["event"] == "forensic_phase"}
    assert "submit_clicked" in forensic_phases
    assert "receipt_verified" not in forensic_phases

    attempt_id = next(e["attempt_id"] for e in events if e["event"] == "attempt_started")
    classification = next(e for e in events if e["event"] == "forensic_classification"
                          and e["attempt_id"] == attempt_id)
    assert classification["candidate"] == "receipt_reconciliation_candidate"
