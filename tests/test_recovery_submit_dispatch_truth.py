"""Submission-truth at dispatch time — these tests DRIVE the real recovery
loop (BrowserUseRecoveryRefactored.apply) and the real ExternalApplySession
ledger integration with a temporary ledger file; nothing here re-implements
the logic under test.

Invariants proven:
  1. A submit-like click that fails AFTER dispatch (e.g. Playwright's click
     timing out during its post-click wait) keeps possible_submit — the loop
     may not exit with an ordinary pre-submit failure.
  2. A submitting control with a neutral selector (button#finalize with
     type=submit) is recognized via DOM evidence, not just selector keywords.
  3. Once a possible submission is unresolved, a further submit-like action is
     fenced — never dispatched — in both the LLM loop and saved-skill replay.
  4. A provable pre-dispatch failure (target absent AND click failed) stays an
     ordinary retryable failure — jobs are not frozen unnecessarily.
  5. The session adopts an unverified recovery result and persists
     PHASE_UNVERIFIED in a real ledger file that blocks a fresh process.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored
from src.sources.adapters.context import AtsApplyResult
from src.sources.adapters.idempotency import SubmissionLedger, PHASE_UNVERIFIED


# ─── harness ────────────────────────────────────────────────────────────────

class FakePage:
    """Minimal page: probe_map controls what the DOM probe reports per selector."""
    def __init__(self, probe_map=None):
        self.url = "https://jobs.example.com/apply"
        self.probe_map = probe_map or {}

    async def title(self):
        return "Apply"

    def locator(self, sel):
        return SimpleNamespace(inner_text=AsyncMock(return_value="form body text"))

    async def evaluate(self, js, arg=None):
        return self.probe_map.get(arg, "other")

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def goto(self, *a, **kw):
        return None


class _AllowPolicy:
    async def confirm_submit(self, ctx, meta):
        return True

    def authorized(self, ctx):
        return True


def _loop_cfg():
    return SimpleNamespace(
        enabled=True,
        max_steps=6,
        body_text_snippet_len=200,
        post_action_delay_ms=0,
        skill_replay_delay_ms=0,
        step_timeout_ms=10,
        success_indicators=[],
        loop_detection=SimpleNamespace(
            enabled=False, max_repeated_states=3, max_repeated_actions=3, state_hash_window=5,
        ),
        progress_metrics=SimpleNamespace(
            min_progress_threshold=0.1, recent_action_window=5,
            top_selectors_count=3, state_change_target_ratio=0.5,
        ),
    )


def _recovery(llm_responses, skills=None, exec_results=None, executed=None):
    """Build a recovery instance whose ONLY fakes are the page, the LLM, and
    the raw action executor — the loop, guards, fence, and probe logic run real.

    exec_results: dict selector -> bool (default True). executed: list that
    collects (action, selector) for every raw dispatch attempt."""
    r = BrowserUseRecoveryRefactored.__new__(BrowserUseRecoveryRefactored)
    r.browser_config = _loop_cfg()
    r.config = SimpleNamespace(
        telemetry=SimpleNamespace(track_step_efficiency=False, track_loop_events=False),
        llm_prompting=SimpleNamespace(model_task="general", temperature=0.0),
    )
    r._load_skills = lambda domain: (skills or [])
    r._save_skills = lambda domain, steps: None
    r._build_system_prompt = lambda pc: "s"
    r._build_user_prompt = lambda sd, ctx, pc: "u"
    r._clean_json_response = json.loads

    responses = iter(llm_responses)

    class _MC:
        async def complete(self, **kw):
            return next(responses)

    r.mc = _MC()

    async def _get_elements(page):
        return []

    async def _get_inputs(page):
        return ""

    r._get_interactive_elements = _get_elements
    r._get_input_values_snapshot = _get_inputs

    results = exec_results or {}
    log = executed if executed is not None else []

    async def _exec(page, action, selector, val, resume):
        log.append((action, selector))
        return results.get(selector, True)

    r._execute_action = _exec
    return r


def _ctx(page):
    return SimpleNamespace(page=page, url=page.url, resume_path=None, policy=_AllowPolicy())


NO_RECEIPT = AsyncMock(return_value=(False, ""))


# ─── 1. timeout after dispatch ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_click_failing_after_dispatch_is_unverified():
    """Submit click dispatched, then the browser op fails/times out
    (_execute_action returns False). A later 'fail' exit must be
    submission_unverified — NOT an ordinary retryable failure."""
    executed = []
    r = _recovery(
        llm_responses=[
            '{"action": "click", "selector": "button[type=submit]", "value": null}',
            '{"action": "fail", "selector": null, "value": null, "explanation": "page hung"}',
        ],
        exec_results={"button[type=submit]": False},  # simulated post-dispatch timeout
        executed=executed,
    )
    page = FakePage(probe_map={"button[type=submit]": "unknown"})
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    assert res.status == "submission_unverified"
    assert res.submitted is False and res.verified is False
    assert ("click", "button[type=submit]") in executed


# ─── 2. neutral selector, DOM says submit ───────────────────────────────────

@pytest.mark.asyncio
async def test_neutral_selector_submit_control_recognized_by_dom_probe():
    """button#finalize carries no submit keyword, but the DOM probe reports
    type=submit — 'done' with no receipt must be unverified, not review_ready."""
    r = _recovery(
        llm_responses=[
            '{"action": "click", "selector": "#finalize", "value": null}',
            '{"action": "done", "selector": null, "value": null}',
        ],
    )
    page = FakePage(probe_map={"#finalize": "submit"})
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    assert res.status == "submission_unverified"


@pytest.mark.asyncio
async def test_done_without_receipt_after_submit_click_is_unverified():
    """The real loop (not a re-implementation): submit click succeeds, LLM says
    done, receipt never verifies → submission_unverified."""
    r = _recovery(
        llm_responses=[
            '{"action": "click", "selector": "button[type=submit]", "value": null}',
            '{"action": "done", "selector": null, "value": null}',
        ],
    )
    page = FakePage()
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    assert res.status == "submission_unverified"


# ─── 3. submission fence ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_submit_click_in_loop_is_fenced_not_dispatched():
    executed = []
    r = _recovery(
        llm_responses=[
            '{"action": "click", "selector": "button[type=submit]", "value": null}',
            '{"action": "click", "selector": "#finalize", "value": null}',
        ],
        executed=executed,
    )
    page = FakePage(probe_map={"#finalize": "submit"})
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    assert res.status == "submission_unverified"
    dispatched_clicks = [s for a, s in executed if a == "click"]
    assert dispatched_clicks == ["button[type=submit]"], (
        f"second submit-like click must never dispatch, got {dispatched_clicks}"
    )


@pytest.mark.asyncio
async def test_replay_with_two_submit_actions_fences_the_second():
    executed = []
    skills = [
        {"action": "fill", "selector": "#name", "value": "x"},
        {"action": "click", "selector": "button[type=submit]", "value": None},
        {"action": "click", "selector": "#confirm-apply", "value": None},
    ]
    r = _recovery(llm_responses=[], skills=skills, executed=executed)
    page = FakePage()
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    # replay stopped at the fence and the ambiguity propagated out of apply()
    assert res.status == "submission_unverified"
    dispatched_clicks = [s for a, s in executed if a == "click"]
    assert dispatched_clicks == ["button[type=submit]"]


# ─── 4. provable pre-dispatch failure stays retryable ───────────────────────

@pytest.mark.asyncio
async def test_absent_submit_target_click_failure_is_not_frozen():
    """DOM probe proves the target doesn't exist AND the click failed →
    nothing was submitted; a later 'fail' stays an ordinary retryable status."""
    r = _recovery(
        llm_responses=[
            '{"action": "click", "selector": "button[type=submit]", "value": null}',
            '{"action": "fail", "selector": null, "value": null, "explanation": "no submit"}',
        ],
        exec_results={"button[type=submit]": False},
    )
    page = FakePage(probe_map={"button[type=submit]": "absent"})
    with patch("src.sources.adapters.recovery_browseruse_refactored.verify_receipt", new=NO_RECEIPT):
        res = await r.apply(_ctx(page))
    assert res.status == "submit_not_found"


# ─── 5. session + real temporary ledger ─────────────────────────────────────

def _session(tmp_path, recovery_result):
    from src.sources.adapters.session import ExternalApplySession

    class _Session(ExternalApplySession):
        # shadow the read-only BaseScraper property so the test can point the
        # (stubbed) profile lock at a temp dir
        _profile_dir = None

    s = _Session.__new__(_Session)
    s.ledger = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    s.run_log = MagicMock()
    s._policy_override = None
    s._maybe_notify = lambda *a, **kw: None

    async def _route_auth(*a, **kw):
        return None

    s._route_auth = _route_auth

    page = FakePage()

    async def _start_browser(**kw):
        return page

    async def _close_browser():
        return None

    s._start_browser = _start_browser
    s._close_browser = _close_browser
    s._profile_dir = tmp_path / "profile"

    class _Adapter:
        name = "stub"

        async def apply(self, ctx):
            return AtsApplyResult.blocked("submit_not_found", "no submit control found")

    class _Registry:
        async def pick(self, ctx):
            return _Adapter()

    s.registry = _Registry()

    class _Recovery:
        async def apply(self, ctx):
            return recovery_result

    class _Lock:
        def __init__(self, *a, **kw): ...

        async def acquire_async(self):
            return self

        def release(self): ...

    return s, _Recovery, _Lock


@pytest.mark.asyncio
async def test_session_persists_unverified_recovery_in_real_ledger(tmp_path):
    s, _Recovery, _Lock = _session(
        tmp_path, AtsApplyResult.unverified("clicked submit, receipt never confirmed"))
    job = {"url": "https://jobs.example.com/apply/123", "job_id": "j1"}

    with patch("src.sources.adapters.session.ProfileLock", _Lock), \
         patch("src.sources.adapters.recovery_browseruse_refactored.BrowserUseRecoveryRefactored", _Recovery):
        res = await s.apply(job, auto_submit=True)

    assert res.status == "submission_unverified"

    # the ambiguity is durable: a FRESH ledger instance (fresh process) reading
    # the same file must block a blind retry
    from src.sources.adapters.idempotency import canonical_key
    key = canonical_key(job)
    fresh = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    rec = fresh.record(key)
    assert rec and rec["phase"] == PHASE_UNVERIFIED
    assert fresh.needs_reconciliation(key)

    s2, _Recovery2, _Lock2 = _session(tmp_path, AtsApplyResult.unverified("x"))
    with patch("src.sources.adapters.session.ProfileLock", _Lock2), \
         patch("src.sources.adapters.recovery_browseruse_refactored.BrowserUseRecoveryRefactored", _Recovery2):
        res2 = await s2.apply(job, auto_submit=True)
    assert res2.status == "submit_unverified_unresolved"


@pytest.mark.asyncio
async def test_session_clears_marker_on_genuine_pre_submit_failure(tmp_path):
    """A recovery result that provably never submitted must release the marker
    so the job stays retryable — no permanent false hold."""
    s, _Recovery, _Lock = _session(
        tmp_path, AtsApplyResult.blocked("submit_not_found", "nothing clicked"))
    job = {"url": "https://jobs.example.com/apply/456", "job_id": "j2"}

    with patch("src.sources.adapters.session.ProfileLock", _Lock), \
         patch("src.sources.adapters.recovery_browseruse_refactored.BrowserUseRecoveryRefactored", _Recovery):
        res = await s.apply(job, auto_submit=True)

    assert res.status == "submit_not_found"
    from src.sources.adapters.idempotency import canonical_key
    fresh = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    assert fresh.record(canonical_key(job)) is None
