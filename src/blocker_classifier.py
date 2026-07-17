"""P2: authoritative apply-failure classification + circuit-breaker policy.

The apply loop re-attempts every 'approved' job on every run with no gate — the
measured baseline showed jobs retried up to 17× and 245 total wasted retries on
jobs that never succeed. This module maps each apply-outcome status to a
*control-flow* class and a per-class retry cap, so the orchestrator can stop
attempting unwinnable jobs and route them to the right place.

Distinct from StateManager's `_FAILURE_CLUSTERS`, which is a coarse *display*
grouping for the success report. This is the retry-decision authority.
"""
from __future__ import annotations

import os
from enum import Enum

import re
from typing import Dict, List, Optional
from .auth_constants import HUMAN_SOURCES, AUTOMATED_SOURCES, REAUTH_CREDS
from .apply_outcome import ApplyOutcomeCode


class BlockerClass(str, Enum):
    SUCCESS = "success"            # applied — never re-attempt
    TRANSIENT = "transient"       # network/timeout/5xx/bot-block — retry a few times
    AUTH_REQUIRED = "auth_required"  # session/login — route to reauth, don't burn attempts blindly
    NEEDS_HUMAN = "needs_human"   # page-structure/field/submit issue — surface, don't blind-retry
    PERMANENT = "permanent"       # bad url / unknown source — never retry
    UNKNOWN = "unknown"           # unmapped status — cautious retry


# Explicit status → class map
_STATUS_TO_CLASS: dict[ApplyOutcomeCode, BlockerClass] = {
    ApplyOutcomeCode.APPLIED: BlockerClass.SUCCESS,
    
    # transient
    ApplyOutcomeCode.EXTERNAL_ATS_ERROR: BlockerClass.TRANSIENT,
    ApplyOutcomeCode.BROWSER_TIMEOUT: BlockerClass.TRANSIENT,
    ApplyOutcomeCode.MODEL_TIMEOUT: BlockerClass.TRANSIENT,
    ApplyOutcomeCode.UNKNOWN_EXTERNAL_ATS_ERROR: BlockerClass.TRANSIENT,
    ApplyOutcomeCode.ERROR: BlockerClass.TRANSIENT,
    ApplyOutcomeCode.REAUTH_RETRY_ERROR: BlockerClass.TRANSIENT,
    
    # auth
    ApplyOutcomeCode.SESSION_EXPIRED: BlockerClass.AUTH_REQUIRED,
    ApplyOutcomeCode.LOGIN_REQUIRED: BlockerClass.AUTH_REQUIRED,
    ApplyOutcomeCode.REAUTH_FAILED: BlockerClass.AUTH_REQUIRED,
    ApplyOutcomeCode.NEEDS_SESSION: BlockerClass.AUTH_REQUIRED,
    ApplyOutcomeCode.NEEDS_SESSION_PREP: BlockerClass.AUTH_REQUIRED,
    ApplyOutcomeCode.HUMAN_ACTION_REQUIRED: BlockerClass.AUTH_REQUIRED,
    
    # config / permanent
    ApplyOutcomeCode.CREDENTIALS_MISSING: BlockerClass.PERMANENT,
    ApplyOutcomeCode.BAD_ATS_URL: BlockerClass.PERMANENT,
    ApplyOutcomeCode.UNKNOWN_SOURCE: BlockerClass.PERMANENT,
    ApplyOutcomeCode.MISSING_ATS_URL: BlockerClass.PERMANENT,
    ApplyOutcomeCode.INDEED_EASY_APPLY_OR_NO_ATS: BlockerClass.PERMANENT,
    
    # needs human
    ApplyOutcomeCode.SUBMIT_NOT_FOUND: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.FORM_NOT_REACHED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.FORM_NOT_DETECTED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.STUCK_ON_REQUIRED_FIELD: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.EXTERNAL_APPLY_NOT_FOUND: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.ATS_FAILURE: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.KEYWORD_COVERAGE_FAILED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.PDF_TEXT_LAYER_FAILED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.RESUME_UPLOAD_FAILED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.ATS_SELECTOR_FAILED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.REQUIRED_FIELD_UNANSWERED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.FORM_EMPTY_NOT_SUBMITTED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.SUBMISSION_CANCELLED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.SUBMISSION_UNVERIFIED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.STEP_BLOCKED: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.NEEDS_ANSWER: BlockerClass.NEEDS_HUMAN,
    ApplyOutcomeCode.NEEDS_HYDRATION: BlockerClass.NEEDS_HUMAN,
    
    ApplyOutcomeCode.UNKNOWN: BlockerClass.UNKNOWN,
}

