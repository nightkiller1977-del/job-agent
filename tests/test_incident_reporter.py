"""incident_reporter transport tests — all offline via httpx.MockTransport
injected through the `client` parameter (dependency injection; no network)."""
import httpx
import pytest

from src import incident_reporter
from src.failure_evidence import FailureEvidence, SubmissionState

ENV = {
    "COORDINATOR_URL": "https://coordinator.test",
    "AICC_JOB_AGENT_SERVICE_TOKEN": "svc-token-123",
    "JOB_AGENT_REPOSITORY_SLUG": "nightkiller1977-del/job-agent",
}


def _evidence():
    return FailureEvidence(
        incident_id="job-j1-a1",
        job_id="j1",
        run_id="r1",
        attempt_id="a1",
        adapter_name="greenhouse",
        vendor="greenhouse",
        host="boards.greenhouse.io",
        failure_status="bad_ats_url",
        submission_state=SubmissionState.NOT_ATTEMPTED,
        blocker_class="permanent",
        detail="404 posting gone",
        http_status=404,
    )


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _set_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _clear_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_report_disabled_without_env(monkeypatch):
    _clear_env(monkeypatch)
    calls = []

    def handler(request):  # must never be reached
        calls.append(request)
        return httpx.Response(201, json={})

    assert incident_reporter.is_configured() is False
    assert incident_reporter.report_failure(_evidence(), client=_client(handler)) is None
    assert incident_reporter.check_repair_status("op-1", client=_client(handler)) is None
    assert calls == []


def test_report_disabled_when_slug_missing(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("JOB_AGENT_REPOSITORY_SLUG")
    assert incident_reporter.is_configured() is False
    assert incident_reporter.report_failure(_evidence()) is None


def test_report_payload_shape_and_auth(monkeypatch):
    _set_env(monkeypatch)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "id": "op-uuid-1", "operationType": "repair",
            "status": "queued", "created": True,
        })

    out = incident_reporter.report_failure(_evidence(), client=_client(handler))
    assert out == {"id": "op-uuid-1", "status": "queued", "created": True,
                   "operationType": "repair"}
    assert seen["url"] == "https://coordinator.test/incidents/job-agent"
    assert seen["auth"] == "Bearer svc-token-123"
    body = seen["body"]
    assert body["repositorySlug"] == "nightkiller1977-del/job-agent"
    assert body["schemaVersion"] == "1"
    assert body["incidentId"] == "job-j1-a1"
    assert body["jobId"] == "j1"
    assert body["runId"] == "r1"
    assert body["attemptId"] == "a1"
    assert body["adapterName"] == "greenhouse"
    assert body["vendor"] == "greenhouse"
    assert body["failureStatus"] == "bad_ats_url"
    assert body["submissionState"] == "not_attempted"
    assert body["challengeDetected"] is False
    assert body["httpStatus"] == 404
    assert body["blocker"] is None
    assert body["detail"] == "404 posting gone"


@pytest.mark.parametrize("code", [400, 401, 500])
def test_report_non_2xx_returns_none_without_raising(monkeypatch, code):
    _set_env(monkeypatch)

    def handler(request):
        return httpx.Response(code, json={"error": "nope"})

    assert incident_reporter.report_failure(_evidence(), client=_client(handler)) is None


def test_report_transport_error_returns_none(monkeypatch):
    _set_env(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("boom")

    assert incident_reporter.report_failure(_evidence(), client=_client(handler)) is None


def test_check_repair_status_parsing(monkeypatch):
    _set_env(monkeypatch)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "id": "op-uuid-1", "operationType": "repair", "status": "completed",
            "currentStage": "promoted", "prUrl": "https://github.com/x/y/pull/9",
            "promotedCommitSha": "abc123", "createdAt": "t0", "updatedAt": "t1",
        })

    out = incident_reporter.check_repair_status("op-uuid-1", client=_client(handler))
    assert seen["url"] == "https://coordinator.test/incidents/job-agent/operations/op-uuid-1"
    assert seen["auth"] == "Bearer svc-token-123"
    assert out["status"] == "completed"
    assert out["prUrl"] == "https://github.com/x/y/pull/9"


def test_check_repair_status_non_200_and_empty_id(monkeypatch):
    _set_env(monkeypatch)

    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    assert incident_reporter.check_repair_status("op-x", client=_client(handler)) is None
    assert incident_reporter.check_repair_status("", client=_client(handler)) is None
