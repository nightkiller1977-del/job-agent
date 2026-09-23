"""P3: proactive session/auth preflight decision + reauth-viability guard."""
from src.blocker_classifier import (
    needs_preflight_reauth,
    preflight_reauth_viable,
    classify,
    BlockerClass,
)


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


# --- P3 reauth-viability guard (avoid doomed reauths mid-apply) ---

def test_human_fallback_source_is_viable_with_creds(monkeypatch):
    """ACES-283: usajobs is automated-first now (stored creds + TOTP / emailed code,
    human only as fallback, never blocking a non-interactive run), so a proactive
    mid-apply reauth is worth attempting whenever its credentials exist. It used to
    be unconditionally 'needs_session_prep', which meant a scheduled run could never
    recover a USAJobs session on its own."""
    monkeypatch.setenv("USAJOBS_EMAIL", "user@example.com")
    monkeypatch.setenv("USAJOBS_PASSWORD", "secret")
    assert preflight_reauth_viable("usajobs") == (True, "")


def test_human_fallback_source_without_creds_not_viable(monkeypatch):
    monkeypatch.delenv("USAJOBS_EMAIL", raising=False)
    monkeypatch.delenv("USAJOBS_PASSWORD", raising=False)
    assert preflight_reauth_viable("usajobs") == (False, "credentials_missing")


def test_automated_source_missing_creds_not_viable(monkeypatch):
    monkeypatch.delenv("JOBRIGHT_EMAIL", raising=False)
    monkeypatch.delenv("JOBRIGHT_PASSWORD", raising=False)
    viable, why = preflight_reauth_viable("jobright")
    assert viable is False and why == "credentials_missing"


def test_automated_source_with_creds_is_viable(monkeypatch):
    monkeypatch.setenv("JOBRIGHT_EMAIL", "user@example.com")
    monkeypatch.setenv("JOBRIGHT_PASSWORD", "secret")
    viable, why = preflight_reauth_viable("jobright")
    assert viable is True and why == ""


def test_new_statuses_are_classified():
    # needs_session_prep routes like an auth blocker; credentials_missing must be
    # permanent so the circuit breaker never blind-retries a config gap.
    assert classify("needs_session_prep") is BlockerClass.AUTH_REQUIRED
    assert classify("credentials_missing") is BlockerClass.PERMANENT
    # historic rows from before USAJobsScraper.apply() raised AuthFailedError
    assert classify("usajobs_login_required") is BlockerClass.AUTH_REQUIRED


def test_source_absent_from_reauth_creds_is_never_viable(monkeypatch):
    """A source with no _REAUTH_CREDS entry at all — like builtin — must not be
    confused with one whose (zero) required creds are trivially satisfied.

    Before this fix, _REAUTH_CREDS.get(source, ()) returned "()" for both
    cases, so the missing-creds comprehension was empty either way and this
    reported viable by accident. In production that meant a
    builtin_login_required job (AUTH_REQUIRED, ACES-437) triggered a P3
    preflight reauth for "builtin", ReauthManager.handle("builtin") returned
    False (builtin is in neither AUTOMATED_SOURCES nor HUMAN_SOURCES), and the
    orchestrator recorded reauth_failed — discarding the accurate diagnosis and
    burning the AUTH_REQUIRED retry budget on a reauth that could never
    succeed (Copilot + Codex review findings on PR #151).
    """
    assert "builtin" not in __import__("src.blocker_classifier", fromlist=["_REAUTH_CREDS"])._REAUTH_CREDS
    assert preflight_reauth_viable("builtin") == (False, "credentials_missing")


def test_unrelated_env_vars_cannot_make_an_unsupported_source_viable(monkeypatch):
    """Setting env vars that happen to share a name with another source's
    credentials must not accidentally satisfy an unsupported source's (empty)
    requirement list."""
    monkeypatch.setenv("JOBRIGHT_EMAIL", "user@example.com")
    monkeypatch.setenv("JOBRIGHT_PASSWORD", "secret")
    assert preflight_reauth_viable("builtin")[0] is False
