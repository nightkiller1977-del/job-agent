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
from datetime import datetime
from enum import Enum
from typing import NamedTuple

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
    "submit_click_failed": BlockerClass.NEEDS_HUMAN,  # control located but click did not land
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
    # Fall back to the model-classified cache (sync read; never blocks). The
    # background classifier stored its verdict against the reason samples IT
    # observed, so this lookup must not require the reasons to match.
    try:
        from src.blocker_intelligence import latest_classification
        verdict = latest_classification(key)
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


# ─── Circuit re-arm ────────────────────────────────────────────────────────
# should_attempt() above is a one-way door: apply_attempt_count only ever
# increments (state_manager.record_apply_attempt), so once a job reaches its
# cap the circuit stays open forever. That is correct while nothing changes —
# but two things do change, and neither is visible to the attempt counter:
#
#   1. Our code. NEEDS_HUMAN literally means "retrying without a code fix
#      won't help". When the fix ships, every job capped on that blocker is
#      still locked out, so the fix cannot be measured. ACES-428 (iframe
#      submit detection) landed against 7 jobs already capped at the
#      NEEDS_HUMAN ceiling of 1 — none of them could exercise it.
#   2. The environment. A gateway 502 or a bad-network night burns the cap on
#      jobs that would succeed on a healthy run.
#
# So the circuit re-arms on exactly those two signals, and on nothing else.
# PERMANENT and SUCCESS never re-arm: a closed posting, a bad URL and an
# already-submitted application do not become winnable because we edited code.

# Modules that implement the apply/submit path. A change to any of them can
# plausibly turn a previous blocker into a submission; changes to scoring,
# discovery, notifications or docs cannot, and must not re-arm anything.
_APPLY_PATH_GLOBS = ("sources/**/*.py",)
_APPLY_PATH_FILES = ("orchestrator.py", "resume_tailor.py")

# Environment flags that re-route the apply path without changing a byte of it.
# Flipping one is as material as an edit — ACES-284 calls out USE_ADAPTER_REGISTRY
# specifically, since the registry route carries the fixes that make retries
# succeed — so they are part of the build identity.
_APPLY_PATH_ENV_FLAGS = ("USE_ADAPTER_REGISTRY",)

# Classes whose blocker a code change can plausibly resolve.
_CODE_REARM_CLASSES = frozenset({
    BlockerClass.TRANSIENT,
    BlockerClass.AUTH_REQUIRED,
    BlockerClass.NEEDS_HUMAN,
    BlockerClass.UNKNOWN,
})

# Classes the cooldown can re-arm. Deliberately the same set as the code-change
# path, NEEDS_HUMAN included. Waiting does not itself teach the agent to find a
# submit button, so on its own this would be a weak signal — but the measured
# failure mode (ACES-284) was 48 approved jobs aging out to `expired` having
# never been retried after one breaker trip. A small, bounded number of retries
# across the posting's life is cheap insurance against a blocker that was really
# a bad night, a half-loaded page, or a fix that shipped outside the
# fingerprinted path. _MAX_COOLDOWN_REARMS is what keeps it from becoming the
# unbounded retry loop this module was built to stop.
_COOLDOWN_REARM_CLASSES = _CODE_REARM_CLASSES

_DEFAULT_COOLDOWN_HOURS = 24

# Cooldown re-arm is bounded; code-change re-arm is not. A code change is its own
# evidence that something material happened, and each one grants exactly one
# retry (the next attempt stamps the new fingerprint). The clock carries no such
# evidence — left unbounded it would hand every doomed job a free attempt every
# day forever, which is precisely the 245-wasted-retry behaviour this module
# exists to stop. After this many cooldown re-arms without a different outcome,
# the blocker is not environmental and the circuit stays open.
_MAX_COOLDOWN_REARMS = 3

class Rearm(NamedTuple):
    """Why a circuit re-armed. *kind* drives policy (only COOLDOWN is budgeted);
    *reason* is the operator-facing explanation. Kept separate so no caller has
    to parse the message to decide what happened."""

    kind: str
    reason: str

    COOLDOWN = "cooldown"
    CODE_CHANGE = "code_change"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.reason


_fingerprint_cache: str | None = None


