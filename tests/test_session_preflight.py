"""P3: proactive session/auth preflight decision."""
from src.blocker_classifier import needs_preflight_reauth


def test_auth_blocker_triggers_preflight():
    assert needs_preflight_reauth("workday_session_expired", "jobright", set()) is True
    assert needs_preflight_reauth("brassring_login_required", "linkedin", set()) is True


def test_non_auth_blocker_does_not_trigger():
    assert needs_preflight_reauth("submit_not_found", "jobright", set()) is False
    assert needs_preflight_reauth("external_ats_error", "jobright", set()) is False
    assert needs_preflight_reauth("applied", "jobright", set()) is False
    assert needs_preflight_reauth(None, "jobright", set()) is False  # never tried


def test_only_once_per_source_per_run():
    done = set()
    assert needs_preflight_reauth("workday_session_expired", "jobright", done) is True
    done.add("jobright")
    # second job from same source in the same run must not re-trigger
    assert needs_preflight_reauth("workday_session_expired", "jobright", done) is False
    # a different source still triggers
    assert needs_preflight_reauth("brassring_login_required", "linkedin", done) is True
