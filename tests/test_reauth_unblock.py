"""ACES-284 (first slice): a successful reauth of a source re-arms that source's own
login-blocked approved jobs, the way prepare_sessions() does after a human sign-in.

Without this, jobs blocked on an expired session stayed blocked (and eventually
circuit-open → expired) even after the session was refreshed automatically — the
refresh and the per-job block never talked to each other.
"""
import json

from src.orchestrator import Orchestrator
from src.state_manager import StateManager


def _orch(tmp_path):
    # Skip __init__: no .env load, no secret-store fill, no scorer — just state.
    o = Orchestrator.__new__(Orchestrator)
    o.config = {}
    o.state = StateManager(str(tmp_path / "jobs.db"))
    return o


def _job(state, job_id, source, last_status=None):
    state.upsert_job({
        "job_id": job_id,
        "source": source,
        "title": "Director Engineering",
        "company": "ExampleCo",
        "url": f"https://example.com/jobs/{job_id}",
        "status": "approved",
        "score": 90,
    })
    if last_status:
        state.record_apply_attempt(job_id, last_status, "detail")


def _prepared(state, job_id) -> bool:
    row = next(j for j in state.get_approved_unapplied() if j["job_id"] == job_id)
    return bool(json.loads(row.get("extra_json") or "{}").get("session_prepared_at"))


def test_clears_only_own_login_blocks_for_that_source(tmp_path):
    o = _orch(tmp_path)
    _job(o.state, "u1", "usajobs", "usajobs_login_required")
    _job(o.state, "u2", "usajobs", "reauth_failed")
    _job(o.state, "u3", "usajobs", "submit_not_found")       # not a login block
    _job(o.state, "l1", "linkedin", "linkedin_authwall")     # other source
    _job(o.state, "u4", "usajobs")                           # never attempted

    assert o._unblock_session_jobs_after_reauth("usajobs") == 2

    assert _prepared(o.state, "u1")
    assert _prepared(o.state, "u2")
    assert not _prepared(o.state, "u3")
    assert not _prepared(o.state, "l1")
    assert not _prepared(o.state, "u4")


def test_external_ats_walls_are_not_cleared_by_source_reauth(tmp_path):
    """A LinkedIn login refresh cannot clear a Workday wall on a LinkedIn-origin
    job — the same distinction prepare_sessions draws (PR #81)."""
    o = _orch(tmp_path)
    _job(o.state, "w1", "linkedin", "workday_session_expired")
    _job(o.state, "l1", "linkedin", "linkedin_authwall")
    _job(o.state, "l2", "linkedin", "linkedin_login_required")

    assert o._unblock_session_jobs_after_reauth("linkedin") == 2

    assert _prepared(o.state, "l1")
    assert _prepared(o.state, "l2")
    assert not _prepared(o.state, "w1")


def test_no_matching_jobs_is_a_noop(tmp_path):
    o = _orch(tmp_path)
    _job(o.state, "j1", "jobright")
    _job(o.state, "j2", "jobright", "submit_not_found")
    assert o._unblock_session_jobs_after_reauth("jobright") == 0
    assert not _prepared(o.state, "j1")
    assert not _prepared(o.state, "j2")


def test_state_errors_never_propagate(tmp_path):
    """Bookkeeping after a reauth must never turn a successful refresh into a crash."""
    o = _orch(tmp_path)

    class _Broken:
        def get_approved_unapplied(self):
            raise RuntimeError("db locked")

    o.state = _Broken()
    assert o._unblock_session_jobs_after_reauth("usajobs") == 0
