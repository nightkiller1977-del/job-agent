"""Exhaustive guard against status-vocabulary drift.

The circuit breaker only works if every status the code actually emits maps to a
real BlockerClass (not UNKNOWN). Rather than hardcode a list, this walks the
source tree and collects:

  1. Every `ApplyOutcomeCode.<MEMBER>` referenced anywhere.
  2. Every string literal passed to `record_apply_attempt(...)` /
     `_set_apply_outcome(...)` (positional status arg or `status=`/`readiness=`).
  3. The hyphenated readiness strings the orchestrator emits.

and asserts none classify as UNKNOWN (except the intentional UNKNOWN sentinel).
"""
import ast
import os
from pathlib import Path

from src.apply_outcome import ApplyOutcomeCode
from src.blocker_classifier import classify, BlockerClass

# Function name → positional index of the *status* argument.
#   record_apply_attempt(job_id, status, ...)  → index 1
#   _set_apply_outcome(status, detail, ...)     → index 0
_STATUS_ARG_INDEX = {"record_apply_attempt": 1, "_set_apply_outcome": 0}
# Statuses that are *meant* to be the cautious/unknown sentinel.
_ALLOWED_UNKNOWN = {"unknown", "started"}


def _collect() -> tuple[set[str], set[str]]:
    src_dir = Path(__file__).parent.parent / "src"
    enum_members: set[str] = set()
    literal_statuses: set[str] = set()

    for root, _, files in os.walk(src_dir):
        for file in files:
            if not file.endswith(".py"):
                continue
            try:
                tree = ast.parse((Path(root) / file).read_text())
            except Exception:
                continue
            for node in ast.walk(tree):
                # ApplyOutcomeCode.MEMBER
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ApplyOutcomeCode"
                ):
                    enum_members.add(node.attr)
                # status-literal call args
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                    idx = _STATUS_ARG_INDEX.get(name)
                    if idx is None:
                        continue  # only inspect apply-status emitters
                    if len(node.args) > idx:
                        arg = node.args[idx]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            literal_statuses.add(arg.value)
                    for kw in node.keywords:
                        if kw.arg in ("status", "readiness") and isinstance(kw.value, ast.Constant):
                            if isinstance(kw.value.value, str):
                                literal_statuses.add(kw.value.value)
    return enum_members, literal_statuses


def test_all_enum_members_classify():
    """Every ApplyOutcomeCode member (except the UNKNOWN sentinel) must map to a real class."""
    unknowns = [
        m.name for m in ApplyOutcomeCode
        if m.value not in _ALLOWED_UNKNOWN and classify(m) == BlockerClass.UNKNOWN
    ]
    assert not unknowns, f"Enum members mapping to UNKNOWN: {unknowns}"


def test_referenced_enum_members_exist_and_classify():
    enum_members, _ = _collect()
    missing = [m for m in enum_members if not hasattr(ApplyOutcomeCode, m)]
    assert not missing, f"Code references non-existent ApplyOutcomeCode members: {missing}"
    unknowns = [
        m for m in enum_members
        if getattr(ApplyOutcomeCode, m).value not in _ALLOWED_UNKNOWN
        and classify(getattr(ApplyOutcomeCode, m)) == BlockerClass.UNKNOWN
    ]
    assert not unknowns, f"Referenced enum members classify UNKNOWN: {unknowns}"


def test_literal_and_readiness_statuses_classify():
    _, literals = _collect()
    # hyphenated readiness vocabulary emitted by the orchestrator's classifier
    readiness = {
        "needs-session", "needs-hydration", "needs-answer",
        "needs-review", "needs-portal-login",
    }
    # representative vendor-prefixed families that scrapers may emit as strings
    vendor = {
        "ashby_submit_not_found", "workday_form_not_reached",
        "microsoft_login_required", "linkedin_step_blocked",
    }
    candidates = (literals | readiness | vendor)
    unknowns = [s for s in candidates if s not in _ALLOWED_UNKNOWN and classify(s) == BlockerClass.UNKNOWN]
    assert not unknowns, f"Emitted statuses mapping to UNKNOWN: {unknowns}"
