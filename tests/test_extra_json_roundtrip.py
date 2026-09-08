"""Regression tests for the extra_json double-nesting bug.

Production evidence (sanitized): every job pulled back from the cloud
dashboard's /api/jobs/approved endpoint was re-upserted with its Postgres row
columns intact, so upsert_job() treated the `extra_json` string and
`updated_at` column as job "extras" and produced:

    {"updated_at": "2026-08-05T...", "extra_json": "{\"has_easy_apply\": true, ...}"}

All flat consumers (apply readiness classification, attempt counting, the
circuit breaker) then saw an empty dict. These tests pin the fix:
upsert_job() must never nest, and parse_extra_json() must heal legacy rows.
"""
import json

import pytest

from src.state_manager import StateManager, parse_extra_json


# Synthesized equivalent of a real corrupted row (no real employer/user data).
INNER = {
    "has_easy_apply": True,
    "search_query": "Director of Engineering",
    "recommended_action": "apply",
    "apply_last_attempt": "2026-08-05T03:26:49.622868",
    "apply_last_status": "linkedin_error",
    "apply_last_detail": "Page.evaluate: Target page, context or browser has been closed",
    "apply_attempt_count": 2,
    "submitted": False,
    "blocker_class": "unknown",
}
NESTED = json.dumps({
    "updated_at": "2026-08-05T03:26:49.942927+00:00",
    "extra_json": json.dumps(INNER),
})


@pytest.fixture()
def state(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "jobs.db"))
    yield sm
    sm.close()


def _dashboard_row(job_id="job-abc-123"):
    """A row shaped like the cloud dashboard's /api/jobs/approved response."""
    return {
        "job_id": job_id,
        "source": "linkedin",
        "title": "Director of Engineering",
        "company": "ExampleCorp",
        "location": "Remote",
        "salary_raw": "$200K/yr",
        "remote_type": "remote",
        "url": "https://example.com/jobs/123",
        "description": "Lead a team of managers.",
        "score": 90,
        "score_reason": "Strong fit.",
        "flags": "",
        "status": "approved",
        "discovered_at": "2026-07-01T00:00:00",
        "reviewed_at": None,
        "applied_at": None,
        # Postgres-only columns that must not leak into extras:
        "updated_at": "2026-08-05T03:26:49.942927+00:00",
        "created_at": "2026-07-01T00:00:00+00:00",
        "extra_json": json.dumps(INNER),
    }


class TestParseExtraJson:
    def test_flat_passthrough(self):
        assert parse_extra_json(json.dumps(INNER)) == INNER

    def test_heals_single_nesting(self):
        extra = parse_extra_json(NESTED)
        assert extra["apply_last_status"] == "linkedin_error"
        assert extra["apply_attempt_count"] == 2
        assert "extra_json" not in extra
        assert "updated_at" not in extra

    def test_heals_double_nesting(self):
        double = json.dumps({"updated_at": "2026-08-06T00:00:00", "extra_json": NESTED})
        extra = parse_extra_json(double)
        assert extra["recommended_action"] == "apply"
        assert "extra_json" not in extra

    def test_none_empty_and_garbage(self):
        assert parse_extra_json(None) == {}
        assert parse_extra_json("") == {}
        assert parse_extra_json("not json{") == {}
        assert parse_extra_json(json.dumps([1, 2])) == {}

    def test_dict_input(self):
        assert parse_extra_json(dict(INNER)) == INNER


class TestUpsertRoundtrip:
    def test_dashboard_row_upsert_stays_flat(self, state):
        assert state.upsert_job(_dashboard_row()) is True
        job = state.get_job("job-abc-123")
        extra = json.loads(job["extra_json"])
        # Flat keys visible, no wrapper, no Postgres metadata.
        assert extra["apply_last_status"] == "linkedin_error"
        assert extra["has_easy_apply"] is True
        assert "extra_json" not in extra
        assert "updated_at" not in extra
        assert "created_at" not in extra

    def test_record_apply_attempt_after_pull_increments_count(self, state):
        state.upsert_job(_dashboard_row())
        state.record_apply_attempt("job-abc-123", status="needs-answer", detail="stuck on page 3/4")
        extra = json.loads(state.get_job("job-abc-123")["extra_json"])
        # Pre-fix, the nested wrapper hid apply_attempt_count, resetting it to 1.
        assert extra["apply_attempt_count"] == 3
        assert extra["apply_last_status"] == "needs-answer"

    def test_record_apply_attempt_heals_legacy_nested_row(self, state):
        # Simulate a row corrupted by the old code path.
        row = _dashboard_row("job-legacy-1")
        row.pop("extra_json")
        row.pop("updated_at")
        row.pop("created_at")
        state.upsert_job(row)
        with state._connect() as conn:
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (NESTED, "job-legacy-1"),
            )
        state.record_apply_attempt("job-legacy-1", status="applied", detail="ok")
        extra = json.loads(state.get_job("job-legacy-1")["extra_json"])
        assert extra["apply_attempt_count"] == 3  # 2 prior attempts preserved
        assert extra["submitted"] is True
        assert "extra_json" not in extra
