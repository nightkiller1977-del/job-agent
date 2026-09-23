"""Circuit re-arm: apply_attempt_count is otherwise a one-way door.

should_attempt() opens the circuit at the cap and record_apply_attempt() only
ever increments, so a capped job is skipped forever — including after the fix
that would have made it succeed. These cover the two signals that re-arm it
(apply-path code changed / environmental blocker aged out), the classes that
must never re-arm, and the persistence contract.
"""
import json
from datetime import datetime, timedelta

import pytest

from src.blocker_classifier import (
    BlockerClass,
    Rearm,
    apply_path_fingerprint,
    classify,
    rearm_reason,
    should_attempt,
)
from src.state_manager import StateManager


@pytest.fixture
def sm(tmp_path):
    mgr = StateManager(db_path=str(tmp_path / "jobs.db"))
    yield mgr
    mgr.close()


def _capped(status: str, *, fingerprint: str = "old0000000000000", attempts: int = 9, at=None) -> dict:
    """An extra_json blob for a job that has exhausted its budget on *status*."""
    return {
        "apply_last_status": status,
        "apply_attempt_count": attempts,
        "apply_code_fingerprint": fingerprint,
        "apply_last_attempt": (at or datetime.utcnow()).isoformat(),
    }


# ─── code-change re-arm ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "status",
    ["submit_not_found", "form_not_reached", "browser_timeout", "workday_session_expired", "captcha"],
)
def test_code_change_rearms_every_fixable_class(status):
    verdict = rearm_reason(_capped(status), current_fingerprint="new1111111111111")
    assert verdict and verdict.kind == Rearm.CODE_CHANGE
    assert "code changed" in verdict.reason


def test_unchanged_code_does_not_rearm():
    fp = "same222222222222"
    assert rearm_reason(_capped("submit_not_found", fingerprint=fp), current_fingerprint=fp) is None


def test_missing_fingerprint_is_not_treated_as_a_change():
    """Rows predating the field must not stampede the whole backlog through an
    unverified code path on the first run after deploy."""
    extra = _capped("submit_not_found")
    del extra["apply_code_fingerprint"]
    assert rearm_reason(extra, current_fingerprint="new1111111111111") is None


def test_undeterminable_current_fingerprint_does_not_rearm():
    assert rearm_reason(_capped("submit_not_found"), current_fingerprint="") is None


# ─── classes that must never re-arm ────────────────────────────────────────

@pytest.mark.parametrize("status", ["expired", "bad_ats_url", "unknown_source", "credentials_missing"])
def test_permanent_blockers_never_rearm(status):
    """A closed posting does not reopen because we edited code."""
    assert classify(status) is BlockerClass.PERMANENT
    old = datetime.utcnow() - timedelta(days=90)
    assert rearm_reason(_capped(status, at=old), current_fingerprint="new1111111111111") is None


def test_applied_never_rearms():
    """Re-arming a success would re-submit an application already sent."""
    assert rearm_reason(_capped("applied"), current_fingerprint="new1111111111111") is None


def test_never_attempted_job_has_no_circuit_to_rearm():
    assert rearm_reason({}, current_fingerprint="new1111111111111") is None


def test_job_with_budget_left_is_not_rearmed():
    extra = _capped("browser_timeout", attempts=0)
    assert rearm_reason(extra, current_fingerprint="new1111111111111") is None


# ─── cooldown re-arm ───────────────────────────────────────────────────────

