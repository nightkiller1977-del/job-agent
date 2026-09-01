from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.state_manager import StateManager
from src.scorer import JobScorer


SUCCESS_STATUSES = {"inserted", "duplicate"}
TRACKING_QUERY_KEYS = {"redirect", "redirect_url", "target", "target_url", "u", "url"}


@dataclass
class IngestResult:
    status: str
    job_id: str | None = None
    source_event_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "status": self.status,
            "job_id": self.job_id,
            "source_event_id": self.source_event_id,
            "reason": self.reason,
        }.items() if v is not None}


def run_ingest_email_command(args: argparse.Namespace) -> int:
    if not getattr(args, "json_stdin", False):
        _emit({"status": "invalid", "reason": "ingest-email requires --json-stdin"})
        return 2

    try:
        payload = json.load(sys.stdin)
        results = asyncio.run(ingest_email_payload_async(
            payload,
            state=StateManager(args.db_path),
            scorer=JobScorer(),
        ))
    except ValidationError as exc:
        _emit({"status": "invalid", "reason": str(exc)})
        return 2
    except Exception as exc:
        _emit({"status": "failed", "reason": str(exc)})
        return 1

    statuses = [result.status for result in results]
    _emit({
        "status": "ok" if all(status in SUCCESS_STATUSES for status in statuses) else "failed",
        "results": [result.to_dict() for result in results],
    })
    return 0 if all(status in SUCCESS_STATUSES for status in statuses) else 1


def ingest_email_payload(payload: dict[str, Any], state: StateManager, scorer: Any | None = None) -> list[IngestResult]:
    return asyncio.run(ingest_email_payload_async(payload, state, scorer=scorer or _ReviewOnlyScorer()))


async def ingest_email_payload_async(payload: dict[str, Any], state: StateManager, scorer: Any | None = None) -> list[IngestResult]:
    normalized = validate_payload(payload)
    results: list[IngestResult] = []
    scorer = scorer or _ReviewOnlyScorer()

    for index, job in enumerate(normalized["jobs"]):
        job_id = _job_id(normalized["source_event_id"], job, index)
        source_event_id = normalized["source_event_id"]
        score, score_reason, flags, recommended_action = await _score_email_job(job, scorer)
        record = {
            "job_id": job_id,
            "source": "email",
            "title": job["title"],
            "company": job["company"],
            "location": job.get("location", ""),
            "url": job.get("apply_url") or "",
            "description": job.get("redacted_excerpt") or "",
            "status": _status_for_recommendation(recommended_action, job.get("lane", "review")),
            "discovered_at": normalized["received_at"],
            "score": score,
            "score_reason": score_reason,
            "flags": _merge_flags(flags, "email-origin"),
            "recommended_action": recommended_action,
            "email_source": {
                "source_event_id": source_event_id,
                "provider": normalized["source"]["provider"],
                "account": normalized["source"]["account"],
                "message_id": normalized["source"]["message_id"],
                "content_hash": normalized["source"]["content_hash"],
                "received_at": normalized["received_at"],
                "redacted_excerpt": job.get("redacted_excerpt", ""),
                "lane": job.get("lane", "review"),
                "rejected_reason": job.get("rejected_reason"),
            },
        }

        try:
            inserted = state.upsert_job(record)
        except Exception as exc:
            results.append(IngestResult(
                status="failed",
                job_id=job_id,
                source_event_id=source_event_id,
                reason=str(exc),
            ))
            continue

        results.append(IngestResult(
            status="inserted" if inserted else "duplicate",
            job_id=job_id,
            source_event_id=source_event_id,
        ))

    return results


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a JSON object")

    source_event_id = _required_str(payload, "source_event_id")
    received_at = payload.get("received_at") or _now()
    source = _validate_source(payload.get("source"))
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValidationError("jobs must be a non-empty array")

    return {
        "source_event_id": source_event_id,
        "received_at": str(received_at),
        "source": source,
        "jobs": [_validate_job(job, i) for i, job in enumerate(jobs)],
    }


