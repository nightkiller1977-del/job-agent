"""ACES-399 — the read-only forensic classifier.

Proves two things per the handoff: (1) the classification rules map bounded
evidence to the right candidate label, and (2) the classifier is genuinely
observational — it never imports/calls/mutates blocker_classifier.py, and
running it alongside blocker_classifier changes nothing about retry/circuit-
breaker decisions.
"""
from __future__ import annotations

import ast
from pathlib import Path

import src.blocker_classifier as blocker_classifier
from src.sources.adapters import forensic_classifier as fc


def _evt(phase, **extra):
    return {"phase": phase, **extra}


# --------------------------------------------------------------------------- #
# classification rules
# --------------------------------------------------------------------------- #

def test_bot_challenge_maps_to_browser_environment_candidate():
    events = [_evt("form_reached", auth_state="bot_challenge", host="x.com", vendor="generic")]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.BROWSER_ENVIRONMENT_CANDIDATE


def test_redirected_to_signin_maps_to_session_auth_candidate():
    events = [_evt("form_reached", auth_state="redirected_to_signin")]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.SESSION_AUTH_CANDIDATE


def test_logged_out_password_wall_maps_to_session_auth_candidate():
    # Copilot review (PR #138): probe_page_evidence() reports "logged_out" for
    # a password field with no matching "sign in"-ish text nearby (e.g. an SSO
    # landing page) — that is still a login wall and must not fall through to
    # navigation_adapter_candidate / unknown.
    events = [_evt("form_reached", auth_state="logged_out", form_present=False)]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.SESSION_AUTH_CANDIDATE


def test_host_vendor_mismatch_maps_to_url_handoff_candidate():
    # Attempt was routed to "greenhouse" but the page we actually landed on
    # resolves (via the existing detect_vendor()) to lever.
    events = [_evt("form_reached", host="jobs.lever.co", vendor="greenhouse",
                   auth_state="unknown", form_present=True)]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.URL_HANDOFF_CANDIDATE


def test_matching_host_and_vendor_is_not_a_handoff_mismatch():
    events = [_evt("form_reached", host="boards.greenhouse.io", vendor="greenhouse",
                   auth_state="logged_in", form_present=True, submit_control_present=True)]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] != fc.URL_HANDOFF_CANDIDATE


def test_form_absent_with_no_auth_or_bot_signal_is_navigation_adapter_candidate():
    events = [_evt("form_reached", form_present=False, auth_state="unknown",
                   host="acme.example.com", vendor="generic")]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.NAVIGATION_ADAPTER_CANDIDATE


def test_early_form_absent_reading_does_not_shadow_later_progress():
    """A CTA-vendor's FORM_REACHED probe fires right after navigation, before
    the entry CTA is even clicked — form_present=False there is normal and
    must NOT be read as "no form was ever reached" once later evidence (a
    submit_clicked phase) proves otherwise."""
    events = [
        _evt("form_reached", form_present=False, auth_state="unknown",
             host="careers.microsoft.com", vendor="microsoft"),
        _evt("entry_cta_found", host="careers.microsoft.com", vendor="microsoft"),
        _evt("submit_clicked", submit_control_present=True),
    ]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] != fc.NAVIGATION_ADAPTER_CANDIDATE
    assert out["candidate"] == fc.RECEIPT_RECONCILIATION_CANDIDATE


def test_cta_found_but_form_never_confirmed_is_still_navigation_adapter_candidate():
    events = [
        _evt("form_reached", form_present=False, auth_state="unknown"),
        _evt("entry_cta_found"),
    ]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.NAVIGATION_ADAPTER_CANDIDATE


def test_validation_errors_present_is_required_field_candidate():
    events = [
        _evt("form_reached", form_present=True, auth_state="logged_in"),
        _evt("submit_clicked", validation_errors_present=True),
    ]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.REQUIRED_FIELD_CANDIDATE


def test_submit_clicked_without_receipt_is_receipt_reconciliation_candidate():
    events = [
        _evt("form_reached", form_present=True, auth_state="logged_in"),
        _evt("submit_clicked", submit_control_present=True),
    ]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.RECEIPT_RECONCILIATION_CANDIDATE


