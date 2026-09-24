"""Unit tests for Multi-Signal Email Confirmation Tracker."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import pytest
from src.email_confirmation_tracker import EmailConfirmationTracker, _CONFIRMATION_REVIEW_QUEUE_FILE
from src.state_manager import StateManager


def test_workday_email_confirmation_scoring():
    tracker = EmailConfirmationTracker()
    job = {
        "job_id": "wd_job_1",
        "company": "Salesforce",
        "title": "Senior Director of Infrastructure",
        "url": "https://salesforce.wd12.myworkdayjobs.com/en-US/External/job/123",
        "status": "applied",
        "applied_at": (datetime.now() - timedelta(hours=2)).isoformat(),
    }

    sender = "Salesforce Careers <salesforce@myworkday.com>"
    subject = "Thank you for applying: Senior Director of Infrastructure"
    body = "We have received your application for Senior Director of Infrastructure. Requisition ID: REQ-98412."
    msg_date = datetime.now()

    score, evidence = tracker.calculate_match_score(sender, subject, body, msg_date, job)
    assert score >= 0.85
    assert evidence.get("company_matched") == "salesforce"
    assert evidence.get("vendor_domain") == "myworkday.com"
    assert evidence.get("confirmation_id_extracted") == "REQ-98412" or evidence.get("confirmation_id_verified") == "REQ-98412"


def test_greenhouse_email_confirmation_scoring():
    tracker = EmailConfirmationTracker()
    job = {
        "job_id": "gh_job_1",
        "company": "Stripe",
        "title": "Staff Software Engineer, Platform",
        "url": "https://boards.greenhouse.io/stripe/jobs/456",
        "status": "applied",
        "applied_at": (datetime.now() - timedelta(days=1)).isoformat(),
    }

    sender = "Stripe Recruiting <no-reply@greenhouse-mail.io>"
    subject = "Your application to Stripe"
    body = "Thank you for applying to the Staff Software Engineer, Platform position at Stripe. Confirmation #: ST-4401."
    msg_date = datetime.now()

    score, evidence = tracker.calculate_match_score(sender, subject, body, msg_date, job)
    assert score >= 0.85
    assert evidence.get("company_matched") == "stripe"
    assert evidence.get("vendor_domain") == "greenhouse-mail.io"


def test_unmatched_email_scoring():
    tracker = EmailConfirmationTracker()
    job = {
        "job_id": "random_job_1",
        "company": "Apple",
        "title": "Engineering Manager",
        "url": "https://jobs.apple.com",
        "status": "applied",
    }

    sender = "Unknown Recruiter <recruiter@randomfirm.com>"
    subject = "Newsletter weekly update"
    body = "Here are the top articles this week."
    msg_date = datetime.now()

    score, _ = tracker.calculate_match_score(sender, subject, body, msg_date, job)
    assert score < 0.50


@pytest.fixture
def state_mgr(tmp_path):
    return StateManager(db_path=tmp_path / "test_jobs.db")


def test_confirmation_transition_requires_submission_evidence(state_mgr, tmp_path):
    """A high-scoring email match must never fabricate submitting/submitted history
    for a row with no ledger-backed evidence (e.g. a legacy row, or one the
    orchestrator never got to stamp) — it should flag for manual review instead."""
    job = {
        "job_id": "legacy_job_1",
        "title": "Director of Engineering",
        "company": "Tech Corp",
        "source": "linkedin",
        "status": "applied",
    }
    state_mgr.upsert_job(job)
    fetched = state_mgr.get_job("legacy_job_1")
    assert fetched.get("confirmation_status") is None

    tracker = EmailConfirmationTracker(
        state_manager=state_mgr, review_queue_file=tmp_path / "confirmation_review_queue.json"
    )
    outcome = {"confirmed": True}
    tracker._apply_confirmation_transition(fetched, 0.9, outcome)

    assert state_mgr.get_job("legacy_job_1")["confirmation_status"] is None
    assert outcome["needs_manual_confirmation"] is True


def test_confirmation_transition_advances_with_submission_evidence(state_mgr):
    """When the orchestrator already stamped submitted/receipt_pending from a real
    apply-success event, a matching confirmation email should legitimately advance
    the row to confirmed_by_employer."""
    job = {
        "job_id": "real_job_1",
        "title": "VP Engineering",
        "company": "Global Corp",
        "source": "jobright",
        "status": "approved",
    }
    state_mgr.upsert_job(job)
    state_mgr.transition_confirmation("real_job_1", "submitting")
    state_mgr.transition_confirmation("real_job_1", "submitted")
    state_mgr.set_status("real_job_1", "applied")

    tracker = EmailConfirmationTracker(state_manager=state_mgr)
    outcome = {"confirmed": True}
    fetched = state_mgr.get_job("real_job_1")
    tracker._apply_confirmation_transition(fetched, 0.9, outcome)

    assert state_mgr.get_job("real_job_1")["confirmation_status"] == "confirmed_by_employer"
    assert "needs_manual_confirmation" not in outcome


def test_fetch_candidate_jobs_includes_approved_submission_unverified(state_mgr):
    """ACES-445: a job whose submit was clicked but couldn't be receipt-verified
    stays at status='approved' / confirmation_status='submission_unverified'
    (StateManager never promotes it to 'applied' without a verified ledger
    phase). The inbox scan is its only remaining path to reconciliation, so it
    must be a scan candidate even though its status isn't 'applied'."""
    state_mgr.upsert_job({
        "job_id": "unverified_job_1",
        "title": "Engineering Manager",
        "company": "Valon",
        "source": "ashby",
        "status": "approved",
    })
    state_mgr.transition_confirmation("unverified_job_1", "submitting")
    state_mgr.transition_confirmation("unverified_job_1", "submission_unverified")

    tracker = EmailConfirmationTracker(state_manager=state_mgr)
    candidates = {j["job_id"] for j in tracker._fetch_candidate_jobs()}

    assert "unverified_job_1" in candidates