def _validate_source(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError("source must be an object")
    return {
        "provider": _required_str(value, "source.provider"),
        "account": _required_str(value, "source.account"),
        "message_id": _required_str(value, "source.message_id"),
        "content_hash": _required_str(value, "source.content_hash"),
    }


def _validate_job(job: Any, index: int) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValidationError(f"jobs[{index}] must be an object")

    title = _required_str(job, f"jobs[{index}].title")
    company = _required_str(job, f"jobs[{index}].company")
    apply_url = str(job.get("apply_url") or "").strip()
    lane = str(job.get("lane") or "review").strip()

    if apply_url:
        parsed = _validate_apply_url(apply_url)
        apply_url = urllib.parse.urlunparse(parsed)
    elif lane != "review-follow-up":
        raise ValidationError(f"jobs[{index}].apply_url is required unless lane is review-follow-up")

    return {
        "title": title,
        "company": company,
        "location": str(job.get("location") or "").strip(),
        "apply_url": apply_url,
        "lane": lane,
        "redacted_excerpt": _redacted(job.get("redacted_excerpt")),
        "rejected_reason": job.get("rejected_reason"),
    }


def _validate_apply_url(value: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("apply_url must be an absolute http(s) URL")
    host = parsed.hostname or ""
    if _is_private_or_local_host(host):
        raise ValidationError("apply_url must not point to a private or local host")
    if _looks_tracking_host(host):
        raise ValidationError("apply_url must not be a tracking or redirect URL")
    query_keys = {k.lower() for k, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys.intersection(TRACKING_QUERY_KEYS):
        raise ValidationError("apply_url must not contain tracking redirect parameters")
    return parsed._replace(fragment="")


def _required_str(mapping: dict[str, Any], field: str) -> str:
    value = mapping.get(field.split(".")[-1])
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


def _job_id(source_event_id: str, job: dict[str, Any], index: int) -> str:
    raw = "|".join([
        source_event_id,
        str(index),
        job["title"].lower(),
        job["company"].lower(),
        job.get("apply_url", "").lower(),
    ])
    return f"email:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _redacted(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\n", " ").strip()
    return text[:500]


def _is_private_or_local_host(host: str) -> bool:
    h = host.lower()
    return (
        h in {"localhost", "0.0.0.0", "::1"}
        or h.startswith("127.")
        or h.startswith("10.")
        or h.startswith("192.168.")
        or any(h.startswith(f"172.{i}.") for i in range(16, 32))
        or h.startswith("169.254.")
    )


def _looks_tracking_host(host: str) -> bool:
    h = host.lower()
    return "click" in h or "redirect" in h or h.startswith("trk.") or ".trk." in h or h.startswith("tracking.") or ".tracking." in h


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


class ValidationError(ValueError):
    pass


class _ReviewOnlyScorer:
    async def score(self, job: dict[str, Any]) -> tuple[int, str, str, str]:
        return 50, "Email-origin lead queued for review without live scorer.", "FLAG_FOR_REVIEW", "review"


async def _score_email_job(job: dict[str, Any], scorer: Any) -> tuple[int, str, str, str]:
    score_input = {
        "source": "email",
        "title": job["title"],
        "company": job["company"],
        "location": job.get("location", ""),
        "url": job.get("apply_url", ""),
        "description": job.get("redacted_excerpt", ""),
    }
    result = scorer.score(score_input)
    if hasattr(result, "__await__"):
        result = await result
    score, reason, flags, action = result
    return int(score), str(reason or ""), str(flags or ""), str(action or "review")


def _status_for_recommendation(recommended_action: str, lane: str) -> str:
    if recommended_action == "skip":
        return "skipped"
    return "discovered"


def _merge_flags(*flags: str) -> str:
    parts: list[str] = []
    for group in flags:
        for flag in str(group or "").split(","):
            normalized = flag.strip()
            if normalized and normalized not in parts:
                parts.append(normalized)
    if "requires-review" not in parts:
        parts.append("requires-review")
    return ",".join(parts)