def test_environmental_blocker_rearms_after_cooldown(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    stale = datetime.utcnow() - timedelta(hours=30)
    verdict = rearm_reason(_capped("browser_timeout", fingerprint=fp, at=stale), current_fingerprint=fp)
    assert verdict and verdict.kind == Rearm.COOLDOWN
    assert "cooldown" in verdict.reason


def test_environmental_blocker_holds_inside_cooldown(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    recent = datetime.utcnow() - timedelta(hours=2)
    assert rearm_reason(_capped("browser_timeout", fingerprint=fp, at=recent), current_fingerprint=fp) is None


def test_needs_human_gets_its_decay_retry(monkeypatch):
    """ACES-284 acceptance: a submit_not_found job is retried once after the
    configured decay with no human action. 48 approved jobs aged out to expired
    without a single retry after one breaker trip."""
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    stale = datetime.utcnow() - timedelta(days=3)
    verdict = rearm_reason(_capped("submit_not_found", fingerprint=fp, at=stale), current_fingerprint=fp)
    assert verdict and verdict.kind == Rearm.COOLDOWN


def test_decay_retry_is_bounded_not_nightly(monkeypatch):
    """The decay grants a few retries over a posting's life, not one per night —
    unbounded, this would restore the 245-wasted-retry behaviour."""
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    stale = datetime.utcnow() - timedelta(days=90)
    extra = _capped("submit_not_found", fingerprint=fp, at=stale)
    extra["apply_cooldown_rearm_count"] = 3
    assert rearm_reason(extra, current_fingerprint=fp) is None


def test_cooldown_can_be_disabled(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "0")
    fp = "same222222222222"
    stale = datetime.utcnow() - timedelta(days=30)
    assert rearm_reason(_capped("browser_timeout", fingerprint=fp, at=stale), current_fingerprint=fp) is None


def test_corrupt_timestamp_does_not_raise(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    extra = _capped("browser_timeout", fingerprint=fp)
    extra["apply_last_attempt"] = "not-a-timestamp"
    assert rearm_reason(extra, current_fingerprint=fp) is None


# ─── fingerprint ───────────────────────────────────────────────────────────

def test_fingerprint_is_stable_and_non_empty():
    assert apply_path_fingerprint()
    assert apply_path_fingerprint() == apply_path_fingerprint()


def test_attempt_stamps_the_current_fingerprint(sm):
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert extra["apply_code_fingerprint"] == apply_path_fingerprint()


# ─── persistence ───────────────────────────────────────────────────────────

def test_rearm_restores_the_budget_the_gate_reads(sm):
    """The end-to-end contract: a job the gate refuses becomes attemptable."""
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert should_attempt(extra["apply_last_status"], extra["apply_attempt_count"])[0] is False

    assert sm.rearm_circuit("j1", "apply-path code changed") is True

    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert should_attempt(extra["apply_last_status"], extra["apply_attempt_count"])[0] is True


def test_rearm_preserves_the_failure_evidence(sm):
    """Granting a retry must not launder away why the job failed — the funnel
    and the adaptive cap are built from this history."""
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    before = json.loads(sm.get_job("j1")["extra_json"])

    sm.rearm_circuit("j1", "apply-path code changed")
    after = json.loads(sm.get_job("j1")["extra_json"])

    assert after["apply_last_status"] == before["apply_last_status"]
    assert after["apply_last_detail"] == before["apply_last_detail"]
    assert after["apply_status_history"] == before["apply_status_history"]
    assert after["apply_attempt_count"] == 0
    assert after["apply_rearm_count"] == 1
    assert after["apply_rearm_reason"] == "apply-path code changed"


def test_rearm_clears_the_open_circuit_markers(sm):
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    sm.flag_circuit_break("j1", "needs_human", "needs_human retry cap reached (1/1)")
    assert json.loads(sm.get_job("j1")["extra_json"])["circuit_broken"] is True

    sm.rearm_circuit("j1", "apply-path code changed")
    after = json.loads(sm.get_job("j1")["extra_json"])
    for marker in ("circuit_broken", "circuit_class", "circuit_reason", "circuit_broken_at"):
        assert marker not in after


def test_repeated_rearms_are_counted(sm):
    sm.upsert_job({"job_id": "j1"})
    for _ in range(3):
        sm.record_apply_attempt("j1", "submit_not_found", "no button")
        sm.rearm_circuit("j1", "apply-path code changed")
    assert json.loads(sm.get_job("j1")["extra_json"])["apply_rearm_count"] == 3


def test_rearm_is_a_noop_without_an_open_circuit(sm):
    sm.upsert_job({"job_id": "j1"})
    assert sm.rearm_circuit("j1", "nothing to do") is False


def test_rearm_is_a_noop_for_an_unknown_job(sm):
    assert sm.rearm_circuit("does-not-exist", "nothing to do") is False


def test_rearm_does_not_re_fire_on_the_next_run(sm):
    """Self-limiting: the attempt after a re-arm stamps the current fingerprint,
    so the same code change cannot grant an unbounded retry loop."""
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    sm.rearm_circuit("j1", "apply-path code changed")
    sm.record_apply_attempt("j1", "submit_not_found", "still no button")

    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert rearm_reason(extra) is None
    assert should_attempt(extra["apply_last_status"], extra["apply_attempt_count"])[0] is False


# ─── cooldown is budgeted, code change is not ──────────────────────────────

def test_cooldown_rearm_stops_after_its_budget(monkeypatch):
    """Unbounded, the clock would hand every doomed job a free attempt every day
    forever — the exact waste the circuit breaker exists to prevent."""
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    fp = "same222222222222"
    stale = datetime.utcnow() - timedelta(days=30)
    extra = _capped("browser_timeout", fingerprint=fp, at=stale)

    extra["apply_cooldown_rearm_count"] = 2
    assert rearm_reason(extra, current_fingerprint=fp) is not None

    extra["apply_cooldown_rearm_count"] = 3
    assert rearm_reason(extra, current_fingerprint=fp) is None


def test_code_change_rearm_survives_an_exhausted_cooldown_budget(monkeypatch):
    """A shipped fix must reach a job even when the clock already gave up on it."""
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    stale = datetime.utcnow() - timedelta(days=30)
    extra = _capped("browser_timeout", at=stale)
    extra["apply_cooldown_rearm_count"] = 99

    verdict = rearm_reason(extra, current_fingerprint="new1111111111111")
    assert verdict and verdict.kind == Rearm.CODE_CHANGE


def test_only_cooldown_rearms_spend_the_cooldown_budget(sm):
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "browser_timeout", "timed out")

    sm.rearm_circuit("j1", "apply-path code changed", cooldown=False)
    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert extra["apply_rearm_count"] == 1
    assert "apply_cooldown_rearm_count" not in extra

    sm.record_apply_attempt("j1", "browser_timeout", "timed out")
    sm.rearm_circuit("j1", "transient blocker idle 30h", cooldown=True)
    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert extra["apply_rearm_count"] == 2
    assert extra["apply_cooldown_rearm_count"] == 1


# ─── apply-path routing flags are part of the build identity ───────────────

def test_adapter_registry_flip_rearms_capped_jobs(monkeypatch):
    """ACES-284 acceptance: flipping USE_ADAPTER_REGISTRY re-arms
    submit_not_found / form_not_reached jobs once. The registry route carries the
    fixes that make retries succeed, and flipping it changes no source byte."""
    from src.blocker_classifier import reset_fingerprint_cache

    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "1")
    reset_fingerprint_cache()
    before = apply_path_fingerprint()

    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "0")
    reset_fingerprint_cache()
    after = apply_path_fingerprint()
    assert before != after

    for status in ("submit_not_found", "form_not_reached"):
        verdict = rearm_reason(_capped(status, fingerprint=before), current_fingerprint=after)
        assert verdict and verdict.kind == Rearm.CODE_CHANGE

    reset_fingerprint_cache()


