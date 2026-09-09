"""Job Agent Dashboard — FastAPI review/control surface.

MongoDB Atlas is the dashboard's cloud persistence backend. Local Job Agent
SQLite remains a zero-cost offline/journal store; the dashboard has no runtime
Postgres dependency.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

load_dotenv()
app = FastAPI(title="Job Agent Dashboard")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
_static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

MONGODB_URI = os.environ.get("MONGODB_URI", "").strip()
JOB_AGENT_DB = os.environ.get("JOB_AGENT_DB", "job_agent").strip() or "job_agent"
SYNC_SECRET = os.environ.get("SYNC_SECRET", "")
CREDENTIAL_ENCRYPTION_KEY = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
_client: MongoClient | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value, default: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return default
    return default


def _public_doc(doc: dict | None) -> dict:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    return out


def get_db():
    global _client
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI not configured")
    if _client is None:
        _client = MongoClient(
            MONGODB_URI,
            appname="job-agent-dashboard",
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            retryWrites=True,
        )
    return _client[JOB_AGENT_DB]


def init_db() -> None:
    if not MONGODB_URI:
        return
    db = get_db()
    db.command("ping")
    db.jobs.create_index([("job_id", ASCENDING)], unique=True, name="uq_jobs_job_id")
    db.jobs.create_index([("status", ASCENDING), ("score", DESCENDING), ("discovered_at", DESCENDING)], name="idx_jobs_status_score")
    db.jobs.create_index([("discovered_at", DESCENDING)], name="idx_jobs_discovered")
    db.jobs.create_index([("updated_at", DESCENDING)], name="idx_jobs_updated")
    db.sync_events.create_index([("synced_at", DESCENDING)], name="idx_sync_events_synced")
    db.sync_events.create_index([("legacy_postgres_id", ASCENDING)], unique=True, sparse=True, name="uq_sync_legacy_postgres_id")
    db.credentials.create_index([("platform", ASCENDING)], unique=True, name="uq_credentials_platform")
    db.job_relationships.create_index([("from_id", ASCENDING), ("type", ASCENDING)], name="idx_relationships_from")
    db.job_relationships.create_index([("to_id", ASCENDING), ("type", ASCENDING)], name="idx_relationships_to")


def _get_cipher():
    if not CREDENTIAL_ENCRYPTION_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(CREDENTIAL_ENCRYPTION_KEY.encode())
    except Exception:
        return None


def _encrypt_password(plain: str) -> str:
    cipher = _get_cipher()
    if cipher is None:
        raise HTTPException(status_code=503, detail="Credential storage disabled until CREDENTIAL_ENCRYPTION_KEY is configured")
    return cipher.encrypt(plain.encode()).decode()


@app.on_event("startup")
def on_startup():
    init_db()


class ActionRequest(BaseModel):
    job_id: str
    action: str


class ExternalJobRequest(BaseModel):
    url: str


class CredentialsRequest(BaseModel):
    platform: str
    email: str
    password: str


def _stats(db) -> dict:
    stats = {(r.get("_id") or "unknown"): r["cnt"] for r in db.jobs.aggregate([{"$group": {"_id": "$status", "cnt": {"$sum": 1}}}])}
    now = _utcnow()
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    stats["today"] = db.jobs.count_documents({"discovered_at": {"$gte": day_start}})
    return stats


def _sort_score_then_date(cursor, date_field: str):
    return cursor.sort([("score", DESCENDING), (date_field, DESCENDING)])


@app.get("/health")
async def health():
    if not MONGODB_URI:
        return {"ok": False, "database": "unconfigured", "backend": "mongodb"}
    try:
        db = get_db()
        db.command("ping")
        marker = db.migration_state.find_one({"_id": "render-postgres-v1"}, {"status": 1, "jobs": 1, "sync_events": 1})
        return {
            "ok": True,
            "database": "ok",
            "backend": "mongodb",
            "legacy_migration": marker.get("status") if marker else "not-run",
        }
    except Exception:
        return {"ok": False, "database": "unavailable", "backend": "mongodb"}


@app.head("/")
async def head_index():
    return HTMLResponse(status_code=200)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not MONGODB_URI:
        return HTMLResponse("<h1>Dashboard not configured</h1><p>Set <code>MONGODB_URI</code>.</p>", status_code=503)
    try:
        db = get_db()
        stats = _stats(db)
        pending = list(_sort_score_then_date(db.jobs.find({"status": "discovered"}), "discovered_at").limit(100))
        applied = list(db.jobs.find({"status": "applied"}).sort("updated_at", DESCENDING).limit(10))
        approved = list(_sort_score_then_date(db.jobs.find({"status": "approved"}), "updated_at").limit(20))
        sync_log = list(db.sync_events.find({}).sort("synced_at", DESCENDING).limit(5))
        latest_sync = db.sync_events.find_one({}, sort=[("synced_at", DESCENDING)])
        last_sync = latest_sync.get("synced_at") if latest_sync else None
        credentials = {
            row["platform"]: {"email": row.get("email", ""), "password_set": bool(row.get("password"))}
            for row in db.credentials.find({}, {"_id": 0, "platform": 1, "email": 1, "password": 1})
        }
    except Exception as exc:
        return HTMLResponse(f"<h1>Database error</h1><pre>{exc}</pre>", status_code=500)

    needs_prep = {
        "workday_session_expired", "brassring_login_required", "microsoft_login_required",
        "linkedin_authwall", "linkedin_login_required", "needs-session", "needs-portal-login",
    }
    needs_you = {
        "workday_account_required", "brassring_registration_required", "required_field_unanswered",
        "linkedin_stuck_on_required_field", "needs-answer", "needs-review", "needs-hydration",
        "needs_resume_review", "dummy_resume_blocked",
    }
    approved_ready, approved_needs_prep, approved_needs_you = [], [], []
    for row in approved:
        d = _public_doc(row)
        extra_raw = d.get("extra_json")
        extra = extra_raw if isinstance(extra_raw, dict) else {}
        if extra_raw and not isinstance(extra_raw, dict):
            try:
                extra = json.loads(extra_raw)
            except Exception:
                extra = {}
        d["apply_last_status"] = extra.get("apply_last_status", "")
        d["apply_last_detail"] = extra.get("apply_last_detail", "")
        d["apply_attempt_count"] = extra.get("apply_attempt_count", 0)
        status = d["apply_last_status"]
        attempts = d["apply_attempt_count"] or 0
        if status in needs_prep:
            approved_needs_prep.append(d)
        elif status in needs_you or (status == "form_not_detected" and attempts >= 3):
            approved_needs_you.append(d)
        else:
            approved_ready.append(d)
    approved_ready.sort(key=lambda j: -(j.get("score") or 0))

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "stats": stats,
            "pending": [_public_doc(r) for r in pending],
            "applied": [_public_doc(r) for r in applied],
            "approved_ready": approved_ready,
            "approved_needs_prep": approved_needs_prep,
            "approved_needs_you": approved_needs_you,
            "sync_log": [_public_doc(r) for r in sync_log],
            "last_sync": last_sync,
            "now": _utcnow(),
            "credentials": credentials,
        },
    )


@app.post("/api/sync")
async def sync_jobs(request: Request, x_sync_secret: Optional[str] = Header(default=None)):
    if SYNC_SECRET and x_sync_secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Invalid sync secret")
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of jobs")
    db, now, upserted, errors = get_db(), _utcnow(), 0, []
    for raw in body:
        if not isinstance(raw, dict) or not raw.get("job_id"):
            continue
        payload = {
            "job_id": raw["job_id"], "source": raw.get("source", ""), "title": raw.get("title", ""),
            "company": raw.get("company", ""), "location": raw.get("location", ""),
            "salary_raw": raw.get("salary_raw", ""), "remote_type": raw.get("remote_type", ""),
            "url": raw.get("url", ""), "score": raw.get("score"), "score_reason": raw.get("score_reason", ""),
            "flags": raw.get("flags", ""), "extra_json": raw.get("extra_json"),
            "discovered_at": _parse_dt(raw.get("discovered_at"), now), "updated_at": now,
        }
        try:
            db.jobs.update_one(
                {"job_id": raw["job_id"]},
                {"$set": payload, "$setOnInsert": {"status": raw.get("status", "discovered")}},
                upsert=True,
            )
            upserted += 1
        except Exception as exc:
            errors.append(str(exc))
    sources = sorted({j.get("source", "") for j in body if isinstance(j, dict) and j.get("source")})
    db.sync_events.insert_one({"synced_at": now, "job_count": upserted, "source": ", ".join(sources), "notes": f"{len(errors)} errors" if errors else None})
    return {"ok": True, "upserted": upserted, "errors": errors}


@app.post("/api/action")
async def job_action(body: ActionRequest):
    valid = {"approved", "skipped", "bookmarked", "applied", "expired", "archive"}
    if body.action not in valid:
        raise HTTPException(status_code=400, detail=f"action must be one of {valid}")
    status = "skipped" if body.action == "archive" else body.action
    result = get_db().jobs.find_one_and_update(
        {"job_id": body.job_id}, {"$set": {"status": status, "updated_at": _utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job_id": body.job_id, "status": status}


@app.post("/api/jobs/external")
async def add_external_job(body: ExternalJobRequest):
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    job_id = hashlib.md5(url.encode()).hexdigest()[:16]
    lower = url.lower()
    source = "linkedin" if "linkedin.com" in lower else "usajobs" if "usajobs.gov" in lower else "jobright" if "jobright.ai" in lower else "external"
    db = get_db()
    if db.jobs.find_one({"job_id": job_id}, {"_id": 1}):
        raise HTTPException(status_code=400, detail="Job already exists in queue")
    now = _utcnow()
    db.jobs.insert_one({"job_id": job_id, "source": source, "title": "Importing...", "company": "Pending local agent sync", "url": url, "status": "discovered", "flags": "needs_hydration", "discovered_at": now, "updated_at": now})
    return {"ok": True, "job_id": job_id, "url": url}


@app.get("/api/jobs/unhydrated")
async def get_unhydrated(x_sync_secret: Optional[str] = Header(default=None)):
    if SYNC_SECRET and x_sync_secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Invalid sync secret")
    return list(get_db().jobs.find({"flags": {"$regex": "needs_hydration"}}, {"_id": 0, "job_id": 1, "url": 1, "source": 1}))


@app.get("/api/status")
async def api_status():
    try:
        db = get_db()
        latest = db.sync_events.find_one({}, sort=[("synced_at", DESCENDING)])
        return {"stats": _stats(db), "last_sync": latest.get("synced_at").isoformat() if latest and latest.get("synced_at") else None}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/jobs/pending")
async def get_pending():
    return [_public_doc(r) for r in _sort_score_then_date(get_db().jobs.find({"status": "discovered"}), "discovered_at")]


@app.get("/api/jobs/approved")
async def get_approved():
    return [_public_doc(r) for r in _sort_score_then_date(get_db().jobs.find({"status": "approved"}), "updated_at")]


@app.get("/api/errors")
async def get_errors():
    rows = get_db().sync_events.find({"notes": {"$nin": [None, ""]}}).sort("synced_at", DESCENDING).limit(20)
    return [_public_doc(r) for r in rows]


@app.post("/api/credentials")
async def save_credentials(body: CredentialsRequest):
    platform = body.platform.strip().lower()
    valid = {"indeed", "linkedin", "jobright"}
    if platform not in valid:
        raise HTTPException(status_code=400, detail=f"platform must be one of {sorted(valid)}")
    update = {"email": body.email.strip(), "updated_at": _utcnow()}
    if body.password.strip():
        update["password"] = _encrypt_password(body.password.strip())
    get_db().credentials.update_one({"platform": platform}, {"$set": update, "$setOnInsert": {"platform": platform}}, upsert=True)
    return {"ok": True, "platform": platform}
