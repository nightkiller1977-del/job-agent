"""Unit tests for StateManager confirmation_status state transitions."""
import pytest
from pathlib import Path
from src.state_manager import StateManager, InvalidStateTransitionError


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
