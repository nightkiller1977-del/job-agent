"""
Job Agent Dashboard — FastAPI app for Render.com deployment.
Receives job syncs from the local agent and provides a web UI for review.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Job Agent Dashboard")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Serve static files (self-hosted Tailwind CSS — avoids Safari ITP blocking CDN)
_static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SYNC_SECRET = os.environ.get("SYNC_SECRET", "")

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id       TEXT PRIMARY KEY,
  source       TEXT,
  title        TEXT,
  company      TEXT,
  location     TEXT,
  salary_raw   TEXT,
  remote_type  TEXT,
  url          TEXT,
  score        INTEGER,
  score_reason TEXT,
  flags        TEXT,
  status       TEXT DEFAULT 'discovered',
  discovered_at TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs(discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);

CREATE TABLE IF NOT EXISTS sync_log (
  id         SERIAL PRIMARY KEY,
  synced_at  TIMESTAMPTZ DEFAULT NOW(),
  job_count  INTEGER,
  source     TEXT,
  notes      TEXT
);
"""


def get_conn():
    """Return a new psycopg2 connection, handling Render's postgres:// prefix."""
    db_url = DATABASE_URL
    # psycopg2 requires postgresql:// not postgres://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    return psycopg2.connect(db_url)


def init_db() -> None:
    """Create tables if they don't exist."""
    if not DATABASE_URL:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLES_SQL)
        conn.commit()


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class JobRecord(BaseModel):
    job_id: str
    source: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary_raw: Optional[str] = None
    remote_type: Optional[str] = None
    url: Optional[str] = None
    score: Optional[int] = None
    score_reason: Optional[str] = None
    flags: Optional[str] = None
    status: Optional[str] = "discovered"
    discovered_at: Optional[str] = None


class ActionRequest(BaseModel):
    job_id: str
    action: str  # "approved" | "skipped" | "bookmarked" | "applied" | "expired"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Render/monitoring health check."""
    db_ok = False
    if DATABASE_URL:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    db_ok = cur.fetchone()[0] == 1
        except Exception:
            db_ok = False
    return {"ok": bool(DATABASE_URL) and db_ok, "database": "ok" if db_ok else "unavailable"}


