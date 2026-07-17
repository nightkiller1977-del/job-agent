from enum import Enum

class ApplyOutcomeCode(str, Enum):
    APPLIED = "applied"
    
    # Needs Human / Structural
    SUBMIT_NOT_FOUND = "submit_not_found"
    FORM_NOT_REACHED = "form_not_reached"
    FORM_NOT_DETECTED = "form_not_detected"
    REQUIRED_FIELD_UNANSWERED = "required_field_unanswered"
    FORM_EMPTY_NOT_SUBMITTED = "form_empty_not_submitted"
    SUBMISSION_CANCELLED = "submission_cancelled"
    SUBMISSION_UNVERIFIED = "submission_unverified"
    STEP_BLOCKED = "step_blocked"
    STUCK_ON_REQUIRED_FIELD = "stuck_on_required_field"
    EXTERNAL_APPLY_NOT_FOUND = "external_apply_not_found"
    ATS_FAILURE = "ats_failure"
    KEYWORD_COVERAGE_FAILED = "keyword_coverage_failed"
    PDF_TEXT_LAYER_FAILED = "pdf_text_layer_failed"
    RESUME_UPLOAD_FAILED = "resume_upload_failed"
    ATS_SELECTOR_FAILED = "ats_selector_failed"
    NEEDS_ANSWER = "needs_answer"
    NEEDS_HYDRATION = "needs_hydration"

    # Auth
    LOGIN_REQUIRED = "login_required"
    SESSION_EXPIRED = "session_expired"
    REAUTH_FAILED = "reauth_failed"
    NEEDS_SESSION = "needs_session"
    NEEDS_SESSION_PREP = "needs_session_prep"
    HUMAN_ACTION_REQUIRED = "human_action_required"

    # Transient
    EXTERNAL_ATS_ERROR = "external_ats_error"
    BROWSER_TIMEOUT = "browser_timeout"
    MODEL_TIMEOUT = "model_timeout"
    UNKNOWN_EXTERNAL_ATS_ERROR = "unknown_external_ats_error"
    ERROR = "error"
    REAUTH_RETRY_ERROR = "reauth_retry_error"

    # Permanent
    CREDENTIALS_MISSING = "credentials_missing"
    BAD_ATS_URL = "bad_ats_url"
    UNKNOWN_SOURCE = "unknown_source"
    MISSING_ATS_URL = "missing_ats_url"
    INDEED_EASY_APPLY_OR_NO_ATS = "indeed_easy_apply_or_no_ats"

    # Fallback
    UNKNOWN = "unknown"
