"""Unit tests for Multi-Signal Email Confirmation Tracker."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from src.email_confirmation_tracker import EmailConfirmationTracker


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
    assert evidence.get("confirmation_id") == "REQ-98412"


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
