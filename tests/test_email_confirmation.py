"""Unit tests for Multi-Signal Email Confirmation Tracker."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import pytest
from src.email_confirmation_tracker import EmailConfirmationTracker
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


def test_confirmation_transition_requires_submission_evidence(state_mgr):
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

    tracker = EmailConfirmationTracker(state_manager=state_mgr)
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

    # Case 2: Mismatched requisition ID in email -> mismatch penalty / no bonus
    body_mismatch = "Thank you for applying. Requisition ID: REQ-9999"
    score_mismatch, ev_mismatch = tracker.calculate_match_score(sender, subject, body_mismatch, None, job_with_req)
    assert "confirmation_id_mismatch" in ev_mismatch
    assert score_mismatch < score_match
