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

# Mirror of reauth.HUMAN_SOURCES + per-source credential env pairs. Kept here (a
# pure, import-light module) so the preflight guard is testable without pulling
# reauth.py's playwright/browser import chain. If reauth's sets change, update both.
#
# HUMAN_SOURCES now means "automated first, human as the fallback" (ACES-283/286):
# ReauthManager tries the stored-credential login (+ TOTP / emailed code) and only
# then notifies a person. So a proactive mid-apply reauth is viable for these
# sources exactly like any automated one — gated on credentials, not on the source.
_HUMAN_FALLBACK_SOURCES = {"usajobs"}
_REAUTH_CREDS = {
    "jobright": ("JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD"),
    "linkedin": ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"),
    "indeed": ("INDEED_EMAIL", "INDEED_PASSWORD"),
    "usajobs": ("USAJOBS_EMAIL", "USAJOBS_PASSWORD"),
}


class BlockerClass(str, Enum):
    SUCCESS = "success"            # applied — never re-attempt
    TRANSIENT = "transient"       # network/timeout/5xx/bot-block — retry a few times
    AUTH_REQUIRED = "auth_required"  # session/login — route to reauth, don't burn attempts blindly
    NEEDS_HUMAN = "needs_human"   # page-structure/field/submit issue — surface, don't blind-retry
    PERMANENT = "permanent"       # bad url / unknown source — never retry
    UNKNOWN = "unknown"           # unmapped status — cautious retry