def test_fetch_candidate_jobs_excludes_approved_without_unverified_receipt(state_mgr):
    """An 'approved' row that was never submitted (no submission_unverified
    ledger evidence) must not be pulled into the scan — only the specific
    receipt-reconciliation-candidate case from ACES-445 is in scope."""
    state_mgr.upsert_job({
        "job_id": "not_yet_applied_1",
        "title": "Staff Engineer",
        "company": "Acme",
        "source": "greenhouse",
        "status": "approved",
    })

    tracker = EmailConfirmationTracker(state_manager=state_mgr)
    candidates = {j["job_id"] for j in tracker._fetch_candidate_jobs()}

    assert "not_yet_applied_1" not in candidates


def test_fetch_candidate_jobs_excludes_confirmed_by_employer(state_mgr):
    """A row already confirmed by the employer is terminal and must not be
    rescanned."""
    state_mgr.upsert_job({
        "job_id": "already_confirmed_1",
        "title": "Director",
        "company": "Globex",
        "source": "lever",
        "status": "applied",
    })
    state_mgr.transition_confirmation("already_confirmed_1", "submitting")
    state_mgr.transition_confirmation("already_confirmed_1", "submitted")
    state_mgr.transition_confirmation("already_confirmed_1", "confirmed_by_employer")

    tracker = EmailConfirmationTracker(state_manager=state_mgr)
    candidates = {j["job_id"] for j in tracker._fetch_candidate_jobs()}

    assert "already_confirmed_1" not in candidates


