"""The application-attempt state machine (Phase 0.1 / 2a).

One explicit progression, so every apply attempt reports *where* it got to — and
so `applied` can mean exactly one thing: a receipt was verified.

    STARTED -> FORM_REACHED -> FIELDS_FILLED -> SUBMIT_AUTHORIZED
            -> SUBMIT_CLICKED -> RECEIPT_VERIFIED        (success)
                              \\-> ...                     -> FAILED / UNKNOWN

`SUBMIT_CLICKED` is deliberately distinct from `RECEIPT_VERIFIED`: a click with no
confirmation lands in `UNKNOWN`, never success. Wire values are strings so they
persist cleanly in SQLite/JSONL.
"""
from __future__ import annotations

from enum import Enum


class AttemptPhase(str, Enum):
    STARTED = "started"
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
    AttemptPhase.FORM_REACHED: 1,
    AttemptPhase.FIELDS_FILLED: 2,
    AttemptPhase.SUBMIT_AUTHORIZED: 3,
    AttemptPhase.SUBMIT_CLICKED: 4,
    AttemptPhase.RECEIPT_VERIFIED: 5,
    AttemptPhase.FAILED: -1,
    AttemptPhase.UNKNOWN: -1,
}


def rank(phase: AttemptPhase) -> int:
    return _ORDER.get(phase, -1)
