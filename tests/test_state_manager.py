import pytest
import sqlite3
import tempfile
import json
from pathlib import Path
from src.state_manager import StateManager

@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_jobs.db"
        yield str(db_path)

def test_delete_job(temp_db):
    state = StateManager(db_path=temp_db)
    
    job = {
        "job_id": "test123",
        "source": "linkedin",
        "title": "Software Engineer",
        "company": "Test Company",
        "status": "discovered",
    }
    
    # Insert job
    assert state.upsert_job(job) is True
    assert state.get_job("test123") is not None
    
    # Delete job
    state.delete_job("test123")
    assert state.get_job("test123") is None

def test_set_status_expired_deletes_job(temp_db):
    state = StateManager(db_path=temp_db)
    
    job = {
        "job_id": "test456",
        "source": "linkedin",
        "title": "Software Engineer",
        "company": "Test Company",
        "status": "discovered",
    }
    
    # Insert job
    assert state.upsert_job(job) is True
    assert state.get_job("test456") is not None
    
    # Set status to expired
    state.set_status("test456", "expired")
    
    # Verify job is deleted (no longer in DB)
    assert state.get_job("test456") is None

def test_init_db_removes_existing_expired_jobs(temp_db):
    # Manually create db and insert an expired job using raw sqlite
    conn = sqlite3.connect(temp_db)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id      TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        title       TEXT NOT NULL,
        company     TEXT,
        location    TEXT,
        salary_raw  TEXT,
        remote_type TEXT,
        url         TEXT,
        description TEXT,
        score       INTEGER,
        score_reason TEXT,
        flags       TEXT,
        status      TEXT NOT NULL DEFAULT 'discovered',
        discovered_at TEXT NOT NULL,
        reviewed_at   TEXT,
        applied_at    TEXT,
        extra_json  TEXT
    );
    """)
    conn.execute(
        "INSERT INTO jobs (job_id, source, title, status, discovered_at) VALUES (?, ?, ?, ?, ?)",
        ("exp1", "indeed", "Expired Job", "expired", "2026-06-12T00:00:00")
    )
    conn.execute(
        "INSERT INTO jobs (job_id, source, title, status, discovered_at) VALUES (?, ?, ?, ?, ?)",
        ("act1", "indeed", "Active Job", "discovered", "2026-06-12T00:00:00")
    )
    conn.commit()
    conn.close()
    
    # Initialize StateManager on the pre-populated database
    state = StateManager(db_path=temp_db)
    
    # Verify expired job was deleted on init, but active job remains
    assert state.get_job("exp1") is None
    assert state.get_job("act1") is not None

def test_record_apply_attempt_merges_metadata_without_truncating(temp_db):
    state = StateManager(db_path=temp_db)
    state.upsert_job({"job_id": "meta1", "source": "linkedin", "title": "Role"})
    long_detail = "x" * 700
    metrics = {
        "apply_validation_metrics": {
            "failure_type": "keyword_coverage",
            "detail": long_detail,
        }
    }

    state.record_apply_attempt("meta1", "keyword_coverage_failed", "short", metadata=metrics)

    extra = json.loads(state.get_job("meta1")["extra_json"])
    assert extra["apply_last_detail"] == "short"
    assert extra["apply_validation_metrics"]["detail"] == long_detail

def test_reset_failed_keyword_jobs_clears_circuit_and_attempt_metadata(temp_db):
    state = StateManager(db_path=temp_db)
    state.upsert_job({"job_id": "kw1", "source": "linkedin", "title": "Role", "status": "approved"})
    state.record_apply_attempt(
        "kw1",
        "keyword_coverage_failed",
        "ATS check failed: Keyword coverage low",
        metadata={"apply_validation_metrics": {"failure_type": "keyword_coverage"}},
    )
    state.flag_circuit_break("kw1", "needs_human", "cap reached")

    stats = state.reset_failed_keyword_jobs(dry_run=False)

    assert stats == {"matched": 1, "reset": 1, "unmatched": 0}
    extra = json.loads(state.get_job("kw1")["extra_json"] or "{}")
    for key in (
        "apply_last_status",
        "apply_attempt_count",
        "apply_last_detail",
        "submitted",
        "blocker_class",
        "circuit_broken",
        "circuit_class",
        "circuit_reason",
        "circuit_broken_at",
        "apply_last_attempt",
        "apply_validation_metrics",
    ):
        assert key not in extra
