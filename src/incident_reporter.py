"""Coordinator incident reporting — fail-open transport for FailureEvidence.

Follows the orchestrator cloud-sync precedent: lazy `import httpx`, env-driven
config, and non-fatal try/except everywhere — reporting must never change an
apply outcome.

Env config (all three required to report; unset = reporting disabled):
    COORDINATOR_URL                  e.g. https://coordinator.example.com
    AICC_JOB_AGENT_SERVICE_TOKEN     Bearer token for the incidents API
    JOB_AGENT_REPOSITORY_SLUG        repo the coordinator should repair,
                                     e.g. 'nightkiller1977-del/job-agent'

Coordinator API:
    POST {COORDINATOR_URL}/incidents/job-agent           → 201/200 {"id", "operationType", "status", "created"}
    GET  {COORDINATOR_URL}/incidents/job-agent/operations/{id}
         → 200 {"id","operationType","status","currentStage","prUrl",
                "promotedCommitSha","createdAt","updatedAt"}
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .failure_evidence import FailureEvidence

_log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


def _config() -> tuple[str, str, str]:
    return (
        os.environ.get("COORDINATOR_URL", "").rstrip("/"),
        os.environ.get("AICC_JOB_AGENT_SERVICE_TOKEN", ""),
        os.environ.get("JOB_AGENT_REPOSITORY_SLUG", ""),
    )


def _missing_config() -> list[str]:
    url, token, slug = _config()
    return [
        name
        for name, value in (
            ("COORDINATOR_URL", url),
            ("AICC_JOB_AGENT_SERVICE_TOKEN", token),
            ("JOB_AGENT_REPOSITORY_SLUG", slug),
        )
        if not value
    ]


def is_configured() -> bool:
    return not _missing_config()


def report_failure(evidence: FailureEvidence, *, client: Any = None) -> Optional[dict]:
    """POST one FailureEvidence to the coordinator. Returns
    {'id','status','created','operationType'} on 200/201, else None. Never raises.

    `client` is an optional injected httpx.Client-compatible object (tests);
    when None a real httpx.Client with a 10s timeout is used.
    """
    missing = _missing_config()
    if missing:
        _log.debug(
            "incident reporting disabled — missing env: %s", ", ".join(missing)
        )
        return None
    url, token, slug = _config()
    payload = dict(evidence.to_dict())
    payload["repositorySlug"] = slug
    endpoint = f"{url}/incidents/job-agent"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        import httpx

        if client is not None:
            r = client.post(endpoint, json=payload, headers=headers)
        else:
            with httpx.Client(timeout=_TIMEOUT_S) as c:
                r = c.post(endpoint, json=payload, headers=headers)
        if r.status_code not in (200, 201):
            _log.warning(
                "incident report rejected (non-fatal): status=%s incident=%s",
                r.status_code, evidence.incident_id,
            )
            return None
        data = r.json()
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "created": data.get("created"),
            "operationType": data.get("operationType"),
        }
    except Exception as exc:
        _log.warning(
            "incident report failed (non-fatal): incident=%s error=%s",
            evidence.incident_id, exc,
        )
        return None


def check_repair_status(operation_id: str, *, client: Any = None) -> Optional[dict]:
    """GET one repair operation's status view. Returns the parsed dict on 200,
    else None. Never raises. status == 'completed' means the repair landed."""
    if not operation_id:
        return None
    missing = _missing_config()
    if missing:
        _log.debug(
            "incident reporting disabled — missing env: %s", ", ".join(missing)
        )
        return None
    url, token, _slug = _config()
    endpoint = f"{url}/incidents/job-agent/operations/{operation_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        import httpx

        if client is not None:
            r = client.get(endpoint, headers=headers)
        else:
            with httpx.Client(timeout=_TIMEOUT_S) as c:
                r = c.get(endpoint, headers=headers)
        if r.status_code != 200:
            _log.warning(
                "repair status check returned %s (non-fatal): operation=%s",
                r.status_code, operation_id,
            )
            return None
        return r.json()
    except Exception as exc:
        _log.warning(
            "repair status check failed (non-fatal): operation=%s error=%s",
            operation_id, exc,
        )
        return None
