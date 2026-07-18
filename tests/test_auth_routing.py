"""Phase 3 — routing adapter auth-wall outcomes to re-auth / session-prep."""
import pytest

from src.sources.adapters.auth_routing import (
    is_auth_required, directive_for, ReauthRouter, ManagerReauthRouter,
)


def test_is_auth_required():
    assert is_auth_required("workday_session_expired")
    assert is_auth_required("microsoft_login_required")
    assert is_auth_required("teamtailor_login_required")
    assert is_auth_required("needs_session_prep")
    assert not is_auth_required("applied")
    assert not is_auth_required("submit_not_found")
    assert not is_auth_required("")


def test_directive_for_vendor_portal():
    d = directive_for("teamtailor_login_required", {"source": "linkedin", "company": "Acme Corp"})
    assert d.vendor == "teamtailor" and d.source == "linkedin"
    assert d.action == "prepare_sessions"
    # remediation must target the job's origin source + company (prepare-sessions
    # filters on job["source"], so '--source jobright' would never open this portal)
    assert 'prepare-sessions --source linkedin --company "Acme Corp"' in d.remediation
    assert "--source jobright" not in d.remediation
    assert "teamtailor" in d.remediation
    d2 = directive_for("workday_session_expired", {})
    assert d2.vendor == "workday" and d2.action == "prepare_sessions"
    assert "prepare-sessions --source jobright" in d2.remediation  # default source, no company
    assert "--company" not in d2.remediation
    assert directive_for("applied", {}) is None


@pytest.mark.asyncio
async def test_base_router_records():
    r = ReauthRouter()
    d = directive_for("microsoft_login_required", {})
    assert await r.route(d) is False
    assert r.routed == [d]


@pytest.mark.asyncio
async def test_manager_router_portal_is_human_not_auto():
    r = ManagerReauthRouter(config={})
    d = directive_for("workday_session_expired", {})
    assert await r.route(d) is False        # portal -> prepare-sessions/human, not auto reauth
    assert r.routed and r.routed[0].vendor == "workday"


@pytest.mark.asyncio
async def test_session_routes_auth_outcome(tmp_path, monkeypatch):
    from src.events import RunLog, read_run
    from src.sources.adapters.registry import AtsAdapterRegistry
    from src.sources.adapters.session import ExternalApplySession
    from src.sources.adapters.context import AtsApplyResult
    from src.sources.adapters.idempotency import SubmissionLedger
    from test_adapter_reliability import _RecordingAdapter, _FakePage, JOB

    router = ReauthRouter()
    reg = AtsAdapterRegistry(
        fallback=_RecordingAdapter(AtsApplyResult.blocked("workday_session_expired", "expired")))
    sess = ExternalApplySession({}, registry=reg,
                                ledger=SubmissionLedger(tmp_path / "l.json"),
                                run_log=RunLog(agent="t", runs_dir=tmp_path / "runs"),
                                reauth_router=router)

    async def _start(load_extensions=False):
        return _FakePage()

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", lambda save_session=True: _noop())
    prof = tmp_path / "jobright_profile"
    prof.mkdir()
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "workday_session_expired"
    assert res.analytics.get("reauth_directive", {}).get("action") == "prepare_sessions"
    assert router.routed and router.routed[0].vendor == "workday"
    events = read_run(sess.run_log.run_id, runs_dir=tmp_path / "runs")
    assert any(e["event"] == "auth_required" for e in events)


@pytest.mark.asyncio
async def test_session_preserves_reauth_refresh_signal(tmp_path, monkeypatch):
    from src.events import RunLog, read_run
    from src.sources.adapters.registry import AtsAdapterRegistry
    from src.sources.adapters.session import ExternalApplySession
    from src.sources.adapters.context import AtsApplyResult
    from src.sources.adapters.idempotency import SubmissionLedger
    from test_adapter_reliability import _RecordingAdapter, _FakePage, JOB

    class RefreshRouter(ReauthRouter):
        async def route(self, directive):
            await super().route(directive)
            return True   # ReauthManager refreshed the session -> caller should retry

    router = RefreshRouter()
    reg = AtsAdapterRegistry(fallback=_RecordingAdapter(AtsApplyResult.blocked("session_expired", "x")))
    sess = ExternalApplySession({}, registry=reg, ledger=SubmissionLedger(tmp_path / "l.json"),
                                run_log=RunLog(agent="t", runs_dir=tmp_path / "runs"),
                                reauth_router=router)

    async def _start(load_extensions=False):
        return _FakePage()

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", lambda save_session=True: _noop())
    prof = tmp_path / "jobright_profile"
    prof.mkdir()
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    res = await sess.apply(JOB, auto_submit=True)
    assert res.analytics.get("reauth_refreshed") is True   # retry signal preserved
    events = read_run(sess.run_log.run_id, runs_dir=tmp_path / "runs")
    assert any(e["event"] == "reauth_refreshed" for e in events)


def test_blocker_classifier_maps_vendor_login_statuses():
    from src.blocker_classifier import classify, BlockerClass
    for s in ("microsoft_login_required", "smartrecruiters_login_required",
              "teamtailor_login_required", "brassring_login_required",
              "workday_session_expired"):
        assert classify(s) is BlockerClass.AUTH_REQUIRED, s


@pytest.mark.asyncio
async def test_session_no_routing_on_normal_outcome(tmp_path, monkeypatch):
    from src.events import RunLog
    from src.sources.adapters.registry import AtsAdapterRegistry
    from src.sources.adapters.session import ExternalApplySession
    from src.sources.adapters.context import AtsApplyResult
    from src.sources.adapters.idempotency import SubmissionLedger
    from test_adapter_reliability import _RecordingAdapter, _FakePage, JOB

    router = ReauthRouter()
    reg = AtsAdapterRegistry(fallback=_RecordingAdapter(AtsApplyResult.blocked("submit_not_found")))
    sess = ExternalApplySession({}, registry=reg,
                                ledger=SubmissionLedger(tmp_path / "l.json"),
                                run_log=RunLog(agent="t", runs_dir=tmp_path / "runs"),
                                reauth_router=router)

    async def _start(load_extensions=False):
        return _FakePage()

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", lambda save_session=True: _noop())
    prof = tmp_path / "jobright_profile"
    prof.mkdir()
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    res = await sess.apply(JOB, auto_submit=False)
    assert "reauth_directive" not in res.analytics
    assert router.routed == []


async def _noop():
    return None
