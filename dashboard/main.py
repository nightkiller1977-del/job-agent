"""Job Agent Dashboard — FastAPI review/control surface.

MongoDB Atlas is the dashboard's cloud persistence backend. Local Job Agent
SQLite remains a zero-cost offline/journal store; the dashboard has no runtime
Postgres dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
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

# Paths reachable without the shared secret: the app's own liveness probe, which
# carries no data and no side effects, plus the login page that exchanges the
# shared secret for a session cookie. /metrics is deliberately NOT here — it
# exposes the Prometheus process/runtime registry, and no public scraper depends
# on it, so it authenticates like any other machine route.
_UNAUTHENTICATED_PATHS = frozenset({"/health", "/login"})
# Static assets are needed to render the login page and the authenticated shell;
# they contain no application data.
_UNAUTHENTICATED_PREFIXES = ("/static/",)
# Header form of the shared secret. Machine callers (src/orchestrator.py) and the
# public apply ingress send it; handler behaviour is unchanged.
_SYNC_SECRET_HEADER = "x-sync-secret"
# Browser session cookie. HttpOnly so page scripts cannot read it, SameSite=Strict
# so a cross-site request cannot ride it, Secure whenever the request is https.
_SESSION_COOKIE = "ja_session"
_SESSION_MAX_AGE = 12 * 60 * 60
_ACTION_IDEMPOTENCY_KEY_LIMIT = 64
# Browser navigations without a session are redirected here. Kept as a constant so
# the middleware and the route cannot drift apart.
_LOGIN_REDIRECT = "/login"


def _session_signature(token: str) -> str:
    return hmac.new(SYNC_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def _make_session_token() -> str:
    # The issue time is bound into the signed payload, so expiry is enforced by
    # THIS process rather than left to the browser honoring max-age. A copied
    # cookie stops working after _SESSION_MAX_AGE even while SYNC_SECRET is stable.
    token = f"{secrets.token_urlsafe(32)}.{int(time.time())}"
    return f"{token}.{_session_signature(token)}"


def _valid_session_token(value: str) -> bool:
    """True only for an unexpired token carrying a signature minted from the
    current secret.

    Signing (rather than storing sessions) keeps the gate stateless and
    restart-safe and invalidates every session the moment SYNC_SECRET rotates.
    """
    if not SYNC_SECRET:
        return False
    token, _, signature = value.rpartition(".")
    if not token or not signature:
        return False
    if not hmac.compare_digest(signature, _session_signature(token)):
        return False
    random_part, _, issued_raw = token.rpartition(".")
    if not random_part:
        return False
    try:
        issued_at = int(issued_raw)
    except ValueError:
        return False
    # Reject future-dated payloads too: a signed issue time that is not yet past
    # is either clock skew or a forged payload, and must not extend the window.
    now = time.time()
    return 0 <= now - issued_at <= _SESSION_MAX_AGE


def _is_secure_request(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects (never an open redirect)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


class SharedSecretMiddleware(BaseHTTPMiddleware):
    """Fail-closed authentication gate for every non-probe route.

    This dashboard controls employer-facing submission state and stores job-board
    credential emails, yet most routes were previously open: an unauthenticated
    GET / rendered the queues and credential emails, and an unauthenticated
    POST /api/action could mark jobs applied/archived. The gate is fail-closed —
    if SYNC_SECRET is unset the app refuses to serve anything but /health
    rather than silently reverting to a public site.

    Browsers authenticate through a one-time exchange at /login, which sets an
    HttpOnly session cookie; the shared secret therefore never appears in a URL,
    browser history, or access log, and is never readable by page scripts.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _UNAUTHENTICATED_PATHS or path.startswith(_UNAUTHENTICATED_PREFIXES):
            return await call_next(request)
        if SYNC_SECRET and self._authenticated(request):
            return await call_next(request)
        # Return responses directly: this middleware sits OUTSIDE Starlette's
        # ExceptionMiddleware, so an HTTPException raised here would escape as a 500
        # instead of being rendered as 403/503.
        if not SYNC_SECRET:
            return self._denied(503, "Dashboard authentication is not configured (SYNC_SECRET unset).")
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return self._denied(403, "Invalid sync secret")
        # A browser navigation: send it to the login page instead of a raw 403.
        return RedirectResponse(_LOGIN_REDIRECT, status_code=303)

    @staticmethod
    def _authenticated(request: Request) -> bool:
        provided = request.headers.get(_SYNC_SECRET_HEADER)
        if provided and hmac.compare_digest(provided, SYNC_SECRET):
            return True
        return _valid_session_token(request.cookies.get(_SESSION_COOKIE, ""))

    @staticmethod
    def _denied(status_code: int, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status_code)


