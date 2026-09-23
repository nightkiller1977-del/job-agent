"""ACES-434: an ambiguous submission outcome must never be automatically
re-armed, by the code-change path, the cooldown path, or an unfiltered
`rearm-breakers` sweep — including a --class or --job-id override aimed
directly at one.

Before this fix: `submission_unverified` (and its siblings
`duplicate_application_prevented` / `submit_unverified_unresolved`) were not in
blocker_classifier._STATUS_TO_CLASS, so classify() fell through to
BlockerClass.UNKNOWN — and UNKNOWN sits in both _CODE_REARM_CLASSES and
_COOLDOWN_REARM_CLASSES. A code change anywhere in the apply path (which
happens often) or 24h of elapsed time would silently make these jobs
attemptable again, with no reconciliation of whether the prior click already
reached the employer.

live database evidence (2026-09-23): both jobs this ticket names —
Constellation West (canonical_key mismatch case) and Scan.com — carry a
matching PHASE_UNVERIFIED submission-ledger record, so a resubmit attempt
would additionally be blocked at the ledger layer. This test suite does not
depend on that second layer: it proves the circuit-breaker layer itself now
refuses, so a job whose ledger entry is missing, was written by an older code
path, or lives on a host without that ledger file is protected too.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.blocker_classifier import (
    AMBIGUOUS_SUBMISSION_STATUSES,
    BlockerClass,
    classify,
    max_attempts,
    rearm_reason,
    should_attempt,
)
from src.orchestrator import Orchestrator
from src.state_manager import StateManager

AMBIGUOUS = sorted(AMBIGUOUS_SUBMISSION_STATUSES)


# ─── classification ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", AMBIGUOUS)
def test_classifies_as_reconciliation_required_not_unknown(status):
    assert classify(status) is BlockerClass.RECONCILIATION_REQUIRED
    assert classify(status) is not BlockerClass.UNKNOWN


@pytest.mark.parametrize("status", AMBIGUOUS)
def test_cap_is_zero_blind_retry_refused_even_on_first_attempt(status):
    """Distinct from every capped class: this must refuse before any attempts
    have been spent, not just after a budget is exhausted."""
    assert max_attempts(status) == 0
    attempt, reason = should_attempt(status, attempt_count=0, source="jobright")
    assert attempt is False
    assert "reconciliation_required" in reason


# ─── automatic re-arm: code-change path ─────────────────────────────────────

@pytest.mark.parametrize("status", AMBIGUOUS)
def test_code_change_never_rearms_an_ambiguous_status(status):
    extra = {
        "apply_last_status": status,
        "apply_attempt_count": 1,
        "apply_code_fingerprint": "old-fingerprint",
    }
    assert rearm_reason(extra, current_fingerprint="new-fingerprint") is None


def test_code_change_does_rearm_a_genuinely_unknown_status():
    """Control: an actually-unclassified status must still behave as before —
    this fix must not make UNKNOWN itself stop re-arming."""
    extra = {
        "apply_last_status": "some_future_status_nobody_has_classified_yet",
        "apply_attempt_count": 1,
        "apply_code_fingerprint": "old-fingerprint",
    }
    r = rearm_reason(extra, current_fingerprint="new-fingerprint")
    assert r is not None


# ─── automatic re-arm: cooldown path ────────────────────────────────────────

@pytest.mark.parametrize("status", AMBIGUOUS)
def test_cooldown_never_rearms_an_ambiguous_status(status):
    extra = {
        "apply_last_status": status,
        "apply_attempt_count": 1,
        "apply_last_attempt": (datetime.utcnow() - timedelta(hours=48)).isoformat(),
    }
    assert rearm_reason(extra, now=datetime.utcnow()) is None


# ─── rearm-breakers: unfiltered sweep, --class, and --job-id ────────────────

def _make_orchestrator(tmp_path):
    config = {"state_db_path": str(tmp_path / "jobs.db")}
    with patch("src.orchestrator.JobScorer"):
        orc = Orchestrator.__new__(Orchestrator)
        orc.config = config
        orc.state = StateManager(config["state_db_path"])
        orc.scorer = MagicMock()
    return orc


def _approved_job(job_id, status, *, attempts=1):
    return {
        "job_id": job_id,
        "source": "jobright",
        "title": "Engineering Manager",
        "company": "Acme",
        "url": f"https://jobright.ai/jobs/info/{job_id}",
        "status": "approved",
        "score": 90,
        "discovered_at": datetime.utcnow().isoformat(),
        "extra_json": {
            "apply_last_status": status,
            "apply_attempt_count": attempts,
            "circuit_broken": True,
        },
    }


@pytest.fixture
def orchestrator_with_mixed_jobs(tmp_path):
    orc = _make_orchestrator(tmp_path)
    orc.state.upsert_job(_approved_job("ambiguous-1", "submission_unverified"))
    orc.state.upsert_job(_approved_job("ambiguous-2", "duplicate_application_prevented"))
    orc.state.upsert_job(_approved_job("fixable-1", "submit_not_found"))
    return orc


def test_unfiltered_sweep_holds_back_ambiguous_and_reports_it(orchestrator_with_mixed_jobs):
    result = orchestrator_with_mixed_jobs.rearm_breakers(dry_run=True)
    assert result["held_back"] == 2
    assert result["matched"] == 1  # only the genuinely fixable NEEDS_HUMAN job


def test_unfiltered_sweep_does_not_flip_circuit_broken_on_ambiguous_jobs(orchestrator_with_mixed_jobs):
    orchestrator_with_mixed_jobs.rearm_breakers(dry_run=False)
    for jid in ("ambiguous-1", "ambiguous-2"):
        job = orchestrator_with_mixed_jobs.state.get_job(jid)
        extra = job.get("extra_json") or {}
        if isinstance(extra, str):
            import json
            extra = json.loads(extra)
        assert extra.get("circuit_broken") is True, f"{jid} must remain circuit-open"


def test_class_flag_rejects_reconciliation_required_outright(orchestrator_with_mixed_jobs):
    result = orchestrator_with_mixed_jobs.rearm_breakers(
        blocker_class="reconciliation_required", dry_run=True
    )
    assert result == {"matched": 0, "rearmed": 0, "held_back": 0}


def test_job_id_override_cannot_rearm_an_ambiguous_job(orchestrator_with_mixed_jobs):
    """The escape hatch that works for every other class must not work here —
    an operator explicitly targeting one ambiguous job by ID is still refused."""
    result = orchestrator_with_mixed_jobs.rearm_breakers(job_id="ambiguous-1", dry_run=True)
    assert result["matched"] == 0
    assert result["held_back"] == 1


def test_job_id_override_still_works_for_a_fixable_class(orchestrator_with_mixed_jobs):
    """Control: the --job-id escape hatch itself must still function normally
    for a class this fix did not touch."""
    result = orchestrator_with_mixed_jobs.rearm_breakers(job_id="fixable-1", dry_run=True)
    assert result["matched"] == 1
    assert result["held_back"] == 0


def test_class_flag_still_works_for_an_unaffected_class(orchestrator_with_mixed_jobs):
    result = orchestrator_with_mixed_jobs.rearm_breakers(blocker_class="needs_human", dry_run=True)
    assert result["matched"] == 1
    assert result["held_back"] == 0