def test_unverified_email_match_flags_for_manual_review_not_auto_confirm(state_mgr, tmp_path):
    """A matched confirmation email against a submission_unverified row is
    ambiguous, not proof — it must be routed to the manual review queue, the
    same as any other row lacking submitted/receipt_pending evidence. It must
    never silently promote to confirmed_by_employer, since that would fabricate
    certainty ACES-445's whole scenario doesn't have."""
    state_mgr.upsert_job({
        "job_id": "unverified_job_2",
        "title": "Engineering Manager",
        "company": "Valon",
        "source": "ashby",
        "status": "approved",
    })
    state_mgr.transition_confirmation("unverified_job_2", "submitting")
    state_mgr.transition_confirmation("unverified_job_2", "submission_unverified")

    tracker = EmailConfirmationTracker(
        state_manager=state_mgr, review_queue_file=tmp_path / "confirmation_review_queue.json"
    )
    fetched = state_mgr.get_job("unverified_job_2")
    outcome = {"confirmed": True}
    tracker._apply_confirmation_transition(fetched, 0.9, outcome)

    assert state_mgr.get_job("unverified_job_2")["confirmation_status"] == "submission_unverified"
    assert outcome["needs_manual_confirmation"] is True


def test_review_queue_write_is_isolated_to_the_injected_path(state_mgr, tmp_path):
    """ACES-448: a flagged review case must land only in the path passed to the
    constructor, and the real project state/confirmation_review_queue.json must
    stay untouched by tests. A test that forgets to inject a path is exactly the
    bug this ticket fixes — assert the isolation directly rather than trusting
    every call site to remember."""
    queue_path = tmp_path / "confirmation_review_queue.json"
    real_snapshot = (
        _CONFIRMATION_REVIEW_QUEUE_FILE.read_bytes() if _CONFIRMATION_REVIEW_QUEUE_FILE.exists() else None
    )

    job = {
        "job_id": "isolation_check_1",
        "title": "Whatever",
        "company": "Whoever",
        "source": "linkedin",
        "status": "applied",
    }
    state_mgr.upsert_job(job)
    fetched = state_mgr.get_job("isolation_check_1")

    tracker = EmailConfirmationTracker(state_manager=state_mgr, review_queue_file=queue_path)
    tracker._apply_confirmation_transition(fetched, 0.9, {"confirmed": True})

    assert queue_path.exists(), "the injected path must receive the write"
    assert "isolation_check_1" in queue_path.read_text()

    real_after = (
        _CONFIRMATION_REVIEW_QUEUE_FILE.read_bytes() if _CONFIRMATION_REVIEW_QUEUE_FILE.exists() else None
    )
    assert real_after == real_snapshot, "the real project state file must be untouched"


def test_review_queue_defaults_to_the_real_project_path_when_not_overridden():
    """Confirms the constructor wiring without exercising a write: the module's
    real path is only the default, and an explicit override always wins."""
    tracker = EmailConfirmationTracker()
    assert tracker.review_queue_file == _CONFIRMATION_REVIEW_QUEUE_FILE


def test_requisition_id_verification_and_mismatch():
    tracker = EmailConfirmationTracker()
    job_with_req = {
        "job_id": "job_req_1",
        "company": "Amazon",
        "title": "Software Development Manager",
        "requisition_id": "REQ-1001",
        "status": "applied",
    }

    # Case 1: Matching requisition ID in email -> verified bonus
    sender = "Amazon Jobs <no-reply@amazon.com>"
    subject = "Application Confirmation"
    body = "Thank you for applying for Software Development Manager. Requisition ID: REQ-1001"
    score_match, ev_match = tracker.calculate_match_score(sender, subject, body, None, job_with_req)
    assert ev_match.get("confirmation_id_verified") == "REQ-1001"
    assert score_match >= 0.85

    # Case 2: Mismatched requisition ID in email -> hard veto (score 0.0, cannot confirm)
    body_mismatch = "Thank you for applying. Requisition ID: REQ-9999"
    score_mismatch, ev_mismatch = tracker.calculate_match_score(sender, subject, body_mismatch, None, job_with_req)
    assert "confirmation_id_mismatch" in ev_mismatch
    assert ev_mismatch.get("hard_veto") is True
    assert score_mismatch == 0.0
