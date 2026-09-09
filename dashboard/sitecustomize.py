"""One-time legacy Render Postgres -> MongoDB cutover hook.

Python imports ``sitecustomize`` during interpreter startup.  The migration is
strictly gated so it runs only for the Render uvicorn process when
MIGRATE_LEGACY_POSTGRES=true.  It is idempotent and leaves Postgres untouched.
Remove this module and psycopg2 after production parity is verified.
"""
from __future__ import annotations

import os
import sys
from datetime import timezone


def _enabled() -> bool:
    flag = os.getenv("MIGRATE_LEGACY_POSTGRES", "").strip().lower()
    # Do not run during pip/build subprocesses; only the dashboard server.
    return flag in {"1", "true", "yes"} and any("uvicorn" in arg.lower() for arg in sys.argv)


def _run() -> None:
    if not _enabled():
        return

    pg_url = os.getenv("DATABASE_URL", "").strip()
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    mongo_db = os.getenv("JOB_AGENT_DB", "job_agent").strip() or "job_agent"
    if not pg_url or not mongo_uri:
        print("[migration] skipped: DATABASE_URL or MONGODB_URI missing")
        return

    try:
        import psycopg2
        import psycopg2.extras
        from pymongo import MongoClient
    except Exception as exc:
        print(f"[migration] dependencies unavailable: {exc}")
        return

    marker_id = "render-postgres-v1"
    client = None
    pg = None
    try:
        client = MongoClient(mongo_uri, appname="job-agent-postgres-migration", serverSelectionTimeoutMS=10000)
        db = client[mongo_db]
        db.command("ping")
        if db.migration_state.find_one({"_id": marker_id, "status": "complete"}):
            print("[migration] already complete; skipping")
            return

        # Render Postgres requires TLS. Passing sslmode explicitly also handles
        # legacy DATABASE_URL values that lack a query parameter.
        pg = psycopg2.connect(pg_url, sslmode="require", connect_timeout=10)
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM jobs")
            jobs = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM sync_log ORDER BY id")
            sync_rows = [dict(r) for r in cur.fetchall()]

        migrated_jobs = 0
        for row in jobs:
            job_id = row.get("job_id")
            if not job_id:
                continue
            # Preserve dashboard-only approval/application state exactly.
            db.jobs.update_one(
                {"job_id": job_id},
                {"$set": row},
                upsert=True,
            )
            migrated_jobs += 1

        migrated_sync = 0
        for row in sync_rows:
            legacy_id = row.pop("id", None)
            if legacy_id is None:
                continue
            db.sync_events.update_one(
                {"legacy_postgres_id": legacy_id},
                {"$set": {**row, "legacy_postgres_id": legacy_id}},
                upsert=True,
            )
            migrated_sync += 1

        now = __import__("datetime").datetime.now(timezone.utc)
        db.migration_state.update_one(
            {"_id": marker_id},
            {"$set": {
                "status": "complete",
                "completed_at": now,
                "jobs": migrated_jobs,
                "sync_events": migrated_sync,
                "credentials_migrated": False,
                "note": "Cloud credential fetch is retired; credentials remain in aicc-secrets/local secret store.",
            }},
            upsert=True,
        )
        print(f"[migration] complete: jobs={migrated_jobs} sync_events={migrated_sync}")
    except Exception as exc:
        # Fail the migration visibly but do not prevent the old deployment from
        # starting. Production cutover validation will block Postgres deletion.
        print(f"[migration] failed: {type(exc).__name__}: {exc}")
        try:
            if client is not None:
                client[mongo_db].migration_state.update_one(
                    {"_id": marker_id},
                    {"$set": {"status": "failed", "error_type": type(exc).__name__}},
                    upsert=True,
                )
        except Exception:
            pass
    finally:
        if pg is not None:
            pg.close()
        if client is not None:
            client.close()


_run()
