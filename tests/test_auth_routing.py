"""Phase 3 — routing adapter auth-wall outcomes to re-auth / session-prep."""
import pytest

from src.sources.adapters.auth_routing import (
    is_auth_required, directive_for, ReauthRouter, ManagerReauthRouter,
    external_ats_url, needs_external_portal_prep,
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
    # filters on job["source"]/company, then routes portal-blocked jobs to the
    # external-portal prep flow regardless of discovery source)
    assert "prepare-sessions --source linkedin --company 'Acme Corp'" in d.remediation
    assert "--source jobright" not in d.remediation
    assert "teamtailor" in d.remediation
    d2 = directive_for("workday_session_expired", {})
    assert d2.vendor == "workday" and d2.action == "prepare_sessions"
    assert "prepare-sessions --source jobright" in d2.remediation  # default source, no company
    assert "--company" not in d2.remediation
    assert directive_for("applied", {}) is None


def test_directive_shell_quotes_scraped_company():
    import shlex
    # Company names come from scraped listings — a hostile/odd name must not be
    # able to smuggle shell syntax into the copy-pasteable remediation command.
    evil = 'Acme"; rm -rf ~; echo "'
    d = directive_for("workday_session_expired", {"source": "indeed", "company": evil})
    assert f"--company {shlex.quote(evil)}" in d.remediation
    # the quoted form round-trips to the original single argv token
    cmd_tail = d.remediation.split("--company ", 1)[1]
    assert shlex.split(cmd_tail) == [evil]


def test_external_ats_url_extraction():
    ats = "https://acme.wd1.myworkdayjobs.com/job/123"
    assert external_ats_url({"ats_url": ats}) == ats
    assert external_ats_url({"extra_json": {"ats_url": ats}}) == ats
    assert external_ats_url({"extra_json": f'{{"ats_url": "{ats}"}}'}) == ats  # serialized
    assert external_ats_url({"extra_json": "not json"}) == ""
    assert external_ats_url({"ats_url": "javascript:alert(1)"}) == ""  # non-http scheme
    assert external_ats_url({}) == "" and external_ats_url(None) == ""


def test_needs_external_portal_prep_routes_any_origin_portal_job():
    ats = "https://acme.wd1.myworkdayjobs.com/job/123"
    # THE P1: LinkedIn/Indeed-origin job blocked on an external ATS wall must go
    # through the external-portal prep flow, not LinkedInScraper/IndeedScraper
    # prepare_session (which only open linkedin.com / indeed.com).
    for src in ("linkedin", "indeed"):
        job = {"source": src,
               "extra_json": {"apply_last_status": "workday_session_expired", "ats_url": ats}}
        assert needs_external_portal_prep("needs-session", job), src
        assert needs_external_portal_prep("needs-portal-login", job), src
    # needs-review (workday_account_required / brassring_registration_required)
    # must also route to the external portal — the account setup page is on the
    # ATS portal, not on LinkedIn/Indeed.
    for status in ("workday_account_required", "brassring_registration_required"):
        job = {"source": "linkedin",
               "extra_json": {"apply_last_status": status, "ats_url": ats}}
        assert needs_external_portal_prep("needs-review", job), status


def test_needs_external_portal_prep_negative_cases():
    ats = "https://careers.microsoft.com/apply/123"
    # jobright/external sources already dispatch to the portal prep flow
    for src in ("jobright", "external"):
        job = {"source": src,
               "extra_json": {"apply_last_status": "microsoft_login_required", "ats_url": ats}}
        assert not needs_external_portal_prep("needs-session", job), src
    # source's own session is the blocker -> the source's prepare_session is right
    for status in ("linkedin_authwall", "linkedin_login_required"):
        job = {"source": "linkedin", "extra_json": {"apply_last_status": status, "ats_url": ats}}
        assert not needs_external_portal_prep("needs-session", job), status
    # no recorded portal URL -> nothing to open directly
    job = {"source": "linkedin", "extra_json": {"apply_last_status": "workday_session_expired"}}
    assert not needs_external_portal_prep("needs-session", job)
    # readiness classes outside the portal-login set never reroute
    ok = {"source": "linkedin",
          "extra_json": {"apply_last_status": "workday_session_expired", "ats_url": ats}}
    for readiness in ("ready", "needs-hydration", "needs-answer"):
        assert not needs_external_portal_prep(readiness, ok), readiness


@pytest.mark.asyncio
async def test_prepare_sessions_dispatches_portal_blocked_jobs_to_external_prep(monkeypatch):
    """P1 regression: a LinkedIn/Indeed-origin job blocked on an external ATS
    auth wall must be prepped via the external-portal flow (jobright path), while
    a job blocked on the source's own session keeps the source's prep."""
    import json as _json
    from src import orchestrator as orch_mod

    calls = []

    def make_scraper(name):
        class _Scraper:
            def __init__(self, config):
                pass

            async def prepare_session(self, job):
                calls.append((name, job.get("job_id")))
        return _Scraper

    monkeypatch.setattr(orch_mod, "SOURCE_MAP", {
        "jobright": make_scraper("jobright"),
        "linkedin": make_scraper("linkedin"),
        "indeed": make_scraper("indeed"),
    })

    ats = "https://acme.wd1.myworkdayjobs.com/job/1"
    jobs = [
        {"job_id": "portal", "source": "linkedin", "title": "T", "company": "C",
         "url": "https://www.linkedin.com/jobs/view/1",
         "extra_json": _json.dumps(
             {"apply_last_status": "workday_session_expired", "ats_url": ats})},
        {"job_id": "authwall", "source": "linkedin", "title": "T2", "company": "C2",
         "url": "https://www.linkedin.com/jobs/view/2",
         "extra_json": _json.dumps({"apply_last_status": "linkedin_authwall"})},
    ]

    class _State:
        def get_approved_unapplied(self):
            return jobs

    async def _noop(*args, **kwargs):
        return None

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.config = {}
    o.state = _State()
    o.load_credentials_from_dashboard = _noop
    o._pull_approved_from_cloud = _noop

    await o.prepare_sessions()
    assert ("jobright", "portal") in calls    # external ATS wall -> portal prep flow
    assert ("linkedin", "authwall") in calls  # source-session block -> source prep


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