def test_submit_clicked_and_receipt_verified_is_not_reconciliation_candidate():
    events = [
        _evt("form_reached", form_present=True, auth_state="logged_in"),
        _evt("submit_clicked", submit_control_present=True),
        _evt("receipt_verified"),
    ]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] != fc.RECEIPT_RECONCILIATION_CANDIDATE


def test_no_evidence_is_unknown_and_marks_incomplete():
    out = fc.classify_forensic_evidence([])
    assert out["candidate"] == fc.UNKNOWN
    assert out["capture_incomplete"] is True


def test_contradictory_evidence_falls_back_to_unknown_not_a_guess():
    # form_present True (a real form WAS seen) but also flagged incomplete —
    # none of the specific rules fire, and there's no raw-text fallback to
    # reach for; it must land on UNKNOWN, not fabricate a candidate.
    events = [_evt("form_reached", form_present=True, auth_state="logged_in",
                   capture_incomplete=True)]
    out = fc.classify_forensic_evidence(events)
    assert out["candidate"] == fc.UNKNOWN
    assert out["capture_incomplete"] is True


def test_candidate_is_always_one_of_the_documented_seven():
    fixtures = [
        [],
        [_evt("form_reached", auth_state="bot_challenge")],
        [_evt("form_reached", auth_state="redirected_to_signin")],
        [_evt("form_reached", host="jobs.lever.co", vendor="workday")],
        [_evt("form_reached", form_present=False, auth_state="unknown")],
        [_evt("submit_clicked", validation_errors_present=True)],
        [_evt("submit_clicked")],
        [_evt("form_reached", form_present=True, auth_state="logged_in")],
    ]
    for events in fixtures:
        out = fc.classify_forensic_evidence(events)
        assert out["candidate"] in fc._CANDIDATES


# --------------------------------------------------------------------------- #
# purely observational: never touches blocker_classifier.py
# --------------------------------------------------------------------------- #

def test_module_does_not_import_blocker_classifier():
    src = Path(fc.__file__).read_text()
    tree = ast.parse(src)
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            referenced.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                referenced.add(node.module)
    assert not any("blocker_classifier" in name for name in referenced), (
        f"forensic_classifier.py must never import blocker_classifier.py, found: {referenced}"
    )


def test_module_has_no_attribute_referencing_blocker_classifier():
    # Defense in depth beyond the static import check: nothing at module scope
    # holds a reference to the blocker_classifier module or its functions.
    for name, value in vars(fc).items():
        assert value is not blocker_classifier
        assert getattr(value, "__module__", "") != blocker_classifier.__name__


def test_circuit_breaker_decision_is_unchanged_whether_or_not_forensic_classifier_ran():
    last_status = "submit_not_found"
    attempt_count = 1
    source = "jobright"

    decision_before = blocker_classifier.should_attempt(last_status, attempt_count, source=source)

    # Run the forensic classifier "alongside" the retry decision, using
    # evidence derived from the SAME attempt.
    events = [
        _evt("form_reached", form_present=True, auth_state="logged_in"),
        _evt("submit_clicked", validation_errors_present=False),
    ]
    verdict = fc.classify_forensic_evidence(events)
    assert verdict["candidate"] in fc._CANDIDATES  # it ran and produced something

    decision_after = blocker_classifier.should_attempt(last_status, attempt_count, source=source)
    assert decision_before == decision_after


def test_classify_call_is_side_effect_free_on_blocker_classifier_state():
    before_status_map = dict(blocker_classifier._STATUS_TO_CLASS)
    before_caps = dict(blocker_classifier._MAX_ATTEMPTS)

    fc.classify_forensic_evidence([
        _evt("form_reached", auth_state="bot_challenge"),
        _evt("submit_clicked"),
    ])

    assert blocker_classifier._STATUS_TO_CLASS == before_status_map
    assert blocker_classifier._MAX_ATTEMPTS == before_caps
