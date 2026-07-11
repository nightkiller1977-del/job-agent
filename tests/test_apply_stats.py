"""P1 instrumentation: apply funnel & success-rate computation.

Verifies StateManager.get_apply_funnel() and the `submitted` flag that
record_apply_attempt now stamps on every attempt (success OR failure) — the gap
that previously left `submitted` null for all 46 real attempts.
"""
import sqlite3

import pytest

from src.state_manager import StateManager


@pytest.fixture
def sm(tmp_path):
    db = tmp_path / "jobs.db"
    mgr = StateManager(db_path=str(db))
    yield mgr
    mgr.close()


def _job(mgr, job_id, source="linkedin", status="approved"):
    mgr.upsert_job({"job_id": job_id, "source": source, "status": status})


def test_submitted_flag_stamped_on_success_and_failure(sm):
    _job(sm, "j1")
    _job(sm, "j2")
    sm.record_apply_attempt("j1", "applied", "ok")
    sm.record_apply_attempt("j2", "submit_not_found", "no button")

    j1 = sm.get_job("j1")
    j2 = sm.get_job("j2")
    import json
    assert json.loads(j1["extra_json"])["submitted"] is True
    assert json.loads(j2["extra_json"])["submitted"] is False


def test_funnel_success_rate_and_counts(sm):
    # 4 attempts, 1 success -> 25%
    for i, (status) in enumerate(
        ["applied", "external_ats_error", "workday_session_expired", "submit_not_found"]
    ):
        _job(sm, f"j{i}")
        sm.record_apply_attempt(f"j{i}", status, "detail")
    # one discovered job that was never attempted
    _job(sm, "never", status="discovered")

    f = sm.get_apply_funnel()
    assert f["total_jobs"] == 5
    assert f["attempts"] == 4
    assert f["submitted"] == 1
    assert f["attempt_success_rate"] == pytest.approx(0.25)


def test_failure_clusters_group_correctly(sm):
    mapping = {
        "external_ats_error": "form_completion",
        "form_not_reached": "form_completion",
        "workday_session_expired": "auth_session",
        "brassring_login_required": "auth_session",
        "bad_ats_url": "config_error",
        "weird_new_status": "other",
    }
    for i, status in enumerate(mapping):
        _job(sm, f"j{i}")
        sm.record_apply_attempt(f"j{i}", status, "d")

    f = sm.get_apply_funnel()
    assert f["failure_clusters"]["form_completion"] == 2
    assert f["failure_clusters"]["auth_session"] == 2
    assert f["failure_clusters"]["config_error"] == 1
    assert f["failure_clusters"]["other"] == 1
    # a status not in any cluster must not be silently dropped
    assert f["failure_histogram"]["weird_new_status"] == 1


def test_wasted_retries_counts_extra_attempts_on_failed_jobs(sm):
    _job(sm, "fail")
    for _ in range(3):  # 3 attempts, all failing -> 2 wasted
        sm.record_apply_attempt("fail", "external_ats_error", "d")
    _job(sm, "win")
    sm.record_apply_attempt("win", "external_ats_error", "d")
    sm.record_apply_attempt("win", "applied", "d")  # eventually succeeds

    f = sm.get_apply_funnel()
    # "fail": attempt_count=3 -> 2 wasted. "win" ended submitted -> not counted.
    assert f["wasted_retries"] == 2


def test_per_source_breakdown(sm):
    _job(sm, "a", source="linkedin")
    _job(sm, "b", source="linkedin")
    _job(sm, "c", source="indeed")
    sm.record_apply_attempt("a", "applied", "d")
    sm.record_apply_attempt("b", "submit_not_found", "d")
    sm.record_apply_attempt("c", "applied", "d")

    f = sm.get_apply_funnel()
    assert f["per_source"]["linkedin"] == {"attempts": 2, "submitted": 1, "rate": pytest.approx(0.5)}
    assert f["per_source"]["indeed"]["rate"] == pytest.approx(1.0)


def test_show_apply_stats_renders(sm, capsys):
    """The `stats` command render path (Orchestrator.show_apply_stats) runs without
    error and returns the funnel dict. Imported under the conftest playwright stub;
    skips where the scraper import chain's optional deps aren't installed."""
    pytest.importorskip("pypdf")  # resume_helper (pulled in by Orchestrator) needs it
    from src.orchestrator import Orchestrator

    _job(sm, "a", source="linkedin")
    _job(sm, "b", source="jobright")
    sm.record_apply_attempt("a", "applied", "ok")
    sm.record_apply_attempt("b", "external_ats_error", "boom")

    # Call the method without running Orchestrator.__init__ (heavy); it only needs .state
    fake = type("O", (), {"state": sm})()
    result = Orchestrator.show_apply_stats(fake)

    assert result["attempts"] == 2 and result["submitted"] == 1
    out = capsys.readouterr().out
    assert "Apply Success Report" in out
    assert "50.0%" in out  # 1 of 2


def test_legacy_rows_without_submitted_flag_fall_back_to_status(sm):
    """Existing DB rows (pre-P1) have no `submitted` key — funnel must still count
    them by apply_last_status so historical success rate is correct."""
    _job(sm, "legacy")
    # simulate a legacy row: apply_last_status set, but no `submitted` key
    import json
    with sqlite3.connect(str(sm.db_path)) as conn:
        conn.execute(
            "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
            (json.dumps({"apply_last_status": "applied"}), "legacy"),
        )
    f = sm.get_apply_funnel()
    assert f["submitted"] == 1
    assert f["attempts"] == 1