# Per-class attempt caps. Once apply_attempt_count reaches the cap for a job's
# last-status class, the circuit opens and the job is skipped.
_MAX_ATTEMPTS: dict[BlockerClass, int] = {
    BlockerClass.SUCCESS: 0,
    BlockerClass.TRANSIENT: 3,
    BlockerClass.AUTH_REQUIRED: 5,   # allow a few reauth cycles, then stop
    BlockerClass.NEEDS_HUMAN: 1,     # surface immediately; don't blind-retry
    BlockerClass.PERMANENT: 0,       # never retry
    BlockerClass.UNKNOWN: 2,
}


def classify(status: str | ApplyOutcomeCode | None) -> BlockerClass:
    """Map an ApplyOutcomeCode to its control-flow class."""
    if not status:
        return BlockerClass.UNKNOWN
    
    if isinstance(status, ApplyOutcomeCode):
        code = status
    else:
        try:
            code = ApplyOutcomeCode(status.strip())
        except ValueError:
            return BlockerClass.UNKNOWN

    return _STATUS_TO_CLASS.get(code, BlockerClass.UNKNOWN)


def max_attempts(status: str | None) -> int:
    """Retry cap for the given status's class."""
    return _MAX_ATTEMPTS[classify(status)]


def needs_preflight_reauth(
    last_status: str | None, source: str, already_reauthed: set[str]
) -> bool:
    """P3: should we proactively refresh this source's session BEFORE attempting?

    True when the job's last outcome was an auth blocker and we haven't already
    re-authed this source in the current run. Turns the reactive "attempt → fail
    on auth → maybe reauth next run" pattern into "reauth first → attempt".
    """
    return (
        classify(last_status) is BlockerClass.AUTH_REQUIRED
        and source not in already_reauthed
    )


def preflight_reauth_viable(source: str) -> tuple[bool, str]:
    """P3: is a *proactive* reauth worth attempting in an unattended apply run?

    Returns (viable, reason_if_not). Prevents the apply loop from triggering
    doomed reauths that either block on a human-login timeout or fail on missing
    credentials — turning a 10-minute block / scary error into a clean skip.

    - Human sources (usajobs): not viable mid-apply — they wait for a person and
      time out. Handle via `prepare-sessions` instead → "needs_session_prep".
    - Automated sources missing credentials: not viable — check if a source in AUTOMATED_SOURCES lacks env credentials
    """
    if source in HUMAN_SOURCES:
        return False, "needs_session_prep"
    
    if source in AUTOMATED_SOURCES:
        missing = [c for c in REAUTH_CREDS.get(source, ()) if not os.environ.get(c)]
        if missing:
            return False, "credentials_missing"
    return True, ""


def should_attempt(last_status: str | None, attempt_count: int) -> tuple[bool, str]:
    """Decide whether to attempt a job given its last outcome and attempt count.

    Returns (attempt, skip_reason). skip_reason is empty when attempt is True.
    A job never attempted (no last_status) is always attempted.
    """
    if not last_status:
        return True, ""  # never tried — always attempt

    cls = classify(last_status)
    if cls is BlockerClass.SUCCESS:
        return False, "already applied"

    cap = _MAX_ATTEMPTS[cls]
    if cap == 0:
        return False, f"{cls.value} blocker — will not retry: {last_status}"
    if attempt_count >= cap:
        return False, f"{cls.value} retry cap reached ({attempt_count}/{cap}): {last_status}"
    return True, ""
