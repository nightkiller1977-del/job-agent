"""Adaptive-recovery round trip against one controlled broken-ATS fixture.

Proves the full loop the plan calls for, with the coordinator faked at the
HTTP boundary (a stateful httpx.MockTransport that mirrors the real
Aicc-Coordinator intake's validation contract — auth, required fields,
submissionState enum, repository-slug resolution, idempotent replay):

    broken ATS (permanent failure)
      → ExternalApplySession.apply
      → FailureEvidence built (real ids, nav http status)
      → POST /incidents/job-agent (real incident_reporter over the wire)
      → coordinator creates ONE repair operation (idempotent on incidentId)
      → binding persisted on the job (repair_operation_id in extra_json)
      → [repair executes out-of-process; stood in for by flipping the
         operation to 'completed' — the Desktop worker's lease-release]
      → resume_repaired_jobs polls the status view
      → ledger reconciled, job unblocked (session_prepared_at)

Everything is real except the browser page (conftest playwright stubs) and
the coordinator process itself.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

import src.incident_reporter as incident_reporter
from src.events import RunLog, read_run
from src.repair_resume import resume_repaired_jobs
from src.sources.adapters.context import AtsApplyResult
from src.sources.adapters.idempotency import SubmissionLedger
from src.state_manager import StateManager, parse_extra_json

TOKEN = "svc-token-e2e"
SLUG = "nightkiller1977-del/job-agent"
ENV = {
    "COORDINATOR_URL": "https://coordinator.test",
    "AICC_JOB_AGENT_SERVICE_TOKEN": TOKEN,
    "JOB_AGENT_REPOSITORY_SLUG": SLUG,
}

REQUIRED_FIELDS = (
    "schemaVersion", "incidentId", "jobId", "attemptId",
    "failureStatus", "submissionState", "repositorySlug",
)
VALID_SUBMISSION_STATES = {
    "not_attempted", "submit_in_progress", "receipt_verified", "submission_unverified",
}


class FakeCoordinator:
    """Stateful stand-in for Aicc-Coordinator's job-agent intake, enforcing the
    same edge rules as internal/jobintake/create.go so this test breaks if the
    job-agent side ever drifts from the wire contract."""

    def __init__(self):
        self.operations = {}
        self.op_by_incident = {}
        self.post_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})

        if request.method == "POST" and request.url.path == "/incidents/job-agent":
            self.post_count += 1
            body = json.loads(request.content)
            for field in REQUIRED_FIELDS:
                if not str(body.get(field) or "").strip():
                    return httpx.Response(400, json={"error": f"{field} is required"})
            if body["submissionState"] not in VALID_SUBMISSION_STATES:
                return httpx.Response(400, json={"error": "invalid submissionState"})
            if len(body.get("detail") or "") > 2000:
                return httpx.Response(400, json={"error": "detail too long"})
            if body["repositorySlug"] != SLUG:
                return httpx.Response(400, json={"error": "repositorySlug does not match any registered repository"})

            incident_id = body["incidentId"]
            if incident_id in self.op_by_incident:
                op = self.operations[self.op_by_incident[incident_id]]
                return httpx.Response(200, json={
                    "id": op["id"], "operationType": "repair",
                    "status": op["status"], "created": False,
                })
            op_id = f"00000000-0000-4000-8000-{len(self.operations):012d}"
            self.operations[op_id] = {
                "id": op_id, "operationType": "repair", "status": "ready",
                "currentStage": "created", "prUrl": "", "promotedCommitSha": "",
                "payload": {"jobAgent": body},
            }
            self.op_by_incident[incident_id] = op_id
            return httpx.Response(201, json={
                "id": op_id, "operationType": "repair", "status": "ready", "created": True,
            })

        if request.method == "GET" and request.url.path.startswith("/incidents/job-agent/operations/"):
            op_id = request.url.path.rsplit("/", 1)[1]
            op = self.operations.get(op_id)
            if not op:
                return httpx.Response(404, json={"error": "operation not found or not accessible to this caller"})
            view = {k: op[k] for k in (
                "id", "operationType", "status", "currentStage", "prUrl", "promotedCommitSha")}
            return httpx.Response(200, json=view)

        return httpx.Response(404, json={"error": "no route"})

    def complete_repair(self, op_id, pr_url):
        """Stands in for the Desktop repair worker's lease release with
        reason='completed' after validation/approval/promotion."""
        self.operations[op_id].update(status="completed", currentStage="released", prUrl=pr_url)


# --- broken-ATS session harness (same shape as test_incident_emission) -------

class FakePage:
    url = "https://boards.brokenats.example/apply"

    async def goto(self, *a, **kw):
        return SimpleNamespace(status=500)

    async def title(self):
        return "Apply"


class _Lock:
    def __init__(self, *a, **kw): ...

    async def acquire_async(self):
        return self

    def release(self): ...


def _session(tmp_path, state_mgr):
    from src.sources.adapters.session import ExternalApplySession

    class _Session(ExternalApplySession):
        _profile_dir = None

    s = _Session.__new__(_Session)
    s.ledger = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    s.run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    s._policy_override = None
    s._maybe_notify = lambda *a, **kw: None
    s.state_manager = state_mgr

    async def _route_auth(*a, **kw):
        return None

    s._route_auth = _route_auth
    page = FakePage()

    async def _start_browser(**kw):
        return page

    async def _close_browser():
        return None

    s._start_browser = _start_browser
    s._close_browser = _close_browser
    s._profile_dir = tmp_path / "profile"

    class _BrokenAtsAdapter:
        """The controlled fixture: an ATS whose posting is permanently gone."""
        name = "broken_ats_fixture"

        async def apply(self, ctx):
            return AtsApplyResult.blocked("bad_ats_url", "ATS returns 500; posting layout unrecognizable")

    class _Registry:
        async def pick(self, ctx):
            return _BrokenAtsAdapter()

    s.registry = _Registry()
    return s


JOB = {"url": "https://boards.brokenats.example/apply/77", "job_id": "j-77"}


@pytest.mark.asyncio
async def test_broken_ats_round_trip(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)

    coordinator = FakeCoordinator()
    transport = httpx.MockTransport(coordinator.handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: real_client(transport=transport),
    )

    state = StateManager(db_path=tmp_path / "jobs.db")
    state.upsert_job({
        "job_id": "j-77", "title": "Director of Engineering", "company": "Acme",
        "source": "external", "status": "approved", "url": JOB["url"],
    })

    # 1. Broken ATS → failure → evidence → incident POSTed over the wire.
    session = _session(tmp_path, state)
    with monkeypatch.context() as m:
        m.setattr("src.sources.adapters.session.ProfileLock", _Lock)
        res = await session.apply(JOB, auto_submit=False)

    assert res.status == "bad_ats_url"
    assert not res.submitted

    # 2. Coordinator holds exactly one repair operation with the full evidence.
    assert len(coordinator.operations) == 1
    op_id, op = next(iter(coordinator.operations.items()))
    wire = op["payload"]["jobAgent"]
    assert wire["schemaVersion"] == "1"
    assert wire["incidentId"] == f"job-j-77-{res.attempt_id}"
    assert wire["jobId"] == "j-77"
    assert wire["attemptId"] == res.attempt_id
    assert wire["runId"] == session.run_log.run_id
    assert wire["adapterName"] == "broken_ats_fixture"
    assert wire["failureStatus"] == "bad_ats_url"
    assert wire["submissionState"] == "not_attempted"
    assert wire["httpStatus"] == 500
    assert wire["repositorySlug"] == SLUG

    # 3. Binding persisted on the job; run log carries the incident event.
    extra = parse_extra_json(state.get_job("j-77")["extra_json"])
    assert extra["repair_operation_id"] == op_id
    assert extra["repair_incident_id"] == wire["incidentId"]
    events = read_run(session.run_log.run_id, runs_dir=tmp_path / "runs")
    assert [e for e in events if e["event"] == "incident_reported"]

    # 4. A duplicate report of the same incident is an idempotent replay,
    #    not a second operation (retry after lost response).
    from src.failure_evidence import build_failure_evidence
    dup = build_failure_evidence(
        JOB, res, run_id=session.run_log.run_id, vendor="greenhouse",
        host="boards.brokenats.example", adapter_name="broken_ats_fixture",
        ledger_phase=None, http_status=500,
    )
    replay = incident_reporter.report_failure(dup)
    assert replay == {"id": op_id, "status": "ready", "created": False, "operationType": "repair"}
    assert len(coordinator.operations) == 1

    # 5. Repair not finished yet → nothing resumes, job stays bound.
    assert resume_repaired_jobs(state=state, ledger=session.ledger, reporter=incident_reporter) == 0
    assert "session_prepared_at" not in parse_extra_json(state.get_job("j-77")["extra_json"])

    # 6. Repair lands (Desktop worker releases the lease as 'completed').
    coordinator.complete_repair(op_id, pr_url="https://github.com/nightkiller1977-del/job-agent/pull/999")

    # 7. Resume: status polled, ledger reconciled, job unblocked.
    resumed = resume_repaired_jobs(state=state, ledger=session.ledger, reporter=incident_reporter)
    assert resumed == 1
    extra = parse_extra_json(state.get_job("j-77")["extra_json"])
    assert extra["repair_completed_at"]
    assert extra["repair_pr_url"].endswith("/pull/999")
    assert extra["session_prepared_at"]
    # The fixture never began a submit, so the ledger holds no record for the
    # key — submission truth stays clean after reconciliation.
    from src.sources.adapters.idempotency import canonical_key
    assert session.ledger.record(canonical_key(JOB)) is None

    # 8. Second resume pass is a no-op (completion is stamped, not re-polled).
    assert resume_repaired_jobs(state=state, ledger=session.ledger, reporter=incident_reporter) == 0