# Explicit status → class map (statuses observed in the live DB + known emitters).
_STATUS_TO_CLASS: dict[str, BlockerClass] = {
    "applied": BlockerClass.SUCCESS,
    # transient — worth a bounded retry (external_ats_error is often bot-detection,
    # which the patchright work may fix; cap keeps it from looping forever)
    "external_ats_error": BlockerClass.TRANSIENT,
    "browser_timeout": BlockerClass.TRANSIENT,
    "model_timeout": BlockerClass.TRANSIENT,
    "unknown_external_ats_error": BlockerClass.TRANSIENT,
    "error": BlockerClass.TRANSIENT,
    "reauth_retry_error": BlockerClass.TRANSIENT,
    # auth — route to reauth / session prep
    "workday_session_expired": BlockerClass.AUTH_REQUIRED,
    "brassring_login_required": BlockerClass.AUTH_REQUIRED,
    "microsoft_login_required": BlockerClass.AUTH_REQUIRED,
    "smartrecruiters_login_required": BlockerClass.AUTH_REQUIRED,
    "teamtailor_login_required": BlockerClass.AUTH_REQUIRED,
    "reauth_failed": BlockerClass.AUTH_REQUIRED,
    "session_expired": BlockerClass.AUTH_REQUIRED,
    "usajobs_login_required": BlockerClass.AUTH_REQUIRED,  # historic rows; apply now raises AuthFailedError instead
    "needs_session_prep": BlockerClass.AUTH_REQUIRED,  # P3: human source, run prepare-sessions
    # config — user must fix .env / creds; never auto-retry
    "credentials_missing": BlockerClass.PERMANENT,
    # needs human — retrying without a code/profile fix won't help
    "submit_not_found": BlockerClass.NEEDS_HUMAN,
    "form_not_reached": BlockerClass.NEEDS_HUMAN,
    "linkedin_stuck_on_required_field": BlockerClass.NEEDS_HUMAN,
    "linkedin_external_apply_not_found": BlockerClass.NEEDS_HUMAN,
    "microsoft_apply_not_reached": BlockerClass.NEEDS_HUMAN,
    "ats_failure": BlockerClass.NEEDS_HUMAN,
    "keyword_coverage_failed": BlockerClass.NEEDS_HUMAN,
    "pdf_text_layer_failed": BlockerClass.NEEDS_HUMAN,
    "resume_upload_failed": BlockerClass.NEEDS_HUMAN,
    "ats_selector_failed": BlockerClass.NEEDS_HUMAN,
    # resume-tailoring gate (src/resume_tailor.py): the tailored resume never
    # cleared resume.min_score — a human should review/extend the baseline.
    "needs_resume_review": BlockerClass.NEEDS_HUMAN,
    # tailoring model/render hiccups are retryable
    "resume_tailor_error": BlockerClass.TRANSIENT,
    "resume_render_failed": BlockerClass.TRANSIENT,
    # config error: the only resolvable resume is the tests/ fixture — never retry
    "dummy_resume_blocked": BlockerClass.PERMANENT,
    # permanent — structurally cannot succeed
    "bad_ats_url": BlockerClass.PERMANENT,
    "unknown_source": BlockerClass.PERMANENT,
    "expired": BlockerClass.PERMANENT,  # posting is gone/closed — never retry
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


def classify(status: str | None) -> BlockerClass:
    """Map an apply-outcome status string to its control-flow class.

    Static _STATUS_TO_CLASS is authoritative. For statuses it doesn't
    cover, we consult the model-backed cache in
    :mod:`blocker_intelligence` — populated in the background from the
    per-source funnel history — before falling back to UNKNOWN.
    """
    if not status:
        return BlockerClass.UNKNOWN
    key = status.strip()
    static = _STATUS_TO_CLASS.get(key)
    if static is not None:
        return static
    # Fall back to the model-classified cache (sync read; never blocks).
    try:
        from src.blocker_intelligence import classified_status
        verdict = classified_status(key, [])
        if verdict:
            return BlockerClass(verdict)
    except Exception:
        pass
    return BlockerClass.UNKNOWN


def max_attempts(status: str | None, source: str = "") -> int:
    """Retry cap for the given status's class.

    When *source* is provided and the (source, status) pair has proven
    doomed in the persisted funnel (0 successes over ≥5 attempts), the
    cap is lowered adaptively — never raised above the static ceiling.
    """
    static = _MAX_ATTEMPTS[classify(status)]
    if not source or not status:
        return static
    try:
        from src.blocker_intelligence import adaptive_cap
        adjusted, _reason = adaptive_cap(source, status.strip(), static)
        return adjusted
    except Exception:
        return static


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

    - Any source missing its login credentials: not viable → "credentials_missing".
    - Human-fallback sources (usajobs) used to be unconditionally "needs_session_prep"
      here, which meant a scheduled run could never recover a USAJobs session on its
      own. ReauthManager now runs the automated login first and never blocks a
      non-interactive run waiting for a person, so they follow the same rule.
    """
    missing = [c for c in _REAUTH_CREDS.get(source, ()) if not os.environ.get(c)]
    if missing:
        return False, "credentials_missing"
    return True, ""


def should_attempt(
    last_status: str | None,
    attempt_count: int,
    source: str = "",
) -> tuple[bool, str]:
    """Decide whether to attempt a job given its last outcome and attempt count.

    Returns (attempt, skip_reason). skip_reason is empty when attempt is True.
    A job never attempted (no last_status) is always attempted.

    *source* enables the adaptive cap in :func:`max_attempts` — a
    (source, status) pair proven doomed in the persisted funnel history
    has its cap lowered so unwinnable jobs stop consuming attempts.
    """
    if not last_status:
        return True, ""  # never tried — always attempt

    cls = classify(last_status)
    if cls is BlockerClass.SUCCESS:
        return False, "already applied"

    cap = max_attempts(last_status, source=source)
    if cap == 0:
        return False, f"{cls.value} blocker — will not retry: {last_status}"
    if attempt_count >= cap:
        # Include an adaptive-cap note when the effective cap was lowered
        # below the static ceiling, so the log shows *why* the retry stopped.
        static = _MAX_ATTEMPTS[cls]
        suffix = f" [adaptive; static={static}]" if cap < static else ""
        return False, f"{cls.value} retry cap reached ({attempt_count}/{cap}){suffix}: {last_status}"
    return True, ""