def test_unrelated_env_does_not_change_the_fingerprint(monkeypatch):
    from src.blocker_classifier import reset_fingerprint_cache

    reset_fingerprint_cache()
    before = apply_path_fingerprint()
    monkeypatch.setenv("SOME_UNRELATED_FLAG", "whatever")
    reset_fingerprint_cache()
    assert apply_path_fingerprint() == before


# ─── the "nothing submitted" alert names the real lever ────────────────────

def _outcome(status: str, *, cooldown_spent: int = 0) -> dict:
    return {
        "status": "circuit_open",
        "job": {
            "title": "t", "company": "c",
            "extra_json": json.dumps({
                "apply_last_status": status,
                "apply_attempt_count": 2,
                "apply_cooldown_rearm_count": cooldown_spent,
            }),
        },
    }


def test_alert_separates_self_healing_from_stuck(monkeypatch):
    """Eight 'nothing submitted' alerts fired at a human who had no lever to
    pull. The text must distinguish a backlog that retries itself from one
    that is genuinely waiting on a code fix."""
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    from src.orchestrator import Orchestrator

    detail = Orchestrator._nothing_submitted_detail(
        None,
        [_outcome("submit_not_found"), _outcome("browser_timeout"), _outcome("expired")],
        0,
    )
    assert "needs_human 1" in detail and "transient 1" in detail and "permanent 1" in detail
    assert "2 auto-retry within 24h" in detail
    assert "1 need a code fix" in detail


def test_alert_only_names_prepare_sessions_when_sessions_are_the_blocker(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    from src.orchestrator import Orchestrator

    without = Orchestrator._nothing_submitted_detail(None, [_outcome("submit_not_found")], 0)
    assert "prepare-sessions" not in without

    with_sessions = Orchestrator._nothing_submitted_detail(None, [_outcome("submit_not_found")], 6)
    assert "6 need session prep" in with_sessions and "prepare-sessions" in with_sessions


def test_alert_counts_an_exhausted_cooldown_as_stuck(monkeypatch):
    monkeypatch.setenv("APPLY_CIRCUIT_COOLDOWN_HOURS", "24")
    from src.orchestrator import Orchestrator

    detail = Orchestrator._nothing_submitted_detail(None, [_outcome("submit_not_found", cooldown_spent=3)], 0)
    assert "1 need a code fix" in detail
    assert "auto-retry" not in detail


# ─── re-arm must not weaken the duplicate-submission guard ─────────────────

def test_rearm_leaves_the_submission_ledger_intact(sm, tmp_path):
    """A re-armed `submission_unverified` job must not become a duplicate
    application. The circuit breaker and the idempotency ledger are independent
    gates: granting a retry budget says nothing about whether a prior submit
    landed, and the ledger still refuses at
    sources/adapters/session.py `needs_reconciliation` before Chrome launches.
    """
    from src.sources.adapters.idempotency import SubmissionLedger

    ledger = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    key = "greenhouse|https://boards.greenhouse.io/acme/jobs/1"
    ledger.begin(key, "attempt-1")
    ledger.complete(key, "attempt-1", verified=False)
    assert ledger.needs_reconciliation(key) is True

    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "submission_unverified", "no receipt")
    assert sm.rearm_circuit("j1", "cooldown", cooldown=True) is True

    # The retry budget reopened; the duplicate guard did not.
    assert ledger.needs_reconciliation(key) is True
    assert ledger.already_applied(key) is False
