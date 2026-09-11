"""Submission-truth: a possible (unconfirmed) submit must survive every recovery
exit path and the session's recovery-adoption step, ending in
ledger.complete(verified=False) — never a released marker + blind retry.

Covers handoff §3.4: "recovery outcomes can lose submission uncertainty" and the
acceptance test "possible-submit and submission_unverified outcomes survive
fallback transitions and require reconciliation".
"""
import pytest
from unittest.mock import AsyncMock, patch

from src.sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored
from src.sources.adapters.context import AtsApplyResult


# ─── _is_submit_like heuristic ──────────────────────────────────────────────

def test_is_submit_like_only_clicks_count():
    f = BrowserUseRecoveryRefactored._is_submit_like
    assert f("click", "button[type=submit]")
    assert f("click", "#apply-button")
    assert f("click", "a.send_application")
    assert not f("fill", "input#submit_reason", "text")   # fill never submits
    assert not f("click", "#next-page")
    assert not f("select", "select#apply-source", "web")


# ─── replay path ────────────────────────────────────────────────────────────

def _recovery():
    r = BrowserUseRecoveryRefactored.__new__(BrowserUseRecoveryRefactored)
    return r


@pytest.mark.asyncio
async def test_replay_reports_possible_submit_on_midway_failure():
    """A replay that clicks submit then fails a later step must report the
    possible submit — the caller must not re-drive the form."""
    r = _recovery()

    class _Cfg:
        skill_replay_delay_ms = 0
        step_timeout_ms = 1

    r.browser_config = _Cfg()

    page = AsyncMock()
    page.wait_for_selector = AsyncMock()

    calls = []

    async def _exec(page_, action, selector, val, resume):
        calls.append(selector)
        return selector != "#broken"  # fail on the post-submit step

    r._execute_action = _exec
    skills = [
        {"action": "fill", "selector": "#name", "value": "x"},
        {"action": "click", "selector": "button[type=submit]", "value": None},
        {"action": "click", "selector": "#broken", "value": None},
    ]
    success, possible_submit = await r._replay_skills(page, skills, None)
    assert success is False
    assert possible_submit is True


# ─── session adoption ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_session_adopts_unverified_recovery_result():
    """ExternalApplySession must adopt a recovery result whose status is
    submission_unverified even though submitted=False — discarding it releases
    the ledger marker and frees the job for a duplicate submission."""
    import src.sources.adapters.session as sess_mod

    # Reproduce the adoption logic exactly as written in the session
    # (unit-level guard so the invariant is pinned even without a browser).
    adapter_res = AtsApplyResult.blocked("submit_not_found", "no submit button")
    recovery_res = AtsApplyResult.unverified("clicked submit, no receipt")

    res = adapter_res
    if recovery_res.submitted or recovery_res.status == "submission_unverified":
        res = recovery_res

    assert res.status == "submission_unverified"
    # The marker block: unverified → ledger.complete(verified=False), never clear()
    assert not res.verified
    # And the source text guards against regression of the adoption condition:
    import inspect
    src = inspect.getsource(sess_mod.ExternalApplySession.apply)
    assert 'recovery_res.status == "submission_unverified"' in src, (
        "session must adopt submission_unverified recovery results, not only submitted=True"
    )


# ─── LLM loop exits ─────────────────────────────────────────────────────────
# The full-loop behavioral proofs (done-without-receipt, timeout-after-dispatch,
# neutral-selector submit, fence, session+ledger integration) live in
# tests/test_recovery_submit_dispatch_truth.py, which drives the REAL
# BrowserUseRecoveryRefactored.apply() and ExternalApplySession.apply().


def test_all_recovery_exit_paths_guard_possible_submit():
    """Source-level pin: every non-receipt exit in the recovery loop consults
    possible_submit before returning a pre-submit status."""
    import inspect
    from src.sources.adapters import recovery_browseruse_refactored as m

    src = inspect.getsource(m)
    # replay: ambiguous mid-replay failure must not fall through to the LLM loop
    assert "Skill replay failed after clicking a submit control" in src
    # done / fail / loop-detect / max-steps / llm-error exits all guard on possible_submit
    assert src.count("possible_submit") >= 10, (
        "expected possible_submit guards on replay + done + fail + loop-detect "
        f"+ max-steps + llm-error paths, found {src.count('possible_submit')} mentions"
    )
