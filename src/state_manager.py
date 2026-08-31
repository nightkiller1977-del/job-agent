"""
State manager — SQLite backend for tracking all discovered/applied/skipped jobs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    company     TEXT,
    location    TEXT,
    salary_raw  TEXT,
    remote_type TEXT,
    url         TEXT,
    description TEXT,
    score       INTEGER,
    score_reason TEXT,
    flags       TEXT,
    status      TEXT NOT NULL DEFAULT 'discovered',
    discovered_at TEXT NOT NULL,
    reviewed_at   TEXT,
    applied_at    TEXT,
    confirmation_status TEXT,
    extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS archived_jobs (
    job_id         TEXT PRIMARY KEY,
    source         TEXT,
    title          TEXT,
    company        TEXT,
    location       TEXT,
    url            TEXT,
    score          INTEGER,
    status         TEXT,
    discovered_at  TEXT,
    archived_at    TEXT NOT NULL,
    archive_reason TEXT,
    confirmation_status TEXT,
    extra_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_discovered_at ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_archived_at ON archived_jobs(archived_at);
"""


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal confirmation_status transition is attempted."""


VALID_CONFIRMATION_TRANSITIONS = {
    None: {"submitting"},
    "submitting": {"submitted", "submission_unverified"},
    "submitted": {"receipt_pending", "confirmed_by_employer"},
    "receipt_pending": {"confirmed_by_employer"},
    "submission_unverified": {"reconciliation_required"},
    "reconciliation_required": {"submitting"},
    "confirmed_by_employer": set(),
}


class StateManager:
    def __init__(self, db_path: str = "state/jobs.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Persistent shared connection — avoids per-operation open/close overhead
        # on large batch_score runs (50 upserts × connection cycle = significant latency).
        # check_same_thread=False is safe here: all callers are async but SQLite WAL
        # mode handles concurrent readers and a single writer correctly.
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Return the shared connection. Kept for backwards compatibility."""
        return self._conn

    def close(self) -> None:
        """Explicitly close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        self._conn.executescript(DB_SCHEMA)
        # Add confirmation_status column dynamically if migrating existing DB
        try:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN confirmation_status TEXT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE archived_jobs ADD COLUMN confirmation_status TEXT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_confirmation_status ON jobs(confirmation_status)")
        except sqlite3.OperationalError:
            pass
        # Hard-delete any legacy expired rows (pre-archive schema)
        self._conn.execute("DELETE FROM jobs WHERE status = 'expired'")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def upsert_job(self, job: dict) -> bool:
        """
        Insert or update a job record.
        Returns True if this is a newly discovered job, False if it already existed.
        """
        now = datetime.utcnow().isoformat()
        extra = {k: v for k, v in job.items() if k not in {
            "job_id", "source", "title", "company", "location",
            "salary_raw", "remote_type", "url", "description",
            "score", "score_reason", "flags", "status",
            "discovered_at", "reviewed_at", "applied_at",
        }}
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT job_id FROM jobs WHERE job_id = ?", (job["job_id"],)
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, source, title, company, location, salary_raw,
                     remote_type, url, description, score, score_reason,
                     flags, status, discovered_at, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"],
                    job.get("source", ""),
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("location", ""),
                    job.get("salary_raw", ""),
                    job.get("remote_type", ""),
                    job.get("url", ""),
                    job.get("description", ""),
                    job.get("score"),
                    job.get("score_reason", ""),
                    job.get("flags", ""),
                    job.get("status", "discovered"),
                    job.get("discovered_at", now),
                    json.dumps(extra) if extra else None,
                ),
            )
            return True

    def set_status(self, job_id: str, status: str) -> None:
        _log.info("job.status job_id=%s status=%s", job_id, status)
        if status == "expired":
            self.archive_job(job_id, reason="expired")
            return

        now = datetime.utcnow().isoformat()
        ts_field = {
            "reviewed": "reviewed_at",
            "applied": "applied_at",
            "skipped": "reviewed_at",
            "bookmarked": "reviewed_at",
        }.get(status)
        if ts_field:
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE jobs SET status = ?, {ts_field} = ? WHERE job_id = ?",
                    (status, now, job_id),
                )
        else:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (status, job_id),
                )

    def transition_confirmation(self, job_id: str, to_status: str) -> None:
        """Transitions confirmation_status following the formal state transition table."""
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"Job {job_id} not found")

        current_status = job.get("confirmation_status")
        allowed = VALID_CONFIRMATION_TRANSITIONS.get(current_status, set())
        if to_status not in allowed:
            raise InvalidStateTransitionError(
                f"Cannot transition confirmation_status from '{current_status}' to '{to_status}'. Allowed: {allowed}"
            )

        if to_status == "confirmed_by_employer" and job.get("status") != "applied":
            raise InvalidStateTransitionError(
                f"Cannot set confirmation_status='confirmed_by_employer' when job status is '{job.get('status')}' (must be 'applied')"
            )

        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET confirmation_status = ? WHERE job_id = ?",
                (to_status, job_id),
            )
            conn.commit()

    def sync_confirmation_from_ledger(self, job_id: str, ledger: Any = None) -> Optional[str]:
        """Projects SubmissionLedger attempt state onto the job's confirmation_status lifecycle.

        Handles:
          - receipt_verified -> 'submitted'
          - submit_in_progress (live) -> 'submitting'
          - submit_in_progress (stale) -> 'reconciliation_required'
          - submission_unverified -> 'submission_unverified'
        """
        job = self.get_job(job_id)
        if not job:
            return None

        if ledger is None:
            try:
                from .sources.adapters.idempotency import SubmissionLedger
                ledger = SubmissionLedger()
            except Exception:
                return job.get("confirmation_status")

        try:
            from .sources.adapters.idempotency import canonical_key, PHASE_IN_PROGRESS, PHASE_VERIFIED, PHASE_UNVERIFIED
            key = canonical_key(job)
            if not key:
                return job.get("confirmation_status")

            record = ledger.record(key) if hasattr(ledger, "record") else ledger.get(key)
            if not record:
                return job.get("confirmation_status")

            phase = record.get("phase")
            target_status = None

            if phase == PHASE_VERIFIED:
                target_status = "submitted"
            elif phase == PHASE_UNVERIFIED:
                target_status = "submission_unverified"
            elif phase == PHASE_IN_PROGRESS:
                if hasattr(ledger, "is_stale_in_progress") and ledger.is_stale_in_progress(key):
                    target_status = "reconciliation_required"
                else:
                    target_status = "submitting"

            if target_status and target_status != job.get("confirmation_status"):
                try:
                    self.transition_confirmation(job_id, target_status)
                    return target_status
                except InvalidStateTransitionError:
                    # If direct transition blocked by strict lifecycle, update directly under ledger projection
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE jobs SET confirmation_status = ? WHERE job_id = ?",
                            (target_status, job_id),
                        )
                        conn.commit()
                    return target_status

        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Error syncing confirmation from ledger for %s: %s", job_id, exc)

        return job.get("confirmation_status")

    def delete_job(self, job_id: str) -> None:
        """Delete a job record completely from the database."""
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    def archive_job(self, job_id: str, reason: str = "expired") -> Optional[dict]:
        """Move a job to archived_jobs and remove it from the active jobs table.

        Preserves history so expired/pruned jobs can be audited later without
        cluttering the active queue.
        Returns the job dict before archiving, or None if the job was not found.
        """
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            job = dict(row)
            conn.execute(
                """
                INSERT OR REPLACE INTO archived_jobs
                    (job_id, source, title, company, location, url, score,
                     status, discovered_at, archived_at, archive_reason, extra_json, confirmation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"],
                    job.get("source"),
                    job.get("title"),
                    job.get("company"),
                    job.get("location"),
                    job.get("url"),
                    job.get("score"),
                    job.get("status"),
                    job.get("discovered_at"),
                    now,
                    reason,
                    job.get("extra_json"),
                    job.get("confirmation_status"),
                ),
            )
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return job

    def get_stale_jobs(
        self,
        max_age_days: int = 30,
        statuses: Optional[list] = None,
    ) -> list[dict]:
        """Return active jobs older than max_age_days in the given statuses.

        Defaults to 'discovered' and 'approved' — the two states where a job
        can sit indefinitely without triggering the JobExpiredError path.
        """
        if statuses is None:
            statuses = ["discovered", "approved"]
        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ({placeholders}) AND discovered_at < ?
                ORDER BY discovered_at ASC
                """,
                (*statuses, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_score(self, job_id: str, score: int, reason: str, flags: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET score = ?, score_reason = ?, flags = ? WHERE job_id = ?",
                (score, reason, flags, job_id),
            )

    def update_job_details(self, job_id: str, job: dict) -> None:
        """Refresh display fields for an existing job without changing status."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET title = COALESCE(NULLIF(?, ''), title),
                    company = COALESCE(NULLIF(?, ''), company),
                    location = COALESCE(NULLIF(?, ''), location),
                    salary_raw = COALESCE(NULLIF(?, ''), salary_raw),
                    remote_type = COALESCE(NULLIF(?, ''), remote_type),
                    url = COALESCE(NULLIF(?, ''), url),
                    description = COALESCE(NULLIF(?, ''), description)
                WHERE job_id = ?
                """,
                (
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("location", ""),
                    job.get("salary_raw", ""),
                    job.get("remote_type", ""),
                    job.get("url", ""),
                    job.get("description", ""),
                    job_id,
                ),
            )

    def record_apply_attempt(
        self,
        job_id: str,
        status: str,
        detail: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Persist the most recent apply attempt outcome into extra_json.
        Does NOT change the job status field. Increments attempt_count each call.
        Callers: orchestrator.apply_approved() after every attempt, success or block.
        """
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            extra: dict = {}
            if row and row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    pass
            extra["apply_last_attempt"] = now
            extra["apply_last_status"]  = status
            extra["apply_last_detail"]  = (detail or "")[:500]
            extra["apply_attempt_count"] = extra.get("apply_attempt_count", 0) + 1
            # A fresh attempt supersedes any earlier clear_session_block() flag —
            # otherwise a stale flag from a prior sign-in could mask a brand-new
            # block recorded by this attempt.
            extra.pop("session_prepared_at", None)
            # P1 instrumentation: a durable success flag on EVERY attempt (success or
            # failure) so success-rate is computable. Previously only rich analytics
            # were recorded, and only on success — leaving `submitted` null everywhere.
            extra["submitted"] = str(status).strip().lower() == "applied"
            # P2: stamp the control-flow class so the circuit breaker and dashboards
            # can reason about this outcome without re-deriving it.
            from .blocker_classifier import classify
            extra["blocker_class"] = classify(status).value
            if metadata:
                extra.update(metadata)
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (json.dumps(extra), job_id),
            )

    def clear_session_block(self, job_id: str) -> None:
        """Mark a session/portal-blocked job retryable after prepare_sessions() opens
        its portal for a human sign-in.

        record_apply_attempt() is the only writer of apply_last_status, and
        prepare_sessions() never calls it — so without this, _classify_apply_readiness
        keeps reading the stale auth-wall status from the job's last apply attempt
        and marks the job blocked forever, even after the human signs in.

        Stamps `session_prepared_at` rather than blanking apply_last_status/detail:
        get_apply_funnel() skips any job whose apply_last_status is empty, so erasing
        it would silently drop the job from attempt/failure/per-source funnel stats
        until another apply attempt ran. _classify_apply_readiness treats a truthy
        session_prepared_at as "ready to retry", while leaving apply_last_status
        intact for telemetry. record_apply_attempt() clears the flag again on the
        next recorded attempt so a stale flag can't mask a fresh block.

        The marker is stamped even when there is no prior apply_last_status: a
        newly-approved USAJobs job is classified needs-session with no attempt yet,
        so requiring a prior status would leave it blocked until the user burned a
        wasted apply cycle first (Codex #57).
        """
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            extra: dict = {}
            if row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    pass
            extra["session_prepared_at"] = now
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (json.dumps(extra), job_id),
            )

    def record_preflight_block(self, job_id: str, readiness: str, reason: str) -> None:
        """Persist a preflight-only block without replacing the real apply outcome.

        A preflight classification happens before a browser or form interaction, so
        it must not increment apply_attempt_count or overwrite apply_last_status.
        In particular, retaining a concrete portal outcome such as
        workday_session_expired lets prepare_sessions() route the job to the right
        authenticated portal on the following run.
        """
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            extra: dict = {}
            if row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    pass
            extra["preflight_readiness"] = readiness
            extra["preflight_block_reason"] = (reason or "")[:500]
            extra["preflight_blocked_at"] = now
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (json.dumps(extra), job_id),
            )

    def flag_circuit_break(self, job_id: str, blocker_class: str, reason: str) -> None:
        """P2: record that the circuit breaker skipped this job, WITHOUT touching
        apply_last_status / apply_attempt_count (so the real blocker and count are
        preserved for classification). Lets dashboards surface circuit-broken jobs.
        """
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            extra: dict = {}
            if row and row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    pass
            extra["circuit_broken"] = True
            extra["circuit_class"] = blocker_class
            extra["circuit_reason"] = (reason or "")[:300]
            extra["circuit_broken_at"] = now
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (json.dumps(extra), job_id),
            )

    def record_application_analytics(self, job_id: str, analytics: dict) -> None:
        """Merge analytics dict (atsScore, resumeVersion, applicationMethod, etc.) into extra_json.
        Safe to call after record_apply_attempt — merges, does not overwrite.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            extra: dict = {}
            if row and row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    pass
            extra.update(analytics)
            conn.execute(
                "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                (json.dumps(extra), job_id),
            )

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_jobs_by_status(self, status: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC, discovered_at DESC",
                (status,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_review(self) -> list[dict]:
        """Jobs that are discovered (scored) but not yet reviewed."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'discovered'
                ORDER BY score DESC, discovered_at DESC
                """,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_approved_unapplied(self) -> list[dict]:
        """Jobs approved for application but not yet applied."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'approved'
                ORDER BY score DESC, discovered_at DESC
                """,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            today_rows = conn.execute(
                """
                SELECT status, COUNT(*) as cnt FROM jobs
                WHERE date(discovered_at) = date('now')
                GROUP BY status
                """,
            ).fetchall()
            stats["today"] = {r["status"]: r["cnt"] for r in today_rows}
            return stats

    # ------------------------------------------------------------------
    # P1 instrumentation: apply funnel & success-rate
    # ------------------------------------------------------------------

    # Failure-status → cluster, so the report groups the noise the way the
    # plan's measured baseline does. This is a *display* grouping only; the
    # authoritative blocker classifier lands in P2 (src/blocker_classifier.py).
    _FAILURE_CLUSTERS = {
        "form_completion": {
            "external_ats_error", "form_not_reached", "submit_not_found",
            "linkedin_external_apply_not_found", "microsoft_apply_not_reached",
            "ats_selector_failed", "resume_upload_failed",
        },
        "auth_session": {
            "workday_session_expired", "brassring_login_required",
            "reauth_failed", "reauth_retry_error",
        },
        "field_completion": {
            "linkedin_stuck_on_required_field", "ats_failure",
            "keyword_coverage_failed", "pdf_text_layer_failed",
        },
        "transient": {
            "browser_timeout", "model_timeout", "unknown_external_ats_error",
        },
        "config_error": {"bad_ats_url", "unknown_source", "error"},
    }

    @classmethod
    def _cluster_for(cls, status: str) -> str:
        s = (status or "").strip()
        for cluster, members in cls._FAILURE_CLUSTERS.items():
            if s in members:
                return cluster
        return "other"

    def get_apply_funnel(self) -> dict:
        """Compute the apply funnel and success rate from persisted extra_json.

        Reads what P1 records on every attempt (`apply_last_status`, `submitted`,
        `apply_attempt_count`). Pure read; safe to call anytime. Returns a dict:
        totals, funnel counts, attempt_success_rate, failure histogram + clusters,
        per-source breakdown, and wasted-retry count.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, status, extra_json FROM jobs"
            ).fetchall()

        total = len(rows)
        status_counts: dict = {}
        attempts = 0
        submitted = 0
        wasted_retries = 0
        failure_hist: dict = {}
        cluster_hist: dict = {}
        per_source: dict = {}

        for r in rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
            try:
                extra = json.loads(r["extra_json"]) if r["extra_json"] else {}
            except Exception:
                extra = {}
            last = extra.get("apply_last_status")
            if not last:
                continue  # no apply attempt recorded for this job

            attempts += 1
            was_submitted = bool(extra.get("submitted")) or str(last).lower() == "applied"
            src = r["source"] or "unknown"
            ps = per_source.setdefault(src, {"attempts": 0, "submitted": 0})
            ps["attempts"] += 1

            if was_submitted:
                submitted += 1
                ps["submitted"] += 1
            else:
                failure_hist[last] = failure_hist.get(last, 0) + 1
                cluster = self._cluster_for(last)
                cluster_hist[cluster] = cluster_hist.get(cluster, 0) + 1
                # Attempts beyond the first on a job that never succeeded = wasted effort.
                wasted_retries += max(0, int(extra.get("apply_attempt_count", 1)) - 1)

        for ps in per_source.values():
            ps["rate"] = (ps["submitted"] / ps["attempts"]) if ps["attempts"] else 0.0

        return {
            "total_jobs": total,
            "status_counts": status_counts,
            "attempts": attempts,
            "submitted": submitted,
            "attempt_success_rate": (submitted / attempts) if attempts else 0.0,
            "failure_histogram": dict(sorted(failure_hist.items(), key=lambda x: -x[1])),
            "failure_clusters": dict(sorted(cluster_hist.items(), key=lambda x: -x[1])),
            "per_source": per_source,
            "wasted_retries": wasted_retries,
        }

    def reset_failed_keyword_jobs(self, dry_run: bool = False) -> dict:
        """Reset approved jobs blocked by the old keyword-validation failure.

        Matches both legacy failures recorded as an ATS failure detail and the
        structured telemetry produced by the normalized keyword coverage check.
        """
        reset_keys = {
            "apply_last_status",
            "apply_attempt_count",
            "submitted",
            "blocker_class",
            "circuit_broken",
            "circuit_class",
            "circuit_reason",
            "circuit_broken_at",
            "apply_last_attempt",
            "apply_validation_metrics",
        }
        matched: list[tuple[str, dict]] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, extra_json FROM jobs WHERE status = 'approved'"
            ).fetchall()

            for row in rows:
                extra: dict = {}
                if row["extra_json"]:
                    try:
                        extra = json.loads(row["extra_json"])
                    except Exception:
                        extra = {}
                metrics = extra.get("apply_validation_metrics") or {}
                detail = str(extra.get("apply_last_detail") or "")
                status = str(extra.get("apply_last_status") or "")
                legacy_keyword_detail = detail.startswith("ATS check failed") and "keyword" in detail.lower()
                is_match = (
                    metrics.get("failure_type") == "keyword_coverage"
                    or status == "keyword_coverage_failed"
                    or legacy_keyword_detail
                )
                if is_match:
                    matched.append((row["job_id"], extra))

            if not dry_run:
                for job_id, extra in matched:
                    for key in reset_keys:
                        extra.pop(key, None)
                    extra.pop("apply_last_detail", None)
                    conn.execute(
                        "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                        (json.dumps(extra) if extra else None, job_id),
                    )

        return {
            "matched": len(matched),
            "reset": 0 if dry_run else len(matched),
            "unmatched": max(0, len(rows) - len(matched)),
        }

    def already_seen(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row is not None
