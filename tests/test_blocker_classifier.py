"""P2: blocker classification + circuit-breaker policy + persistence."""
import json

import pytest

from src.blocker_classifier import (
    BlockerClass,
    classify,
    max_attempts,
    should_attempt,
)
from src.state_manager import StateManager


@pytest.fixture
def sm(tmp_path):
    mgr = StateManager(db_path=str(tmp_path / "jobs.db"))
    yield mgr
    mgr.close()


@pytest.mark.parametrize(
    "status,expected",
    [
        ("applied", BlockerClass.SUCCESS),
        ("external_ats_error", BlockerClass.TRANSIENT),
        ("browser_timeout", BlockerClass.TRANSIENT),
        ("model_timeout", BlockerClass.TRANSIENT),
        ("unknown_external_ats_error", BlockerClass.TRANSIENT),
        ("error", BlockerClass.TRANSIENT),
        ("workday_session_expired", BlockerClass.AUTH_REQUIRED),
        ("brassring_login_required", BlockerClass.AUTH_REQUIRED),
        ("submit_not_found", BlockerClass.NEEDS_HUMAN),
        ("form_not_reached", BlockerClass.NEEDS_HUMAN),
        ("ats_failure", BlockerClass.NEEDS_HUMAN),
        ("keyword_coverage_failed", BlockerClass.NEEDS_HUMAN),
        ("pdf_text_layer_failed", BlockerClass.NEEDS_HUMAN),
        ("resume_upload_failed", BlockerClass.NEEDS_HUMAN),
        ("ats_selector_failed", BlockerClass.NEEDS_HUMAN),
        ("bad_ats_url", BlockerClass.PERMANENT),
        ("unknown_source", BlockerClass.PERMANENT),
        ("some_new_unmapped_status", BlockerClass.UNKNOWN),
        (None, BlockerClass.UNKNOWN),
    ],
)
def test_classify(status, expected):
    assert classify(status) is expected


def test_never_tried_is_always_attempted():
    ok, reason = should_attempt(None, 0)
    assert ok is True and reason == ""


def test_success_never_reattempted():
    ok, reason = should_attempt("applied", 1)
    assert ok is False and "already applied" in reason


def test_permanent_never_retried():
    ok, reason = should_attempt("bad_ats_url", 0)
    assert ok is False and "permanent" in reason


def test_needs_human_capped_at_one():
    assert should_attempt("submit_not_found", 0)[0] is True   # first attempt allowed
    assert should_attempt("submit_not_found", 1)[0] is False  # then stop


def test_transient_capped_at_three():
    assert should_attempt("external_ats_error", 2)[0] is True
    ok, reason = should_attempt("external_ats_error", 3)
    assert ok is False and "retry cap reached" in reason


def test_auth_required_capped_at_five():
    assert should_attempt("workday_session_expired", 4)[0] is True
    assert should_attempt("workday_session_expired", 5)[0] is False


def test_high_attempt_count_stops_the_baseline_bleed():
    """The real bug: jobs re-attempted up to 17×. Every failure class must stop
    well before that."""
    for status in [
        "external_ats_error", "submit_not_found", "form_not_reached",
        "workday_session_expired", "bad_ats_url",
    ]:
        assert should_attempt(status, 17)[0] is False, status


def test_max_attempts_lookup():
    assert max_attempts("submit_not_found") == 1
    assert max_attempts("external_ats_error") == 3
    assert max_attempts("bad_ats_url") == 0


def test_record_apply_attempt_stamps_blocker_class(sm):
    sm.upsert_job({"job_id": "j1"})
    sm.record_apply_attempt("j1", "workday_session_expired", "expired")
    extra = json.loads(sm.get_job("j1")["extra_json"])
    assert extra["blocker_class"] == "auth_required"


def test_flag_circuit_break_preserves_real_status_and_count(sm):
    sm.upsert_job({"job_id": "j1"})
    # two real failed attempts
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    sm.record_apply_attempt("j1", "submit_not_found", "no button")
    before = json.loads(sm.get_job("j1")["extra_json"])

    sm.flag_circuit_break("j1", "needs_human", "needs_human retry cap reached (2/1)")
    after = json.loads(sm.get_job("j1")["extra_json"])

    # circuit metadata added...
    assert after["circuit_broken"] is True
    assert after["circuit_class"] == "needs_human"
    assert "cap reached" in after["circuit_reason"]
    # ...without clobbering the real outcome or inflating the attempt count
    assert after["apply_last_status"] == before["apply_last_status"] == "submit_not_found"
    assert after["lifetime_attempt_count"] == before["lifetime_attempt_count"] == 2