app.add_middleware(SharedSecretMiddleware)


def _login_page(error: str = "", next_path: str = "/") -> HTMLResponse:
    banner = f'<p class="err">{html.escape(error)}</p>' if error else ""
    status = 401 if error else 200
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Agent — Sign in</title></head>
<body style="font-family:system-ui,sans-serif;background:#0F172A;color:#E2E8F0;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
  <form method="post" action="/login" style="background:#1E293B;padding:28px;border-radius:12px;min-width:300px">
    <h1 style="font-size:16px;margin:0 0 16px">Job Agent</h1>
    {banner}
    <input type="hidden" name="next" value="{html.escape(next_path)}">
    <label style="display:block;font-size:12px;margin-bottom:6px" for="secret">Sync secret</label>
    <input id="secret" name="secret" type="password" autocomplete="current-password" autofocus
           style="width:100%;padding:8px;border-radius:6px;border:1px solid #334155;background:#0F172A;color:#E2E8F0;box-sizing:border-box">
    <button type="submit" style="margin-top:14px;width:100%;padding:9px;border:0;border-radius:6px;background:#4F46E5;color:#fff;font-weight:600;cursor:pointer">Sign in</button>
  </form>
</body></html>""",
        status_code=status,
    )


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
    out.pop("_action_idempotency_keys", None)
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
    # Read the key at call time, not import time: the previous import-time constant
    # made encryption silently unavailable whenever the environment was populated
    # after module import (and made test results depend on import order).
    key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
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
    idempotency_key: Optional[str] = None
    expected_status: Optional[str] = None
    expected_revision: Optional[int] = None


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


@app.get("/login")
async def login_form(request: Request, next: str = "/"):
    # The middleware exempts /login from the session check, so the "not configured"
    # refusal that every other route gets must be repeated here.
    if not SYNC_SECRET:
        return SharedSecretMiddleware._denied(503, "Dashboard authentication is not configured (SYNC_SECRET unset).")
    return _login_page(next_path=_safe_next(next))


@app.post("/login")
async def login_submit(request: Request):
    if not SYNC_SECRET:
        return SharedSecretMiddleware._denied(503, "Dashboard authentication is not configured (SYNC_SECRET unset).")
    # Parse the body by hand: the login form is a single urlencoded field, and this
    # avoids adding python-multipart solely for it.
    raw = (await request.body()).decode("utf-8", "replace")
    fields = {}
    try:
        fields = urllib.parse.parse_qs(raw)
    except ValueError:
        fields = {}
    supplied = fields.get("secret", [""])[0]
    next_path = _safe_next(fields.get("next", [""])[0])
    if not hmac.compare_digest(supplied, SYNC_SECRET):
        return _login_page("Invalid sync secret.", next_path=next_path)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        _SESSION_COOKIE,
        _make_session_token(),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_is_secure_request(request),
        path="/",
    )
    return response


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


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
            "flags": raw.get("flags", ""), "extra_json": raw.get("extra_json"), "updated_at": now,
        }
        try:
            # discovered_at is insert-only, matching both the old Postgres
            # ON CONFLICT clause (which never listed it in DO UPDATE SET) and
            # local SQLite's upsert_job: a re-sync of an already-known job
            # (e.g. orchestrator.py's hydrate_external_jobs, which sends no
            # discovered_at at all) must not overwrite the original discovery
            # date with $parse_dt's `now` fallback.
            db.jobs.update_one(
                {"job_id": raw["job_id"]},
                {
                    "$set": payload,
                    "$setOnInsert": {
                        "status": raw.get("status", "discovered"),
                        "status_revision": 0,
                        "discovered_at": _parse_dt(raw.get("discovered_at"), now),
                    },
                },
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
    jobs = get_db().jobs
    idempotency_key = body.idempotency_key
    expected_status = body.expected_status
    expected_revision = body.expected_revision
    if expected_status is not None:
        allowed_status_chars = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"
        )
        if (
            not expected_status
            or expected_status != expected_status.strip()
            or len(expected_status) > 64
            or any(char not in allowed_status_chars for char in expected_status)
        ):
            raise HTTPException(status_code=400, detail="invalid expected_status")
    if idempotency_key is not None:
        allowed = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_.:"
        )
        if (
            not idempotency_key
            or idempotency_key != idempotency_key.strip()
            or len(idempotency_key) > 128
            or any(char not in allowed for char in idempotency_key)
        ):
            raise HTTPException(status_code=400, detail="invalid idempotency_key")
        if expected_status is None or expected_revision is None:
            raise HTTPException(
                status_code=400,
                detail="keyed actions require expected_status and expected_revision",
            )
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise HTTPException(status_code=400, detail="invalid expected_revision")
        operation_key = f"{status}:{idempotency_key}"
        action_filter = {
            "job_id": body.job_id,
            "_action_idempotency_keys": {"$ne": operation_key},
            "status": expected_status,
        }
        if expected_revision == 0:
            action_filter["$or"] = [
                {"status_revision": 0},
                {"status_revision": {"$exists": False}},
            ]
        else:
            action_filter["status_revision"] = expected_revision
        result = jobs.find_one_and_update(
            action_filter,
            {
                "$set": {"status": status, "updated_at": _utcnow()},
                "$inc": {"status_revision": 1},
                "$push": {
                    "_action_idempotency_keys": {
                        "$each": [operation_key],
                        "$slice": -_ACTION_IDEMPOTENCY_KEY_LIMIT,
                    }
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            current = jobs.find_one(
                {"job_id": body.job_id},
                {"status": 1, "status_revision": 1, "_action_idempotency_keys": 1},
            )
            if not current:
                raise HTTPException(status_code=404, detail="Job not found")
            operation_recorded = operation_key in current.get(
                "_action_idempotency_keys", []
            )
            current_revision = current.get("status_revision", 0)
            if (
                isinstance(current_revision, bool)
                or not isinstance(current_revision, int)
                or current_revision < 0
            ):
                current_revision = 0
            if operation_recorded and current.get("status") != status:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Current status or revision no longer matches expected state",
                        "current_status": current.get("status"),
                        "current_revision": current_revision,
                    },
                )
            if operation_recorded:
                return {
                    "ok": True,
                    "job_id": body.job_id,
                    "status": status,
                    "status_revision": current_revision,
                    "deduplicated": True,
                }
            if current.get("status") == status:
                # A target-state no-op is safe only at the exact expected
                # revision. Advancing the revision here fences any older
                # in-flight request from an away/back (ABA) status cycle.
                if current_revision != expected_revision:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Current status or revision no longer matches expected state",
                            "current_status": current.get("status"),
                            "current_revision": current_revision,
                        },
                    )
                record_filter = {
                    "job_id": body.job_id,
                    "status": status,
                    "_action_idempotency_keys": {"$ne": operation_key},
                }
                if expected_revision == 0:
                    record_filter["$or"] = [
                        {"status_revision": 0},
                        {"status_revision": {"$exists": False}},
                    ]
                else:
                    record_filter["status_revision"] = expected_revision
                recorded = jobs.update_one(
                    record_filter,
                    {
                        "$inc": {"status_revision": 1},
                        "$push": {
                            "_action_idempotency_keys": {
                                "$each": [operation_key],
                                "$slice": -_ACTION_IDEMPOTENCY_KEY_LIMIT,
                            }
                        }
                    },
                )
                if recorded.matched_count:
                    return {
                        "ok": True,
                        "job_id": body.job_id,
                        "status": status,
                        "status_revision": expected_revision + 1,
                        "deduplicated": True,
                    }
                current = jobs.find_one(
                    {"job_id": body.job_id},
                    {"status": 1, "status_revision": 1, "_action_idempotency_keys": 1},
                )
                if not current:
                    raise HTTPException(status_code=404, detail="Job not found")
                if (
                    operation_key in current.get("_action_idempotency_keys", [])
                    and current.get("status") == status
                ):
                    return {
                        "ok": True,
                        "job_id": body.job_id,
                        "status": status,
                        "status_revision": current.get("status_revision", 0),
                        "deduplicated": True,
                    }
                if current.get("status") != status:
                    refreshed_revision = current.get("status_revision", 0)
                    if (
                        isinstance(refreshed_revision, bool)
                        or not isinstance(refreshed_revision, int)
                        or refreshed_revision < 0
                    ):
                        refreshed_revision = 0
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Current status or revision no longer matches expected state",
                            "current_status": current.get("status"),
                            "current_revision": refreshed_revision,
                        },
                    )
                raise HTTPException(
                    status_code=409,
                    detail="Action could not be atomically deduplicated",
                )
            if (
                expected_status is not None
                and current.get("status") != expected_status
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Current status or revision no longer matches expected state",
                        "current_status": current.get("status"),
                        "current_revision": current_revision,
                    },
                )
            if current_revision != expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Current status or revision no longer matches expected state",
                        "current_status": current.get("status"),
                        "current_revision": current_revision,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail="Action could not be atomically deduplicated",
            )
        return {
            "ok": True,
            "job_id": body.job_id,
            "status": status,
            "status_revision": result.get(
                "status_revision", expected_revision + 1
            ),
            "deduplicated": False,
        }

    action_filter = {"job_id": body.job_id}
    if expected_status is not None:
        action_filter["status"] = expected_status
    result = jobs.find_one_and_update(
        action_filter,
        {
            "$set": {"status": status, "updated_at": _utcnow()},
            "$inc": {"status_revision": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        current = jobs.find_one({"job_id": body.job_id}, {"status": 1})
        if not current:
            raise HTTPException(status_code=404, detail="Job not found")
        if current.get("status") == status:
            return {
                "ok": True,
                "job_id": body.job_id,
                "status": status,
                "status_revision": current.get("status_revision", 0),
            }
        raise HTTPException(
            status_code=409,
            detail="Current status no longer matches expected status",
        )
    return {
        "ok": True,
        "job_id": body.job_id,
        "status": status,
        "status_revision": result.get("status_revision", 0),
    }


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
    # score=None matches Postgres's implicit-NULL behavior for an unset
    # INTEGER column — MongoDB has no schema to fall back on, and templates
    # like index.html do `{% if job.score is not none %}{% if job.score >=
    # 80 %}`: a genuinely absent key renders as Jinja2 Undefined, which
    # passes `is not none` but raises UndefinedError on `>=`, crashing the
    # whole page render for every visitor until this job is hydrated/scored.
    db.jobs.insert_one({"job_id": job_id, "source": source, "title": "Importing...", "company": "Pending local agent sync", "url": url, "status": "discovered", "status_revision": 0, "flags": "needs_hydration", "score": None, "discovered_at": now, "updated_at": now})
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
