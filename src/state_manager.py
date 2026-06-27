"""
State manager — SQLite backend for tracking all discovered/applied/skipped jobs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


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
"""


class StateManager:
    def __init__(self, db_path: str = "state/jobs.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(DB_SCHEMA)
            # Hard-delete any legacy expired rows (pre-archive schema)
            conn.execute("DELETE FROM jobs WHERE status = 'expired'")

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

    def record_apply_attempt(self, job_id: str, status: str, detail: str = "") -> None:
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

    def already_seen(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return row is not None
