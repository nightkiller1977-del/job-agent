"""Session-level incident emission — a blocked PERMANENT failure driven through
the real ExternalApplySession.apply triggers incident_reporter.report_failure
exactly once with the right ids, emits the `incident_reported` run-log event,
and persists the repair binding. Mirrors test_recovery_submit_dispatch_truth's
harness (conftest stubs playwright; only the page/adapter/lock are fakes)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.events import RunLog, read_run
from src.sources.adapters.context import AtsApplyResult
from src.sources.adapters.idempotency import SubmissionLedger

ENV = {
    "COORDINATOR_URL": "https://coordinator.test",
    "AICC_JOB_AGENT_SERVICE_TOKEN": "svc-token-123",
    "JOB_AGENT_REPOSITORY_SLUG": "nightkiller1977-del/job-agent",
}


class FakePage:
    url = "https://jobs.example.com/apply"

    async def goto(self, *a, **kw):
        return SimpleNamespace(status=500)  # nav response the session must capture

    async def title(self):
        return "Apply"


class _Lock:
    def __init__(self, *a, **kw): ...

    async def acquire_async(self):
        return self

    def release(self): ...


def _session(tmp_path, adapter_result):
    from src.sources.adapters.session import ExternalApplySession

    class _Session(ExternalApplySession):
        _profile_dir = None  # shadow the read-only BaseScraper property

    s = _Session.__new__(_Session)
    s.ledger = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    s.run_log = RunLog(agent="test", runs_dir=tmp_path / "runs")
    s._policy_override = None
    s._maybe_notify = lambda *a, **kw: None
    s.state_manager = MagicMock()

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

    class _Adapter:
        name = "stub"

        async def apply(self, ctx):
            return adapter_result

    class _Registry:
        async def pick(self, ctx):
            return _Adapter()

    s.registry = _Registry()
    return s


JOB = {"url": "https://jobs.example.com/apply/123", "job_id": "j-77"}


@pytest.mark.asyncio
async def test_permanent_failure_reports_incident_once(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    # 'bad_ats_url' is PERMANENT and not a recovery-trigger status, so the
    # adapter result flows straight to the attempt_finished failure path.
    s = _session(tmp_path, AtsApplyResult.blocked("bad_ats_url", "posting gone"))

    report = MagicMock(return_value={"id": "op-9", "status": "queued", "created": True})
    with patch("src.sources.adapters.session.ProfileLock", _Lock), \
         patch("src.incident_reporter.report_failure", report):
        res = await s.apply(JOB, auto_submit=False)

    assert res.status == "bad_ats_url"
    assert report.call_count == 1
    evidence = report.call_args.args[0]
    assert evidence.incident_id == f"job-j-77-{res.attempt_id}"
    assert evidence.job_id == "j-77"
    assert evidence.attempt_id == res.attempt_id
    assert evidence.run_id == s.run_log.run_id
    assert evidence.adapter_name == "stub"
    assert evidence.failure_status == "bad_ats_url"
    assert evidence.host == "jobs.example.com"
    assert evidence.http_status == 500
    assert evidence.submission_state.value == "not_attempted"

    # run-log event carries the incident/operation binding
    events = read_run(s.run_log.run_id, runs_dir=tmp_path / "runs")
    reported = [e for e in events if e["event"] == "incident_reported"]
    assert len(reported) == 1
    assert reported[0]["incident_id"] == evidence.incident_id
    assert reported[0]["operation_id"] == "op-9"

    # repair binding persisted through the state-manager seam
    s.state_manager.record_apply_attempt.assert_called_once()
    call = s.state_manager.record_apply_attempt.call_args
    assert call.args[0] == "j-77"
    assert call.args[1] == "bad_ats_url"
    meta = call.kwargs["metadata"]
    assert meta["repair_operation_id"] == "op-9"
    assert meta["repair_incident_id"] == evidence.incident_id
    assert meta["repair_reported_at"]


@pytest.mark.asyncio
async def test_transient_failure_is_not_reported(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    s = _session(tmp_path, AtsApplyResult.blocked("browser_timeout", "slow page"))

    report = MagicMock(return_value={"id": "op-x"})
    with patch("src.sources.adapters.session.ProfileLock", _Lock), \
         patch("src.incident_reporter.report_failure", report):
        res = await s.apply(JOB, auto_submit=False)

    assert res.status == "browser_timeout"
    report.assert_not_called()
    s.state_manager.record_apply_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_no_report_when_env_not_configured(tmp_path, monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    s = _session(tmp_path, AtsApplyResult.blocked("bad_ats_url", "posting gone"))

    report = MagicMock(return_value={"id": "op-x"})
    with patch("src.sources.adapters.session.ProfileLock", _Lock), \
         patch("src.incident_reporter.report_failure", report):
        res = await s.apply(JOB, auto_submit=False)

    assert res.status == "bad_ats_url"
    report.assert_not_called()

    events = read_run(s.run_log.run_id, runs_dir=tmp_path / "runs")
    assert not [e for e in events if e["event"] == "incident_reported"]
