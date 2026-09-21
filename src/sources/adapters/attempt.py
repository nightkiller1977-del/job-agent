"""The application-attempt state machine (Phase 0.1 / 2a).

One explicit progression, so every apply attempt reports *where* it got to — and
so `applied` can mean exactly one thing: a receipt was verified.

    STARTED -> ENTRY_CTA_FOUND -> FORM_REACHED -> FIELDS_FILLED -> SUBMIT_AUTHORIZED
            -> SUBMIT_CLICKED -> RECEIPT_VERIFIED        (success)
                              \\-> ...                     -> FAILED / UNKNOWN

`SUBMIT_CLICKED` is deliberately distinct from `RECEIPT_VERIFIED`: a click with no
confirmation lands in `UNKNOWN`, never success. Wire values are strings so they
persist cleanly in SQLite/JSONL.

`ENTRY_CTA_FOUND` (ACES-399) is observational only: it marks that a supported
entry CTA (e.g. vendor_cta.py's "Apply now" click) was located before the form
itself was reached. It carries no new control-flow meaning — `rank()` treats it
the same as any other forward progress — and it is emitted best-effort alongside
the forensic evidence contract in forensics.py, never as a retry/circuit-breaker
signal (that authority stays with blocker_classifier.py).
"""
from __future__ import annotations

from enum import Enum


class AttemptPhase(str, Enum):
    STARTED = "started"
    ENTRY_CTA_FOUND = "entry_cta_found"
    FORM_REACHED = "form_reached"
    FIELDS_FILLED = "fields_filled"
    SUBMIT_AUTHORIZED = "submit_authorized"
    SUBMIT_CLICKED = "submit_clicked"
    RECEIPT_VERIFIED = "receipt_verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Ordered rank for "how far did we get" comparisons / metrics.
_ORDER = {
    AttemptPhase.STARTED: 0,
    AttemptPhase.ENTRY_CTA_FOUND: 1,
    AttemptPhase.FORM_REACHED: 2,
    AttemptPhase.FIELDS_FILLED: 3,
    AttemptPhase.SUBMIT_AUTHORIZED: 4,
    AttemptPhase.SUBMIT_CLICKED: 5,
    AttemptPhase.RECEIPT_VERIFIED: 6,
    AttemptPhase.FAILED: -1,
    AttemptPhase.UNKNOWN: -1,
}


def rank(phase: AttemptPhase) -> int:
    return _ORDER.get(phase, -1)
