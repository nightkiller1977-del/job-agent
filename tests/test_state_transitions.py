"""Unit tests for StateManager confirmation_status state transitions."""
import pytest
from pathlib import Path
from src.state_manager import StateManager, InvalidStateTransitionError
from src.orchestrator import Orchestrator


@pytest.fixture
def state_mgr(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return StateManager(db_path=db_file)


def test_valid_confirmation_lifecycle(state_mgr):
    job = {
        "job_id": "test_job_1",
        "title": "Director of Engineering",
        "company": "Tech Corp",
        "source": "linkedin",
        "status": "approved",
    }
    state_mgr.upsert_job(job)

    # Starts at None
    fetched = state_mgr.get_job("test_job_1")
    assert fetched.get("confirmation_status") is None

    # Step 1: submitting
    state_mgr.transition_confirmation("test_job_1", "submitting")
    assert state_mgr.get_job("test_job_1")["confirmation_status"] == "submitting"

    # Step 2: submitted
    state_mgr.transition_confirmation("test_job_1", "submitted")
    assert state_mgr.get_job("test_job_1")["confirmation_status"] == "submitted"

    # Step 3: receipt_pending
    state_mgr.transition_confirmation("test_job_1", "receipt_pending")
    assert state_mgr.get_job("test_job_1")["confirmation_status"] == "receipt_pending"

    # Step 4: confirmed_by_employer (requires primary status == "applied")
    state_mgr.set_status("test_job_1", "applied")
    state_mgr.transition_confirmation("test_job_1", "confirmed_by_employer")
    assert state_mgr.get_job("test_job_1")["confirmation_status"] == "confirmed_by_employer"


def test_illegal_confirmation_transition_raises_error(state_mgr):
    job = {
        "job_id": "test_job_2",
        "title": "VP Engineering",
        "company": "Global Corp",
        "source": "jobright",
        "status": "approved",
    }
    state_mgr.upsert_job(job)

    # Illegal jump from None -> confirmed_by_employer
    with pytest.raises(InvalidStateTransitionError):
        state_mgr.transition_confirmation("test_job_2", "confirmed_by_employer")


def test_confirmed_by_employer_guard_requires_applied_status(state_mgr):
    job = {
        "job_id": "test_job_3",
        "title": "CTO",
        "company": "Startup Corp",
        "source": "indeed",
        "status": "approved",  # NOT applied
    }
    state_mgr.upsert_job(job)
    state_mgr.transition_confirmation("test_job_3", "submitting")
    state_mgr.transition_confirmation("test_job_3", "submitted")

    # Should raise error because status is 'approved', not 'applied'
    with pytest.raises(InvalidStateTransitionError):
        state_mgr.transition_confirmation("test_job_3", "confirmed_by_employer")


def test_orchestrator_stamps_confirmation_status_on_apply_success(state_mgr):
    """Orchestrator._mark_confirmation_submitted must be the source of confirmation_status
    evidence — called right at the point a real apply-success is observed, not inferred
    later from an email (see EmailConfirmationTracker._apply_confirmation_transition)."""
    job = {
        "job_id": "test_job_4",
        "title": "Staff Engineer",
        "company": "Acme Corp",
        "source": "jobright",
        "status": "approved",
    }
    state_mgr.upsert_job(job)

    orch = Orchestrator.__new__(Orchestrator)  # skip __init__ side effects
    orch.config = {}
    orch.state = state_mgr
    orch._mark_confirmation_submitted("test_job_4")

    assert state_mgr.get_job("test_job_4")["confirmation_status"] == "submitted"


def test_sync_confirmation_from_ledger_projections(state_mgr, tmp_path):
    from src.sources.adapters.idempotency import SubmissionLedger, canonical_key, PHASE_IN_PROGRESS, PHASE_VERIFIED, PHASE_UNVERIFIED
    ledger_path = tmp_path / "apply_ledger.json"
    ledger = SubmissionLedger(path=ledger_path)

    job = {
        "job_id": "job_ledger_1",
        "title": "Principal Architect",
        "company": "Databricks",
        "url": "https://databricks.com/careers/123",
        "source": "linkedin",
        "status": "applied",
    }
    state_mgr.upsert_job(job)
    key = canonical_key(job)

    # 1. Live in-progress -> 'submitting'
    ledger.begin(key, "att_1")
    res1 = state_mgr.sync_confirmation_from_ledger("job_ledger_1", ledger=ledger)
    assert res1 == "submitting"
    assert state_mgr.get_job("job_ledger_1")["confirmation_status"] == "submitting"

    # 2. Complete verified -> 'submitted'
    ledger.complete(key, "att_1", verified=True)
    res2 = state_mgr.sync_confirmation_from_ledger("job_ledger_1", ledger=ledger)
    assert res2 == "submitted"
    assert state_mgr.get_job("job_ledger_1")["confirmation_status"] == "submitted"

    # 3. Unverified attempt on job_ledger_2 -> 'submission_unverified'
    job2 = {
        "job_id": "job_ledger_2",
        "title": "Staff Engineer",
        "company": "Stripe",
        "url": "https://stripe.com/careers/999",
        "source": "jobright",
        "status": "applied",
    }
    state_mgr.upsert_job(job2)
    key2 = canonical_key(job2)
    ledger.begin(key2, "att_2")
    state_mgr.sync_confirmation_from_ledger("job_ledger_2", ledger=ledger)
    ledger.complete(key2, "att_2", verified=False)
    res3 = state_mgr.sync_confirmation_from_ledger("job_ledger_2", ledger=ledger)
    assert res3 == "submission_unverified"
    assert state_mgr.get_job("job_ledger_2")["confirmation_status"] == "submission_unverified"


def test_archive_job_preserves_confirmation_status(state_mgr):
    job = {
        "job_id": "job_archive_test",
        "title": "Engineering Director",
        "company": "Figma",
        "url": "https://figma.com/careers/456",
        "source": "linkedin",
        "status": "applied",
    }
    state_mgr.upsert_job(job)
    state_mgr.transition_confirmation("job_archive_test", "submitting")
    state_mgr.transition_confirmation("job_archive_test", "submitted")
    state_mgr.transition_confirmation("job_archive_test", "confirmed_by_employer")

    archived = state_mgr.archive_job("job_archive_test", reason="accepted_offer")
    assert archived["job_id"] == "job_archive_test"

    # Check archived_jobs table has confirmation_status
    with state_mgr._connect() as conn:
        row = conn.execute("SELECT * FROM archived_jobs WHERE job_id = ?", ("job_archive_test",)).fetchone()
        assert row is not None
        assert dict(row)["confirmation_status"] == "confirmed_by_employer"
