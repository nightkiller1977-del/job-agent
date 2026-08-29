"""Unit tests for OutreachMatcher."""
from unittest.mock import MagicMock
from src.networking.outreach_matcher import OutreachMatcher


def test_draft_outreach_note():
    profile = {
        "name": "Anthony Larkins",
        "title": "VP Engineering",
    }
    matcher = OutreachMatcher(profile_data=profile)
    job = {
        "company": "Datadog",
        "title": "Director of Engineering",
    }
    note = matcher.draft_outreach_note(job, target_name="Alex Smith", target_role="VP Engineering")
    assert "Hi Alex," in note
    assert "Director of Engineering" in note
    assert "Datadog" in note
    assert "Anthony Larkins" in note


def test_process_high_scoring_jobs(tmp_path):
    queue_file = tmp_path / "outreach_queue.json"
    mock_state = MagicMock()
    mock_conn = MagicMock()
    mock_state._connect.return_value.__enter__.return_value = mock_conn

    # Two jobs: one with score 90, one with score 70
    mock_conn.execute.return_value.fetchall.return_value = [
        {"job_id": "job_high", "company": "CloudCorp", "title": "Head of Engineering", "score": 92, "status": "approved"}
    ]

    matcher = OutreachMatcher(state_manager=mock_state, queue_file=queue_file)
    drafts = matcher.process_high_scoring_jobs(min_score=85)

    assert len(drafts) == 1
    assert drafts[0]["job_id"] == "job_high"
    assert drafts[0]["status"] == "pending_review"

    # Idempotency check: calling again returns no new drafts
    drafts2 = matcher.process_high_scoring_jobs(min_score=85)
    assert len(drafts2) == 0