@app.head("/")
async def head_index():
    """Allow HEAD checks on the dashboard root."""
    return HTMLResponse(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page."""
    if not DATABASE_URL:
        return HTMLResponse(
            "<h1>Dashboard not configured</h1>"
            "<p>Set the <code>DATABASE_URL</code> environment variable.</p>",
            status_code=503,
        )

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Stats
                cur.execute(
                    "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
                )
                status_rows = cur.fetchall()
                stats = {r["status"]: r["cnt"] for r in status_rows}

                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM jobs
                    WHERE discovered_at::date = CURRENT_DATE
                    """
                )
                stats["today"] = cur.fetchone()["cnt"]

                # Pending review (discovered)
                cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'discovered'
                    ORDER BY score DESC NULLS LAST, discovered_at DESC
                    LIMIT 100
                    """
                )
                pending = cur.fetchall()

                # Applied (last 10)
                cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'applied'
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """
                )
                applied = cur.fetchall()

                # Approved but not applied (last 10)
                cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'approved'
                    ORDER BY score DESC NULLS LAST, updated_at DESC
                    LIMIT 20
                    """
                )
                approved = cur.fetchall()

                # Sync log (last 5)
                cur.execute(
                    """
                    SELECT * FROM sync_log
                    ORDER BY synced_at DESC
                    LIMIT 5
                    """
                )
                sync_log = cur.fetchall()

                # Last sync time
                cur.execute(
                    "SELECT MAX(synced_at) AS last_sync FROM sync_log"
                )
                row = cur.fetchone()
                last_sync = row["last_sync"] if row else None

    except Exception as exc:
        return HTMLResponse(
            f"<h1>Database error</h1><pre>{exc}</pre>", status_code=500
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "stats": stats,
            "pending": [dict(r) for r in pending],
            "applied": [dict(r) for r in applied],
            "approved": [dict(r) for r in approved],
            "sync_log": [dict(r) for r in sync_log],
            "last_sync": last_sync,
            "now": datetime.now(timezone.utc),
        },
    )


@app.post("/api/sync")
async def sync_jobs(
    request: Request,
    x_sync_secret: Optional[str] = Header(default=None),
):
    """
    Receive a batch of jobs from the local agent.
    Expects a JSON array of job dicts.
    Secured with X-Sync-Secret header (skipped in dev if SYNC_SECRET not set).
    """
    if SYNC_SECRET and x_sync_secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Invalid sync secret")

    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of jobs")

    now = datetime.now(timezone.utc)
    upserted = 0
    errors: list[str] = []

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                for raw in body:
                    if not isinstance(raw, dict) or not raw.get("job_id"):
                        continue

                    discovered_at = raw.get("discovered_at")
                    if not discovered_at:
                        discovered_at = now.isoformat()

                    try:
                        cur.execute(
                            """
                            INSERT INTO jobs
                                (job_id, source, title, company, location,
                                 salary_raw, remote_type, url, score,
                                 score_reason, flags, status, discovered_at,
                                 updated_at)
                            VALUES
                                (%(job_id)s, %(source)s, %(title)s,
                                 %(company)s, %(location)s, %(salary_raw)s,
                                 %(remote_type)s, %(url)s, %(score)s,
                                 %(score_reason)s, %(flags)s, %(status)s,
                                 %(discovered_at)s, NOW())
                            ON CONFLICT (job_id) DO UPDATE SET
                                source       = EXCLUDED.source,
                                title        = EXCLUDED.title,
                                company      = EXCLUDED.company,
                                location     = EXCLUDED.location,
                                salary_raw   = EXCLUDED.salary_raw,
                                remote_type  = EXCLUDED.remote_type,
                                url          = EXCLUDED.url,
                                score        = EXCLUDED.score,
                                score_reason = EXCLUDED.score_reason,
                                flags        = EXCLUDED.flags,
                                updated_at   = NOW()
                            """,
                            {
                                "job_id":       raw.get("job_id"),
                                "source":       raw.get("source", ""),
                                "title":        raw.get("title", ""),
                                "company":      raw.get("company", ""),
                                "location":     raw.get("location", ""),
                                "salary_raw":   raw.get("salary_raw", ""),
                                "remote_type":  raw.get("remote_type", ""),
                                "url":          raw.get("url", ""),
                                "score":        raw.get("score"),
                                "score_reason": raw.get("score_reason", ""),
                                "flags":        raw.get("flags", ""),
                                "status":       raw.get("status", "discovered"),
                                "discovered_at": discovered_at,
                            },
                        )
                        upserted += 1
                    except Exception as row_exc:
                        errors.append(str(row_exc))

                # Write sync log entry
                source_names = list({j.get("source", "") for j in body if isinstance(j, dict)})
                cur.execute(
                    """
                    INSERT INTO sync_log (job_count, source, notes)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        upserted,
                        ", ".join(filter(None, source_names)),
                        f"{len(errors)} errors" if errors else None,
                    ),
                )
            conn.commit()

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"ok": True, "upserted": upserted, "errors": errors}


@app.post("/api/action")
async def job_action(body: ActionRequest):
    """Update a job's status (approved / skipped / bookmarked / applied / expired)."""
    valid_actions = {"approved", "skipped", "bookmarked", "applied", "expired"}
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"action must be one of {valid_actions}",
        )

    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM jobs WHERE job_id = %s", (body.job_id,)
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Job not found")
                cur.execute(
                    "UPDATE jobs SET status = %s, updated_at = NOW() WHERE job_id = %s",
                    (body.action, body.job_id),
                )
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {"ok": True, "job_id": body.job_id, "status": body.action}


@app.get("/api/status")
async def api_status():
    """Return JSON stats."""
    if not DATABASE_URL:
        return {"error": "DATABASE_URL not configured"}

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
                )
                rows = cur.fetchall()
                stats = {r["status"]: r["cnt"] for r in rows}

                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM jobs
                    WHERE discovered_at::date = CURRENT_DATE
                    """
                )
                stats["today"] = cur.fetchone()["cnt"]

                cur.execute("SELECT MAX(synced_at) AS last_sync FROM sync_log")
                row = cur.fetchone()
                last_sync = row["last_sync"].isoformat() if row and row["last_sync"] else None

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return {"stats": stats, "last_sync": last_sync}


@app.get("/api/jobs/pending")
async def get_pending():
    """Jobs with status 'discovered' (need review)."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'discovered'
                    ORDER BY score DESC NULLS LAST, discovered_at DESC
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [dict(r) for r in rows]


@app.get("/api/jobs/approved")
async def get_approved():
    """Jobs approved but not yet applied."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = 'approved'
                    ORDER BY score DESC NULLS LAST, updated_at DESC
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [dict(r) for r in rows]


@app.get("/api/errors")
async def get_errors():
    """Recent sync log entries that had errors."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL not configured")

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM sync_log
                    WHERE notes IS NOT NULL
                    ORDER BY synced_at DESC
                    LIMIT 20
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [dict(r) for r in rows]
