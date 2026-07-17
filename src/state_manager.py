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
    extra_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_discovered_at ON jobs(discovered_at);

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
    extra_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_archived_at ON archived_jobs(archived_at);

CREATE TABLE IF NOT EXISTS apply_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id TEXT,
    source TEXT,
    event_type TEXT NOT NULL,
    status TEXT,
    blocker_class TEXT,
    occurred_at TEXT NOT NULL,
    submission_verified INTEGER DEFAULT 0,
    detail TEXT,
    blocker_fingerprint TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_apply_events_job ON apply_events(job_id);
CREATE INDEX IF NOT EXISTS idx_apply_events_type ON apply_events(event_type);
"""



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
                     status, discovered_at, archived_at, archive_reason, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        if job.get("url"):
            # URL changed/hydrated, unblock it if it was permanently blocked
            self.clear_circuit_state(job_id)

    def record_apply_event(
        self,
        job_id: str,
        event_type: str,
        source: str = "",
        status: str = "",
        blocker_class: str = "",
        submission_verified: bool = False,
        detail: str = "",
        blocker_fingerprint: str = "",
        run_id: str = "",
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO apply_events
                (job_id, run_id, source, event_type, status, blocker_class, occurred_at, submission_verified, detail, blocker_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    run_id,
                    source,
                    event_type,
                    status,
                    blocker_class,
                    now,
                    int(submission_verified),
                    (detail or "")[:500],
                    blocker_fingerprint,
                ),
            )

    def record_apply_attempt(
        self,
        job_id: str,
        status: str,
        detail: str = "",
        metadata: dict | None = None,
        is_preflight: bool = False,
        blocker_fingerprint: str = "",
        run_id: str = "",
    ) -> None:
        """Persist the most recent apply attempt outcome into extra_json and apply_events.
        Does NOT change the job status field.
        """
        now = datetime.utcnow().isoformat()
        from .blocker_classifier import classify
        bclass = classify(status).value

        is_applied = (str(status).strip().lower() == "applied")
        if is_applied:
            event_type = "submission_verified"
        elif is_preflight:
            event_type = "preflight_blocked"
        else:
            event_type = "attempt_failed"
            
        self.record_apply_event(
            job_id=job_id,
            event_type=event_type,
            source=metadata.get("source", "") if metadata else "",
            status=status,
            blocker_class=bclass,
            submission_verified=is_applied,
            detail=detail,
            blocker_fingerprint=blocker_fingerprint,
            run_id=run_id,
        )

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
            extra["blocker_class"] = bclass
            
            if not is_preflight:
                extra["lifetime_attempt_count"] = extra.get("lifetime_attempt_count", 0) + 1
                
            current_fp = extra.get("blocker_fingerprint", "")
            if is_applied or (blocker_fingerprint and blocker_fingerprint != current_fp):
                extra["consecutive_failure_count"] = 0
                extra["circuit_open"] = False
                extra["circuit_broken"] = False
            else:
                if not is_applied and not is_preflight:
                    extra["consecutive_failure_count"] = extra.get("consecutive_failure_count", 0) + 1

            if blocker_fingerprint:
                extra["blocker_fingerprint"] = blocker_fingerprint

            extra["submitted"] = is_applied
            if metadata:
                extra.update(metadata)
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

    def clear_circuit_state(self, job_id: str) -> None:
        """Clear circuit breaker and streak counts so a job can be re-attempted.
        Called when underlying data changes (e.g. URL hydrated, profile answered).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row or not row["extra_json"]:
                return
            try:
                extra = json.loads(row["extra_json"])
            except Exception:
                return
            keys_to_remove = [
                "circuit_broken", "circuit_class", "circuit_reason", "circuit_broken_at",
                "apply_last_status", "blocker_class", "consecutive_failure_count", "circuit_open"
            ]
            changed = False
            for k in keys_to_remove:
                if k in extra:
                    del extra[k]
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                    (json.dumps(extra), job_id)
                )

    def clear_circuit_state_for_source(self, source: str, blocker_classes: list[str]) -> None:
        """Clear circuit breaker for all jobs of a source that failed with specific blocker classes.
        Used when a session is refreshed to unblock all AUTH_REQUIRED jobs.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, extra_json FROM jobs WHERE source = ? AND status = 'approved'",
                (source,)
            ).fetchall()
            for row in rows:
                if not row["extra_json"]:
                    continue
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    continue
                if extra.get("blocker_class") in blocker_classes or extra.get("circuit_class") in blocker_classes:
                    keys_to_remove = [
                        "circuit_broken", "circuit_class", "circuit_reason", "circuit_broken_at",
                        "apply_last_status", "blocker_class", "consecutive_failure_count", "circuit_open"
                    ]
                    for k in keys_to_remove:
                        extra.pop(k, None)
                    conn.execute(
                        "UPDATE jobs SET extra_json = ? WHERE job_id = ?",
                        (json.dumps(extra), row["job_id"])
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
        """Compute the apply funnel and success rate from append-only events.
        """
        with self._connect() as conn:
            job_rows = conn.execute("SELECT status FROM jobs").fetchall()
            events = conn.execute(
                """
                SELECT job_id, source, event_type, status, blocker_class 
                FROM apply_events
                ORDER BY occurred_at ASC
                """
            ).fetchall()

        total = len(job_rows)
        status_counts: dict = {}
        for r in job_rows:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

        attempts = 0
        submitted = 0
        wasted_retries = 0
        failure_hist: dict = {}
        cluster_hist: dict = {}
        per_source: dict = {}
        
        job_events = {}
        for e in events:
            job_events.setdefault(e["job_id"], []).append(e)
            
        for jid, evs in job_events.items():
            browser_attempts = [e for e in evs if e["event_type"] == "browser_attempt_started"]
            
            if not browser_attempts and not evs:
                continue
                
            src = evs[0]["source"] or "unknown"
            ps = per_source.setdefault(src, {"attempts": 0, "submitted": 0})
            
            is_submitted = any(e["event_type"] == "submission_verified" for e in evs)
            
            job_attempts = len(browser_attempts)
            if job_attempts == 0 and any(e["event_type"] == "attempt_failed" for e in evs):
                job_attempts = 1
            
            attempts += job_attempts
            ps["attempts"] += job_attempts
            
            if is_submitted:
                submitted += 1
                ps["submitted"] += 1
            else:
                failed_events = [e for e in evs if e["event_type"] == "attempt_failed"]
                if failed_events:
                    last_fail = failed_events[-1]
                    last_status = last_fail["status"]
                    failure_hist[last_status] = failure_hist.get(last_status, 0) + 1
                    
                    cluster = self._cluster_for(last_status)
                    cluster_hist[cluster] = cluster_hist.get(cluster, 0) + 1
                    
                wasted_retries += max(0, job_attempts - 1)
                
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
