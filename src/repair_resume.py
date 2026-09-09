"""Repair-completion resume — re-enter jobs into the apply loop once the AICC
Coordinator reports their repair operation as completed.

Flow per job stamped with a repair_operation_id (see session._maybe_report_incident):
  1. check_repair_status(operation_id) — coordinator status view.
  2. status == 'completed' → stamp repair_completed_at (+ repair_pr_url) into
     extra_json (merge_job_extra — never increments attempt counters).
  3. Reconcile the ledger projection (sync_confirmation_from_ledger). If the
     job lands in 'reconciliation_required', DO NOT auto-resubmit — submission
     truth is sacred; a human/receipt re-check must resolve the ambiguity first.
  4. Otherwise clear_session_block(job_id) so the job re-enters the apply pool.

Everything here is fail-open: an unreachable coordinator, a bad row, or a DB
hiccup skips the job and never breaks the apply run.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)


def resume_repaired_jobs(state=None, ledger=None, reporter=None) -> int:
    """Scan repair-bound jobs and unblock those whose repair has landed.

    state:    StateManager (default: a fresh instance on the real DB).
    ledger:   SubmissionLedger passed through to sync_confirmation_from_ledger
              (default None = the state manager builds the real one).
    reporter: object/module exposing check_repair_status (default:
              src.incident_reporter).
    Returns the number of jobs actually resumed (held-for-reconciliation jobs
    are stamped complete but NOT counted — they stay held).
    """
    if reporter is None:
        from . import incident_reporter as reporter
    if state is None:
        from .state_manager import StateManager
        state = StateManager()

    from .state_manager import parse_extra_json

    try:
        jobs = state.list_jobs_awaiting_repair()
    except Exception as exc:
        _log.warning("repair_resume.scan_failed error=%s", exc)
        return 0

    resumed = 0
    for job in jobs:
        job_id = job.get("job_id")
        try:
            extra = parse_extra_json(job.get("extra_json"))
            operation_id = extra.get("repair_operation_id")
            if not operation_id or extra.get("repair_completed_at"):
                continue
            op = reporter.check_repair_status(operation_id)
            if not op or op.get("status") != "completed":
                continue

            stamp = {"repair_completed_at": time.time()}
            if op.get("prUrl"):
                stamp["repair_pr_url"] = op.get("prUrl")
            state.merge_job_extra(job_id, stamp)

            # Reconcile submission truth before letting the job move again.
            confirmation = state.sync_confirmation_from_ledger(job_id, ledger=ledger)
            if confirmation == "reconciliation_required":
                # A prior submit is still ambiguous — never auto-resubmit.
                _log.info(
                    "repair_resume.held job_id=%s operation=%s reason=reconciliation_required",
                    job_id, operation_id,
                )
                continue

            state.clear_session_block(job_id)
            resumed += 1
            _log.info(
                "repair_resume.resumed job_id=%s operation=%s", job_id, operation_id
            )
        except Exception as exc:
            _log.warning("repair_resume.job_failed job_id=%s error=%s", job_id, exc)
    return resumed
