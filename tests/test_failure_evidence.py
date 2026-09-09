"""FailureEvidence contract tests — schema versioning, wire names, deterministic
incident ids, should_report routing, and the RunLog sensitive-field-name guard."""
import dataclasses

import pytest

from src import events
from src.failure_evidence import (
    FAILURE_EVIDENCE_SCHEMA_VERSION,
    FailureEvidence,
    SubmissionState,
    build_failure_evidence,
    should_report,
)
from src.sources.adapters.context import AtsApplyResult


JOB = {"job_id": "j-42", "url": "https://boards.greenhouse.io/acme/jobs/123"}

EXPECTED_WIRE_KEYS = {
    "schemaVersion", "incidentId", "jobId", "runId", "attemptId",
    "adapterName", "vendor", "failureStatus", "submissionState",
    "blocker", "challengeDetected", "httpStatus", "detail", "title",
}


def _result(status="bad_ats_url", detail="404 posting gone", attempt_id="a" * 32,
            **analytics):
    res = AtsApplyResult.blocked(status, detail, **analytics)
    res.attempt_id = attempt_id  # blocked() routes kwargs to analytics, not fields
    return res


def _build(result=None, **kw):
    result = result or _result()
    defaults = dict(
        run_id="run-1", vendor="greenhouse", host="boards.greenhouse.io",
        adapter_name="greenhouse", ledger_phase=None, http_status=404,
    )
    defaults.update(kw)
    return build_failure_evidence(JOB, result, **defaults)


def test_schema_version_present_and_string():
    ev = _build()
    assert ev.schema_version == FAILURE_EVIDENCE_SCHEMA_VERSION == "1"
    assert ev.to_dict()["schemaVersion"] == "1"


def test_to_dict_wire_names():
    ev = _build()
    wire = ev.to_dict()
    assert set(wire) == EXPECTED_WIRE_KEYS
    assert wire["jobId"] == "j-42"
    assert wire["runId"] == "run-1"
    assert wire["attemptId"] == "a" * 32
    assert wire["adapterName"] == "greenhouse"
    assert wire["vendor"] == "greenhouse"
    assert wire["failureStatus"] == "bad_ats_url"
    assert wire["submissionState"] == "not_attempted"
    assert wire["httpStatus"] == 404
    # repositorySlug is injected by the reporter, never by the contract
    assert "repositorySlug" not in wire


def test_incident_id_deterministic():
    res = _result(attempt_id="deadbeef")
    ev1 = _build(result=res)
    ev2 = _build(result=res)
    assert ev1.incident_id == ev2.incident_id == "job-j-42-deadbeef"


def test_submission_state_from_ledger_phase():
    assert _build(ledger_phase=None).submission_state is SubmissionState.NOT_ATTEMPTED
    assert _build(ledger_phase="submit_in_progress").submission_state is SubmissionState.SUBMIT_IN_PROGRESS
    assert _build(ledger_phase="receipt_verified").submission_state is SubmissionState.RECEIPT_VERIFIED
    assert _build(ledger_phase="submission_unverified").submission_state is SubmissionState.SUBMISSION_UNVERIFIED
    # unknown phases degrade safely instead of raising
    assert _build(ledger_phase="???").submission_state is SubmissionState.NOT_ATTEMPTED


def test_blocker_derivation_and_challenge_flag():
    res = _result(attempt_id="a1")
    res.analytics["evidence"] = {"evidence_blocker": "captcha"}
    ev = _build(result=res)
    assert ev.blocker == "captcha"
    assert ev.challenge_detected is True

    res2 = _result(attempt_id="a2", blocker="login_required")
    ev2 = _build(result=res2)
    assert ev2.blocker == "login_required"
    assert ev2.challenge_detected is False

    ev3 = _build()
    assert ev3.blocker is None
    assert ev3.challenge_detected is False


def test_blocker_class_stamped():
    assert _build().blocker_class == "permanent"
    unv = AtsApplyResult.unverified("clicked, no receipt")
    assert _build(result=unv).blocker_class == "unknown"


def test_detail_truncated_to_2000():
    res = _result(detail="x" * 5000, attempt_id="a1")
    ev = _build(result=res)
    assert len(ev.detail) == 2000
    assert len(ev.to_dict()["detail"]) == 2000


@pytest.mark.parametrize("status,expected", [
    ("bad_ats_url", True),               # PERMANENT
    ("unknown_source", True),            # PERMANENT
    ("some_brand_new_status", True),     # UNKNOWN
    ("submission_unverified", True),     # explicit ambiguity
    ("browser_timeout", False),          # TRANSIENT
    ("external_ats_error", False),       # TRANSIENT
    ("workday_session_expired", False),  # AUTH_REQUIRED
    ("submit_not_found", False),         # NEEDS_HUMAN
    ("applied", False),                  # SUCCESS
])
def test_should_report_matrix(status, expected):
    res = AtsApplyResult.blocked(status)
    res.status = status  # blocked() keeps the given status; explicit for clarity
    assert should_report(res) is expected


def test_no_sensitive_substring_field_names():
    """Every dataclass field name AND wire key must survive events._SANITIZE —
    RunLog drops any field whose name contains a sensitive substring."""
    names = [f.name for f in dataclasses.fields(FailureEvidence)]
    names += list(_build().to_dict().keys())
    for name in names:
        assert not any(s in name.lower() for s in events._SENSITIVE), (
            f"field name {name!r} contains a RunLog-sensitive substring"
        )
