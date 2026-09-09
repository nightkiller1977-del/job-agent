"""Versioned FailureEvidence contract — the payload job-agent ships to the AICC
Coordinator when an apply attempt fails in a way a code repair could fix.

Wire format (camelCase, see incident_reporter.py for transport):
    POST {COORDINATOR_URL}/incidents/job-agent
    {schemaVersion, incidentId, jobId, runId, attemptId, repositorySlug,
     adapterName, vendor, failureStatus, submissionState, blocker,
     challengeDetected, httpStatus, detail, title?}

`repositorySlug` is injected by the reporter (env config), not carried here.

Contract rules honoured:
  - stdlib dataclass + hand-written to_dict() (repo idiom; no pydantic in src/).
  - incident_id is deterministic (f"job-{job_id}-{attempt_id}") — it is the
    idempotency key at the coordinator, so a re-report of the same attempt
    dedupes instead of opening a second repair.
  - No field NAME may contain a RunLog-sensitive substring (see events._SENSITIVE)
    or the audit stream would silently drop it. Enforced by test_failure_evidence.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from .blocker_classifier import BlockerClass, classify

FAILURE_EVIDENCE_SCHEMA_VERSION = "1"

_DETAIL_MAX = 2000


class SubmissionState(str, Enum):
    """How far the submission got, per the idempotency ledger. Mirrors the
    SubmissionLedger phases plus 'not_attempted' for pre-submit failures."""
    NOT_ATTEMPTED = "not_attempted"
    SUBMIT_IN_PROGRESS = "submit_in_progress"
    RECEIPT_VERIFIED = "receipt_verified"
    SUBMISSION_UNVERIFIED = "submission_unverified"


@dataclass
class FailureEvidence:
    incident_id: str            # deterministic: f"job-{job_id}-{attempt_id}"
    job_id: str
    run_id: str
    attempt_id: str
    adapter_name: str
    vendor: str
    host: str                   # hostname only — never a full URL (may carry tokens)
    failure_status: str         # AtsApplyResult.status vocabulary
    submission_state: SubmissionState
    blocker_class: str          # blocker_classifier.BlockerClass value
    detail: str = ""
    blocker: str | None = None  # e.g. 'captcha' / 'login_required' from apply evidence
    challenge_detected: bool = False
    http_status: int | None = None   # navigation response status, when captured
    schema_version: str = FAILURE_EVIDENCE_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.detail = (self.detail or "")[:_DETAIL_MAX]

    def to_dict(self) -> dict:
        """Coordinator wire shape (camelCase). repositorySlug is added by the
        reporter; host/blocker_class/created_at are local context, not wire fields."""
        state = (
            self.submission_state.value
            if isinstance(self.submission_state, SubmissionState)
            else str(self.submission_state)
        )
        return {
            "schemaVersion": self.schema_version,
            "incidentId": self.incident_id,
            "jobId": self.job_id,
            "runId": self.run_id,
            "attemptId": self.attempt_id,
            "adapterName": self.adapter_name,
            "vendor": self.vendor,
            "failureStatus": self.failure_status,
            "submissionState": state,
            "blocker": self.blocker,
            "challengeDetected": self.challenge_detected,
            "httpStatus": self.http_status,
            "detail": self.detail,
            "title": f"job-agent apply failure: {self.adapter_name}/{self.vendor} — {self.failure_status}",
        }


def build_failure_evidence(
    job: dict,
    result,
    *,
    run_id: str,
    vendor: str,
    host: str,
    adapter_name: str,
    ledger_phase: str | None,
    http_status: int | None = None,
) -> FailureEvidence:
    """Assemble a FailureEvidence from an AtsApplyResult + attempt context.

    ledger_phase is the SubmissionLedger phase for the attempt's canonical key
    (None = no marker → the submit was never attempted).
    """
    job_id = str((job or {}).get("job_id") or "")
    attempt_id = str(getattr(result, "attempt_id", "") or "")
    analytics = getattr(result, "analytics", None) or {}
    evidence = analytics.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    blocker = evidence.get("evidence_blocker") or analytics.get("blocker") or None

    if ledger_phase:
        try:
            submission_state = SubmissionState(ledger_phase)
        except ValueError:
            submission_state = SubmissionState.NOT_ATTEMPTED
    else:
        submission_state = SubmissionState.NOT_ATTEMPTED

    return FailureEvidence(
        incident_id=f"job-{job_id}-{attempt_id}",
        job_id=job_id,
        run_id=str(run_id or ""),
        attempt_id=attempt_id,
        adapter_name=adapter_name or "unknown",
        vendor=vendor or "",
        host=host or "",
        failure_status=str(getattr(result, "status", "") or ""),
        submission_state=submission_state,
        blocker_class=classify(getattr(result, "status", None)).value,
        detail=str(getattr(result, "detail", "") or ""),
        blocker=blocker,
        challenge_detected=blocker == "captcha",
        http_status=http_status,
    )


def should_report(result) -> bool:
    """Report only failures a code repair could plausibly fix:
    PERMANENT and UNKNOWN blocker classes, plus the ambiguous
    'submission_unverified' outcome. Transient, auth, and needs-human
    failures have their own recovery routes — do not open repairs for them.
    """
    status = getattr(result, "status", None)
    if status == "submission_unverified":
        return True
    return classify(status) in (BlockerClass.PERMANENT, BlockerClass.UNKNOWN)
