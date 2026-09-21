"""ACES-399 — read-only forensic classification of apply-attempt evidence.

Purely observational. This module consumes already-sanitized `forensic_phase`
evidence dicts (as written by forensics.emit_forensic_phase / read back via
events.read_run) for ONE attempt and proposes a likely failure category for a
human to review.

It is deliberately NOT the retry/circuit-breaker authority:
  - It does not import, call, or mutate anything in `blocker_classifier.py`.
  - Its output is never fed back into `should_attempt`, `max_attempts`,
    circuit-breaker state, or re-auth routing. Nothing in the orchestrator or
    adapter layer branches on the value this module returns.
  - It never falls back to raw text — evidence that is missing or
    contradictory always resolves to `unknown` with `capture_incomplete=True`,
    never a guess from DOM content (there isn't any DOM content here to guess
    from — only the bounded fields forensics.py already validated).

This is intentionally a small, pure function over plain dicts/lists so it is
trivial to unit test against fixtures without a browser or a live attempt.
"""
from __future__ import annotations

from .attempt import AttemptPhase, rank as _phase_rank
from .generic import detect_vendor

BROWSER_ENVIRONMENT_CANDIDATE = "browser_environment_candidate"
SESSION_AUTH_CANDIDATE = "session_auth_candidate"
URL_HANDOFF_CANDIDATE = "url_handoff_candidate"
NAVIGATION_ADAPTER_CANDIDATE = "navigation_adapter_candidate"
REQUIRED_FIELD_CANDIDATE = "required_field_candidate"
RECEIPT_RECONCILIATION_CANDIDATE = "receipt_reconciliation_candidate"
UNKNOWN = "unknown"

_CANDIDATES = frozenset({
    BROWSER_ENVIRONMENT_CANDIDATE, SESSION_AUTH_CANDIDATE, URL_HANDOFF_CANDIDATE,
    NAVIGATION_ADAPTER_CANDIDATE, REQUIRED_FIELD_CANDIDATE,
    RECEIPT_RECONCILIATION_CANDIDATE, UNKNOWN,
})

_FORM_EVIDENCE_PHASES = ("entry_cta_found", "form_reached")
_FORM_REACHED_RANK = _phase_rank(AttemptPhase.FORM_REACHED)


def _unknown(events: list[dict], reason: str) -> dict:
    return {"candidate": UNKNOWN, "capture_incomplete": True, "reason": reason}


def _rank_of(phase_value: str) -> int:
    try:
        return _phase_rank(AttemptPhase(phase_value))
    except Exception:
        return -1


def classify_forensic_evidence(events: list[dict]) -> dict:
    """Classify one attempt's forensic_phase evidence into a single candidate.

    `events` is a list of plain dicts — typically the `forensic_phase` records
    for one attempt_id, e.g. filtered from `events.read_run(run_id)`. Each
    dict is expected to already be allowlisted/sanitized (forensics.py does
    that at write time); this function additionally never trusts a field it
    doesn't recognize — it only reads the specific keys it needs and ignores
    everything else, so passing an un-sanitized dict cannot inject behavior.

    Returns {"candidate": <one of the constants above>, "capture_incomplete": bool,
    "reason": <short internal note, optional>}. Combination-based: no single
    field decides the outcome on its own, matching the ACES-399 rules.
    """
    if not events:
        return _unknown(events, "no_evidence")

    any_incomplete = any(bool(e.get("capture_incomplete")) for e in events if isinstance(e, dict))
    phases_seen = {e.get("phase") for e in events if isinstance(e, dict) and e.get("phase")}

    # 1. bot/browser-environment: a captcha/challenge wall was observed anywhere.
    if any(e.get("auth_state") == "bot_challenge" for e in events if isinstance(e, dict)):
        return {"candidate": BROWSER_ENVIRONMENT_CANDIDATE, "capture_incomplete": any_incomplete}

    # 2. session/auth: redirected to a sign-in wall, OR a bare login/password
    #    prompt with no matching "sign in"-ish text nearby (forensics.py still
    #    reports these as "logged_out" — a password field is a login wall
    #    either way; see probe_page_evidence).
    if any(e.get("auth_state") in ("redirected_to_signin", "logged_out")
           for e in events if isinstance(e, dict)):
        return {"candidate": SESSION_AUTH_CANDIDATE, "capture_incomplete": any_incomplete}

    # 3. URL handoff: the host we actually landed on doesn't match the vendor
    #    this attempt was routed to. Reuses the existing detect_vendor() —
    #    no new URL parser.
    for e in events:
        if not isinstance(e, dict):
            continue
        host = e.get("host") or ""
        vendor = e.get("vendor") or ""
        if not host or not vendor or vendor in ("generic", "unknown"):
            continue
        detected = detect_vendor(host)
        if detected != "generic" and detected != vendor:
            return {"candidate": URL_HANDOFF_CANDIDATE, "capture_incomplete": any_incomplete}

    # 4. required fields: validation errors were observed on the form.
    if any(e.get("validation_errors_present") is True for e in events if isinstance(e, dict)):
        return {"candidate": REQUIRED_FIELD_CANDIDATE, "capture_incomplete": any_incomplete}

    # 5. navigation/adapter: we reached the form-ish phase(s) but never
    #    actually found a form, and it wasn't an auth/bot wall (already ruled
    #    out above). Guarded on the FURTHEST phase actually observed: an early
    #    form_present=False reading (e.g. the FORM_REACHED probe fires right
    #    after navigation, before a CTA-vendor's entry click) must not shadow
    #    later evidence that a form/submit WAS eventually reached.
    form_events = [
        e for e in events
        if isinstance(e, dict) and e.get("phase") in _FORM_EVIDENCE_PHASES
        and "form_present" in e
    ]
    furthest_rank = max((_rank_of(p) for p in phases_seen), default=-1)
    if (form_events and all(e.get("form_present") is False for e in form_events)
            and furthest_rank <= _FORM_REACHED_RANK):
        return {"candidate": NAVIGATION_ADAPTER_CANDIDATE, "capture_incomplete": any_incomplete}

    # 6. receipt reconciliation: submit was clicked but no receipt followed.
    if "submit_clicked" in phases_seen and "receipt_verified" not in phases_seen:
        return {"candidate": RECEIPT_RECONCILIATION_CANDIDATE, "capture_incomplete": any_incomplete}

    return _unknown(events, "no_matching_rule")
