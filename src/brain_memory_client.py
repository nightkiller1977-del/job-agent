"""Brain Memory outcome/goal/challenge ingestion — fail-open transport (ACES-457).

Follows the incident_reporter.py precedent: lazy `import httpx`, env-driven
config, non-fatal try/except everywhere — ingestion must never change an
apply/discover outcome. The one addition over that precedent is a local
durable outbox (state/brain_memory_outbox.jsonl): incident_reporter.py is a
one-shot, no-retry design (acceptable there — the coordinator incident is
re-derivable from job-agent's own local state on the next run), but ACES-457
requires "fire-and-forget with local durable retry, matching the outbox
pattern used elsewhere in the fleet" — an outcome/goal/challenge record has
no other durable copy, so a dropped delivery is a permanently lost record,
not a re-derivable one.

Env config (all three required to ingest; unset = ingestion disabled):
    BRAIN_MEMORY_URL         e.g. https://ai-commander-brain-memory...azurecontainerapps.io
    BRAIN_MEMORY_KEY_ID      public credential identifier (x-brain-key-id)
    BRAIN_MEMORY_SECRET      HMAC signing secret for this credential

Wire contract (AI-Commander-Brain-Memory-'s docs/operations/artifact-ingestion.md):
    POST {BRAIN_MEMORY_URL}/v1/artifacts/ingest
    Headers: x-brain-key-id, x-brain-request-id, x-brain-timestamp, x-brain-signature
    Signed string: "brain-memory-http-ingest-v1\\nPOST\\n/v1/artifacts/ingest\\n
                     <keyId>\\n<requestId>\\n<issuedAt>\\nsha256(<raw body>)"
    Body: {"descriptor": {...}, "sensitivity": "private", "permittedAgents": [...], "sourceText": "..."}

Sensitivity default is deliberately conservative (ACES-457 constraint): every
record defaults to "private" unless a caller explicitly asserts "shared", and
sourceText must never contain resume content, personal contact details, or
employer-identifying strings — only de-identified pattern/outcome metadata.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0
_INGEST_PATH = "/v1/artifacts/ingest"
_SIGNING_DOMAIN = "brain-memory-http-ingest-v1"
_SCOPE = "job-agent"
_OUTBOX_PATH = Path("state/brain_memory_outbox.jsonl")
_MAX_ATTEMPTS = 5


def _config() -> tuple[str, str, str]:
    return (
        os.environ.get("BRAIN_MEMORY_URL", "").rstrip("/"),
        os.environ.get("BRAIN_MEMORY_KEY_ID", ""),
        os.environ.get("BRAIN_MEMORY_SECRET", ""),
    )


def _missing_config() -> list[str]:
    url, key_id, secret = _config()
    return [
        name
        for name, value in (
            ("BRAIN_MEMORY_URL", url),
            ("BRAIN_MEMORY_KEY_ID", key_id),
            ("BRAIN_MEMORY_SECRET", secret),
        )
        if not value
    ]


def is_configured() -> bool:
    return not _missing_config()


def _issued_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sign(body: str, *, key_id: str, request_id: str, issued_at: str, secret: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = "\n".join(
        [_SIGNING_DOMAIN, "POST", _INGEST_PATH, key_id, request_id, issued_at, digest]
    )
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass
class MissionContextRecord:
    """One goal/challenge/outcome record, matching AI-Commander-Brain-Memory-'s
    src/domain/system-brain-context.ts Zod schemas exactly — do not invent a
    parallel shape (ACES-457 scope note)."""
    kind: str  # "goal" | "challenge" | "outcome"
    record_id: str
    fields: dict = field(default_factory=dict)
    sensitivity: str = "private"
    file_name: str = "record.json"


def _build_envelope(record: MissionContextRecord) -> dict:
    now = _issued_at()
    payload = {
        "id": record.record_id,
        "ownerId": _SCOPE,
        "observedAt": now,
        "kind": record.kind,
        **record.fields,
    }
    descriptor = {
        "artifactId": record.record_id,
        "sourceType": f"job-agent-{record.kind}",
        "sourceId": record.record_id,
        "fileName": record.file_name,
        "mimeType": "application/json",
        "ownerId": _SCOPE,
        "personaId": _SCOPE,
        "scope": _SCOPE,
        "capturedAt": now,
    }
    return {
        "descriptor": descriptor,
        "sensitivity": record.sensitivity,
        "permittedAgents": ["brain"],
        "sourceText": json.dumps(payload, separators=(",", ":")),
    }


def _post(envelope: dict, *, client: Any = None) -> bool:
    """One ingest attempt. Returns True on 2xx, False otherwise. Never raises —
    the caller (outbox) decides whether/when to retry."""
    url, key_id, secret = _config()
    body = json.dumps(envelope, separators=(",", ":"))
    request_id = str(uuid.uuid4())
    issued_at = _issued_at()
    signature = _sign(body, key_id=key_id, request_id=request_id, issued_at=issued_at, secret=secret)
    headers = {
        "content-type": "application/json",
        "x-brain-key-id": key_id,
        "x-brain-request-id": request_id,
        "x-brain-timestamp": issued_at,
        "x-brain-signature": signature,
    }
    endpoint = f"{url}{_INGEST_PATH}"
    try:
        import httpx

        if client is not None:
            r = client.post(endpoint, content=body, headers=headers)
        else:
            with httpx.Client(timeout=_TIMEOUT_S) as c:
                r = c.post(endpoint, content=body, headers=headers)
        if 200 <= r.status_code < 300:
            return True
        _log.warning(
            "brain memory ingest rejected (non-fatal): status=%s artifact=%s",
            r.status_code, envelope.get("descriptor", {}).get("artifactId"),
        )
        return False
    except Exception as exc:
        _log.warning(
            "brain memory ingest failed (non-fatal): artifact=%s error=%s",
            envelope.get("descriptor", {}).get("artifactId"), exc,
        )
        return False


def _load_outbox(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue  # a malformed line must not block every OTHER pending row
    except Exception:
        return []
    return rows


def _save_outbox(path: Path, rows: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
        path.write_text(text + ("\n" if rows else ""), encoding="utf-8")
    except Exception as exc:
        _log.warning("brain memory outbox write failed (non-fatal): error=%s", exc)


def enqueue(record: MissionContextRecord, *, client: Any = None, outbox_path: Path = _OUTBOX_PATH) -> None:
    """Attempts immediate delivery; on failure, persists the record to the
    local outbox for a later flush_pending() call. Never raises — a Brain
    Memory hiccup must never affect the apply/discover run that called this."""
    try:
        if not is_configured():
            return
        envelope = _build_envelope(record)
        if _post(envelope, client=client):
            return
        rows = _load_outbox(outbox_path)
        rows.append({"envelope": envelope, "attempts": 1})
        _save_outbox(outbox_path, rows)
    except Exception as exc:
        _log.warning("brain memory enqueue failed (non-fatal): error=%s", exc)


def flush_pending(*, client: Any = None, outbox_path: Path = _OUTBOX_PATH) -> dict:
    """Retries every pending outbox row. Call once near the start of a
    discover/apply run — never blocks the run itself: unconfigured or an
    empty outbox both return immediately. Rows exceeding _MAX_ATTEMPTS are
    dropped (dead-lettered) with a warning log rather than retried forever.
    Returns {"delivered": n, "dead": n, "pending": n} for the caller's own
    logging; never raises."""
    result = {"delivered": 0, "dead": 0, "pending": 0}
    try:
        if not is_configured():
            return result
        rows = _load_outbox(outbox_path)
        if not rows:
            return result
        remaining = []
        for row in rows:
            envelope = row.get("envelope")
            attempts = int(row.get("attempts") or 0)
            if not isinstance(envelope, dict):
                continue  # drop unparseable rows rather than retry them forever
            if _post(envelope, client=client):
                result["delivered"] += 1
                continue
            attempts += 1
            if attempts >= _MAX_ATTEMPTS:
                _log.warning(
                    "brain memory outbox row dead-lettered after %s attempts: artifact=%s",
                    attempts, envelope.get("descriptor", {}).get("artifactId"),
                )
                result["dead"] += 1
                continue
            row["attempts"] = attempts
            remaining.append(row)
        result["pending"] = len(remaining)
        _save_outbox(outbox_path, remaining)
        return result
    except Exception as exc:
        _log.warning("brain memory outbox flush failed (non-fatal): error=%s", exc)
        return result


def emit_outcome(
    *,
    record_id: str,
    technical_success: bool,
    related_goal_ids: Optional[list[str]] = None,
    related_challenge_ids: Optional[list[str]] = None,
    benefit: Optional[dict] = None,
    evidence_refs: Optional[list[str]] = None,
    sensitivity: str = "private",
    client: Any = None,
) -> None:
    """Emits an `outcome` record (technicalSuccess is separate from measured
    user benefit — leave `benefit` fields unset rather than fabricating a
    value; see AI-Commander-Brain-Memory-'s OutcomeContextSchema)."""
    enqueue(
        MissionContextRecord(
            kind="outcome",
            record_id=record_id,
            sensitivity=sensitivity,
            file_name="outcome.json",
            fields={
                "technicalSuccess": technical_success,
                "relatedGoalIds": related_goal_ids or [],
                "relatedChallengeIds": related_challenge_ids or [],
                "benefit": benefit or {},
                "evidenceRefs": evidence_refs or [],
            },
        ),
        client=client,
    )


def emit_challenge(
    *,
    record_id: str,
    title: str,
    status: str,
    impact: str,
    recurrence_count: int = 0,
    related_goal_ids: Optional[list[str]] = None,
    related_components: Optional[list[str]] = None,
    evidence_refs: Optional[list[str]] = None,
    description: Optional[str] = None,
    sensitivity: str = "private",
    client: Any = None,
) -> None:
    """Emits a `challenge` record (recurring friction — e.g. a circuit-breaker
    trip or a recurring ATS extraction failure)."""
    fields: dict = {
        "title": title,
        "status": status,
        "impact": impact,
        "recurrenceCount": recurrence_count,
        "relatedGoalIds": related_goal_ids or [],
        "relatedComponents": related_components or [],
        "evidenceRefs": evidence_refs or [],
    }
    if description:
        fields["description"] = description
    enqueue(
        MissionContextRecord(
            kind="challenge", record_id=record_id, sensitivity=sensitivity,
            file_name="challenge.json", fields=fields,
        ),
        client=client,
    )


def emit_goal(
    *,
    record_id: str,
    title: str,
    status: str,
    priority: str,
    success_measures: Optional[list[str]] = None,
    target_date: Optional[str] = None,
    description: Optional[str] = None,
    sensitivity: str = "private",
    client: Any = None,
) -> None:
    """Emits a `goal` record (e.g. target role/company search criteria)."""
    fields: dict = {
        "title": title,
        "status": status,
        "priority": priority,
        "successMeasures": success_measures or [],
        "targetDate": target_date,
    }
    if description:
        fields["description"] = description
    enqueue(
        MissionContextRecord(
            kind="goal", record_id=record_id, sensitivity=sensitivity,
            file_name="goal.json", fields=fields,
        ),
        client=client,
    )
