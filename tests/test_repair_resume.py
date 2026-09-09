"""repair_resume tests — real StateManager on a temp jobs DB, fake coordinator
reporter, stub ledger. Proves: a completed repair unblocks the job; a job in
reconciliation_required stays held (submission truth is sacred); a non-completed
repair leaves the job untouched."""
from types import SimpleNamespace

import pytest

from src.repair_resume import resume_repaired_jobs
from src.state_manager import StateManager, parse_extra_json


@pytest.fixture
def state_mgr(tmp_path):
    return StateManager(db_path=tmp_path / "test_jobs.db")


class _NoRecordLedger:
    """Ledger stub with no history — sync_confirmation_from_ledger is a no-op."""
    def record(self, key):
        return None

    def is_stale_in_progress(self, key):
        return False


def _reporter(status_by_op):
    calls = []

    def check_repair_status(op_id):
        calls.append(op_id)
        view = status_by_op.get(op_id)
        return dict(view) if view else None

    return SimpleNamespace(check_repair_status=check_repair_status, calls=calls)


def _insert(state, job_id, **extra):
    job = {
        "job_id": job_id,
        "title": "Director of Engineering",
        "company": "Acme",
        "source": "external",
        "status": "approved",
        "url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        **extra,
    }
    state.upsert_job(job)


def _extra(state, job_id):
    return parse_extra_json(state.get_job(job_id)["extra_json"])


def test_completed_repair_unblocks_job_and_syncs_ledger(state_mgr):
    _insert(state_mgr, "j1", repair_operation_id="op-1",
            repair_incident_id="job-j1-a1", apply_last_status="bad_ats_url")
    reporter = _reporter({"op-1": {
        "id": "op-1", "status": "completed", "prUrl": "https://github.com/x/y/pull/7",
    }})

    sync_calls = []
    orig_sync = state_mgr.sync_confirmation_from_ledger

    def _spy_sync(job_id, ledger=None):
        sync_calls.append((job_id, ledger))
        return orig_sync(job_id, ledger=ledger)

    state_mgr.sync_confirmation_from_ledger = _spy_sync

    ledger = _NoRecordLedger()
    resumed = resume_repaired_jobs(state=state_mgr, ledger=ledger, reporter=reporter)

    assert resumed == 1
    assert reporter.calls == ["op-1"]
    assert sync_calls == [("j1", ledger)]
    extra = _extra(state_mgr, "j1")
    assert extra["repair_completed_at"]
    assert extra["repair_pr_url"] == "https://github.com/x/y/pull/7"
    # clear_session_block stamps session_prepared_at → job re-enters the apply loop
    assert extra["session_prepared_at"]


def test_reconciliation_required_stays_held(state_mgr):
    _insert(state_mgr, "j2", repair_operation_id="op-2")
    # Drive the job into reconciliation_required through the legal transition path.
    state_mgr.transition_confirmation("j2", "submitting")
    state_mgr.transition_confirmation("j2", "submission_unverified")
    state_mgr.transition_confirmation("j2", "reconciliation_required")

    reporter = _reporter({"op-2": {"id": "op-2", "status": "completed"}})
    resumed = resume_repaired_jobs(
        state=state_mgr, ledger=_NoRecordLedger(), reporter=reporter)

    assert resumed == 0
    extra = _extra(state_mgr, "j2")
    # the repair completion is still stamped (won't re-poll the coordinator) …
    assert extra["repair_completed_at"]
    # … but the job is NOT unblocked: never auto-resubmit an ambiguous submit.
    assert "session_prepared_at" not in extra
    assert state_mgr.get_job("j2")["confirmation_status"] == "reconciliation_required"


def test_non_completed_operation_leaves_job_untouched(state_mgr):
    _insert(state_mgr, "j3", repair_operation_id="op-3")
    reporter = _reporter({"op-3": {"id": "op-3", "status": "in_progress"}})

    resumed = resume_repaired_jobs(
        state=state_mgr, ledger=_NoRecordLedger(), reporter=reporter)

    assert resumed == 0
    assert reporter.calls == ["op-3"]
    extra = _extra(state_mgr, "j3")
    assert "repair_completed_at" not in extra
    assert "session_prepared_at" not in extra


def test_already_completed_jobs_not_repolled(state_mgr):
    _insert(state_mgr, "j4", repair_operation_id="op-4",
            repair_completed_at=1234567890.0)
    _insert(state_mgr, "j5")  # no repair binding at all
    reporter = _reporter({"op-4": {"id": "op-4", "status": "completed"}})

    resumed = resume_repaired_jobs(
        state=state_mgr, ledger=_NoRecordLedger(), reporter=reporter)

    assert resumed == 0
    assert reporter.calls == []
    assert state_mgr.list_jobs_awaiting_repair() == []


def test_coordinator_unreachable_is_fail_open(state_mgr):
    _insert(state_mgr, "j6", repair_operation_id="op-6")

    def _boom(op_id):
        raise RuntimeError("coordinator down")

    reporter = SimpleNamespace(check_repair_status=_boom)
    resumed = resume_repaired_jobs(
        state=state_mgr, ledger=_NoRecordLedger(), reporter=reporter)
    assert resumed == 0
    assert "repair_completed_at" not in _extra(state_mgr, "j6")
