import argparse
import concurrent.futures
import io
import json
import sqlite3

import pytest

from src.ingest_email import ingest_email_payload, run_ingest_email_command
from src.state_manager import StateManager


@pytest.fixture
def temp_db(tmp_path):
    return str(tmp_path / "jobs.db")


def _payload(**overrides):
    payload = {
        "source_event_id": "icloud-mail-mcp:jobs:<alert@example.com>",
        "received_at": "2026-09-01T12:00:00+00:00",
        "source": {
            "provider": "icloud-mail-mcp",
            "account": "jobs",
            "message_id": "icloud-mail-mcp:jobs:<alert@example.com>",
            "content_hash": "abc123",
        },
        "jobs": [{
            "title": "Senior Backend Engineer",
            "company": "Acme Cloud",
            "location": "Remote",
            "apply_url": "https://jobs.example.com/acme/backend",
            "redacted_excerpt": "Senior Backend Engineer at Acme Cloud",
            "lane": "job-agent-review",
        }],
    }
    payload.update(overrides)
    return payload


def test_inserted_email_job_defaults_to_discovered_and_preserves_redacted_metadata(temp_db):
    state = StateManager(temp_db)
    results = ingest_email_payload(_payload(), state)

    assert [result.status for result in results] == ["inserted"]
    job = state.get_job(results[0].job_id)
    assert job["source"] == "email"
    assert job["status"] == "discovered"
    assert job["url"] == "https://jobs.example.com/acme/backend"
    assert job["description"] == "Senior Backend Engineer at Acme Cloud"

    extra = json.loads(job["extra_json"])
    assert extra["email_source"]["source_event_id"] == "icloud-mail-mcp:jobs:<alert@example.com>"
    assert extra["email_source"]["content_hash"] == "abc123"
    assert extra["email_source"]["redacted_excerpt"] == "Senior Backend Engineer at Acme Cloud"
    assert "body" not in json.dumps(extra).lower()


def test_duplicate_source_event_and_job_returns_duplicate_without_second_row(temp_db):
    state = StateManager(temp_db)
    first = ingest_email_payload(_payload(), state)
    second = ingest_email_payload(_payload(), state)

    assert first[0].status == "inserted"
    assert second[0].status == "duplicate"
    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_duplicate_email_across_accounts_and_folders_uses_apply_url_idempotency(temp_db):
    state = StateManager(temp_db)
    original = _payload()
    forwarded = _payload(
        source_event_id="icloud-mail-mcp:other:<forwarded@example.com>",
        source={
            "provider": "icloud-mail-mcp",
            "account": "other-account",
            "message_id": "icloud-mail-mcp:other:<forwarded@example.com>",
            "content_hash": "different-email-copy",
        },
    )

    first = ingest_email_payload(original, state)
    second = ingest_email_payload(forwarded, state)

    assert first[0].status == "inserted"
    assert second[0].status == "duplicate"
    assert first[0].job_id == second[0].job_id
    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


def test_sqlite_contention_duplicate_ingests_create_one_job(temp_db):
    payload = _payload()

    def ingest_once():
        state = StateManager(temp_db)
        try:
            return ingest_email_payload(payload, state)[0].status
        finally:
            state.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: ingest_once(), range(2)))

    assert statuses == ["duplicate", "inserted"]
    with sqlite3.connect(temp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    "payload,error",
    [
        ({}, "source_event_id is required"),
        (_payload(source={"provider": "icloud-mail-mcp"}), "source.account is required"),
        (_payload(jobs=[{"company": "Acme Cloud", "apply_url": "https://jobs.example.com/1"}]), "jobs[0].title is required"),
        (_payload(jobs=[{"title": "Role", "apply_url": "https://jobs.example.com/1"}]), "jobs[0].company is required"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "ftp://jobs.example.com/1"}]), "absolute http(s)"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://127.0.0.1/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "https://click.example.com/1"}]), "tracking"),
    ],
)
def test_validation_rejects_missing_identity_and_invalid_urls(temp_db, payload, error):
    with pytest.raises(ValueError) as exc:
        ingest_email_payload(payload, StateManager(temp_db))
    assert error in str(exc.value)


def test_no_url_recruiter_review_lane_is_valid_and_not_approved(temp_db):
    payload = _payload(jobs=[{
        "title": "Staff Platform Engineer",
        "company": "Northstar Systems",
        "apply_url": "",
        "lane": "review-follow-up",
        "redacted_excerpt": "Recruiter asked for availability.",
        "rejected_reason": "missing_url",
    }])

    state = StateManager(temp_db)
    results = ingest_email_payload(payload, state)
    job = state.get_job(results[0].job_id)
    extra = json.loads(job["extra_json"])

    assert results[0].status == "inserted"
    assert job["status"] == "discovered"
    assert job["url"] == ""
    assert extra["email_source"]["lane"] == "review-follow-up"
    assert extra["email_source"]["rejected_reason"] == "missing_url"


def test_operational_failure_returns_failed_result():
    class BrokenState:
        def upsert_job(self, job):
            raise RuntimeError("database unavailable")

    results = ingest_email_payload(_payload(), BrokenState())

    assert results[0].status == "failed"
    assert results[0].reason == "database unavailable"


def test_command_reads_json_from_stdin_and_returns_status(monkeypatch, capsys, temp_db):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    args = argparse.Namespace(json_stdin=True, db_path=temp_db)

    code = run_ingest_email_command(args)

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["results"][0]["status"] == "inserted"


def test_command_rejects_missing_json_stdin(capsys, temp_db):
    code = run_ingest_email_command(argparse.Namespace(json_stdin=False, db_path=temp_db))

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "invalid"