def _cooldown_hours() -> int:
    """Hours before an environmental blocker is retryable. 0 disables cooldown
    re-arm entirely (code-change re-arm is unaffected)."""
    raw = os.environ.get("APPLY_CIRCUIT_COOLDOWN_HOURS", "")
    try:
        return max(0, int(raw)) if raw.strip() else _DEFAULT_COOLDOWN_HOURS
    except ValueError:
        return _DEFAULT_COOLDOWN_HOURS


def apply_path_fingerprint() -> str:
    """Short digest of the apply-path source. Changes exactly when code that
    could alter an apply outcome changes.

    Computed once per process: the files cannot change under a running apply
    loop, and every job in the run must be judged against the same build.
    Unreadable/missing files degrade to an empty digest rather than raising —
    a fingerprint we cannot compute must not break the apply run; it only
    costs us the code-change re-arm for that run.
    """
    global _fingerprint_cache
    if _fingerprint_cache is not None:
        return _fingerprint_cache

    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parent
    paths: set[Path] = set()
    try:
        for pattern in _APPLY_PATH_GLOBS:
            paths.update(p for p in root.glob(pattern) if p.is_file())
        for name in _APPLY_PATH_FILES:
            p = root / name
            if p.is_file():
                paths.add(p)
    except OSError:
        _fingerprint_cache = ""
        return _fingerprint_cache

    digest = hashlib.sha256()
    for flag in _APPLY_PATH_ENV_FLAGS:
        digest.update(f"{flag}={os.environ.get(flag, '')}".encode())
    try:
        for path in sorted(paths):
            if "__pycache__" in path.parts:
                continue
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    except OSError:
        _fingerprint_cache = ""
        return _fingerprint_cache

    _fingerprint_cache = digest.hexdigest()[:16]
    return _fingerprint_cache


def reset_fingerprint_cache() -> None:
    """Drop the memoized fingerprint. The apply-path files cannot change under a
    running apply loop, but the env flags folded into the digest can — and tests
    need to vary both."""
    global _fingerprint_cache
    _fingerprint_cache = None


def rearm_reason(
    extra: dict,
    *,
    current_fingerprint: str | None = None,
    now: datetime | None = None,
) -> Rearm | None:
    """Why this job's open circuit should re-arm now, or None to leave it open.

    *extra* is the job's parsed extra_json. Pure and side-effect free — the
    caller decides what to do with the verdict, so the policy stays testable
    without a database.
    """
    last_status = extra.get("apply_last_status")
    if not last_status:
        return None  # never attempted — no circuit to re-arm
    if not int(extra.get("apply_attempt_count", 0) or 0):
        return None  # budget already available

    cls = classify(last_status)
    if cls is BlockerClass.SUCCESS or cls is BlockerClass.PERMANENT:
        return None

    # 1. Code-change re-arm.
    if cls in _CODE_REARM_CLASSES:
        current = current_fingerprint if current_fingerprint is not None else apply_path_fingerprint()
        recorded = extra.get("apply_code_fingerprint")
        # An absent fingerprint means the attempt predates this field. Treat it
        # as unknown rather than as a change: re-arming every legacy row on the
        # first run after deploy would stampede the whole backlog through a
        # code path we have no evidence about. The cooldown below still applies,
        # and `rearm-breakers` exists for a deliberate one-off sweep.
        if current and recorded and recorded != current:
            return Rearm(
                Rearm.CODE_CHANGE,
                f"apply-path code changed since last attempt ({recorded} → {current})",
            )

    # 2. Cooldown re-arm.
    if cls in _COOLDOWN_REARM_CLASSES:
        hours = _cooldown_hours()
        last_at = extra.get("apply_last_attempt")
        spent = int(extra.get("apply_cooldown_rearm_count", 0) or 0)
        if spent >= _MAX_COOLDOWN_REARMS:
            return None  # repeatedly retried across days — not an environmental blip
        if hours and last_at:
            try:
                elapsed = (now or datetime.utcnow()) - datetime.fromisoformat(str(last_at))
            except (TypeError, ValueError):
                return None
            if elapsed.total_seconds() >= hours * 3600:
                aged = int(elapsed.total_seconds() // 3600)
                return Rearm(
                    Rearm.COOLDOWN,
                    f"{cls.value} blocker idle {aged}h "
                    f"(cooldown {hours}h, {spent + 1}/{_MAX_COOLDOWN_REARMS})",
                )

    return None
