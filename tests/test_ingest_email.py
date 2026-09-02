import argparse
import io
import json
import sqlite3

import pytest

from src.ingest_email import ingest_email_payload, run_ingest_email_command, run_ingest_email_command_async
from src.main import main_async
from src.orchestrator import SOURCE_MAP
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
    results = ingest_email_payload(_payload(), state, scorer=FixedScorer())

    assert [result.status for result in results] == ["inserted"]
    job = state.get_job(results[0].job_id)
    assert job["source"] == "email"
    assert job["status"] == "discovered"
    assert job["score"] == 81
    assert job["score_reason"] == "Strong match; review before apply."
    assert job["url"] == "https://jobs.example.com/acme/backend"
    assert job["description"] == "Senior Backend Engineer at Acme Cloud"

    extra = json.loads(job["extra_json"])
    assert extra["email_source"]["source_event_id"] == "icloud-mail-mcp:jobs:<alert@example.com>"
    assert extra["email_source"]["content_hash"] == "abc123"
    assert extra["email_source"]["redacted_excerpt"] == "Senior Backend Engineer at Acme Cloud"
    assert extra["recommended_action"] == "review"
    assert "body" not in json.dumps(extra).lower()


def test_email_source_routes_to_external_ats_apply_path():
    assert "email" in SOURCE_MAP
    assert SOURCE_MAP["email"] is SOURCE_MAP["external"]


def test_duplicate_source_event_and_job_returns_duplicate_without_second_row(temp_db):
    state = StateManager(temp_db)
    first = ingest_email_payload(_payload(), state, scorer=FixedScorer())
    second = ingest_email_payload(_payload(), state, scorer=FixedScorer())

    assert first[0].status == "inserted"
    assert second[0].status == "duplicate"
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
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://[fd00::1]/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://[fe90::1]/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://2130706433/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://0177.0.0.1/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://127.1/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://0x7f.0.0.1/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://localhost./1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://jobs.localhost/1"}]), "private or local"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "http://[::1"}]), "absolute http(s)"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "https://example.com:bad/1"}]), "absolute http(s)"),
        (_payload(jobs=[{"title": "Role", "company": "Acme", "apply_url": "https://click.example.com/1"}]), "tracking"),
        (_payload(received_at={"bad": "date"}), "received_at must be an ISO-8601 string"),
        (_payload(received_at="not-a-date"), "received_at must be an ISO-8601 string"),
    ],
)
def test_validation_rejects_missing_identity_and_invalid_urls(temp_db, payload, error):
    with pytest.raises(ValueError) as exc:
        ingest_email_payload(payload, StateManager(temp_db))
    assert error in str(exc.value)


def test_validation_allows_legitimate_clickhouse_hostname(temp_db):
    state = StateManager(temp_db)
    payload = _payload(jobs=[{
        "title": "Database Engineering Manager",
        "company": "ClickHouse",
        "location": "Remote",
        "apply_url": "https://careers.clickhouse.com/roles/EngineeringManager",
        "redacted_excerpt": "Database Engineering Manager at ClickHouse",
        "lane": "job-agent-review",
    }])

    result = ingest_email_payload(payload, state, scorer=FixedScorer())[0]

    assert result.status == "inserted"


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
    results = ingest_email_payload(payload, state, scorer=FixedScorer())
    job = state.get_job(results[0].job_id)
    extra = json.loads(job["extra_json"])

    assert results[0].status == "inserted"
    assert job["status"] == "discovered"
    assert job["url"] == ""
    assert extra["email_source"]["lane"] == "review-follow-up"
    assert extra["email_source"]["rejected_reason"] == "missing_url"


def test_email_ingest_scores_before_lifecycle_decision(temp_db):
    state = StateManager(temp_db)
    results = ingest_email_payload(_payload(), state, scorer=FixedScorer(
        score=12,
        reason="Role mismatch.",
        flags="SKIP",
        action="skip",
    ))
    job = state.get_job(results[0].job_id)

    assert results[0].status == "inserted"
    assert job["status"] == "skipped"
    assert job["score"] == 12
    assert job["score_reason"] == "Role mismatch."


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


@pytest.mark.asyncio
async def test_async_command_runs_inside_existing_event_loop(monkeypatch, capsys, temp_db):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    args = argparse.Namespace(json_stdin=True, db_path=temp_db)

    code = await run_ingest_email_command_async(args, scorer=FixedScorer())

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"


@pytest.mark.asyncio
async def test_main_async_ingest_email_uses_real_cli_boundary(monkeypatch, capsys, temp_db):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    monkeypatch.setattr("src.ingest_email.JobScorer", ConfigAwareFixedScorer)

    code = await main_async(argparse.Namespace(command="ingest-email", json_stdin=True, db_path=temp_db))

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"


@pytest.mark.asyncio
async def test_command_classifies_malformed_json_as_invalid(monkeypatch, capsys, temp_db):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"source_event_id":'))
    args = argparse.Namespace(json_stdin=True, db_path=temp_db)

    code = await run_ingest_email_command_async(args, scorer=FixedScorer())

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "invalid"


def test_command_rejects_missing_json_stdin(capsys, temp_db):
    code = run_ingest_email_command(argparse.Namespace(json_stdin=False, db_path=temp_db))

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "invalid"


class FixedScorer:
    def __init__(self, score=81, reason="Strong match; review before apply.", flags="FLAG_FOR_REVIEW", action="review"):
        self.result = (score, reason, flags, action)

    async def score(self, job):
        return self.result


class ConfigAwareFixedScorer(FixedScorer):
    def __init__(self, config=None):
        super().__init__()
        self.config = config or {}
