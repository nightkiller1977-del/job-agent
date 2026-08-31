"""Phase 3 — routing adapter auth-wall outcomes to re-auth / session-prep."""
import pytest
from unittest.mock import patch

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
                return True  # reached a usable portal
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
        def __init__(self):
            self.cleared = []

        def get_approved_unapplied(self):
            return jobs

        def clear_session_block(self, job_id):
            self.cleared.append(job_id)

        def get_job(self, job_id):
            return None

    async def _noop(*args, **kwargs):
        return None

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.config = {}
    o.state = _State()
    o.load_credentials_from_dashboard = _noop
    o._pull_approved_from_cloud = _noop

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True  # interactive: human can actually sign in
        await o.prepare_sessions()
    assert ("jobright", "portal") in calls    # external ATS wall -> portal prep flow
    assert ("linkedin", "authwall") in calls  # source-session block -> source prep
    # Codex #56 P1: both jobs must have their stale auth-wall status cleared after
    # prepare_session() runs, or apply_approved() would classify them as blocked
    # forever even after the human signs in.
    assert set(o.state.cleared) == {"portal", "authwall"}


@pytest.mark.asyncio
async def test_linkedin_prepare_session_returns_true_when_already_authenticated_non_tty(monkeypatch):
    """Codex #57 P2b: an already-authenticated session is verified WITHOUT human
    input, so prepare_session must report success even under launchd (no TTY) —
    otherwise the block never clears on scheduled runs."""
    from unittest.mock import AsyncMock as _AM
    from src.sources.linkedin import LinkedInScraper

    scraper = LinkedInScraper({})
    monkeypatch.setattr(scraper, "_start_browser", _AM(return_value=_AM()))
    monkeypatch.setattr(scraper, "_close_browser", _AM())
    monkeypatch.setattr(scraper, "_delay", _AM())
    monkeypatch.setattr(scraper, "_needs_login", _AM(return_value=False))  # already good

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = await scraper.prepare_session({"url": "https://www.linkedin.com/jobs/view/1"})
    assert result is True


@pytest.mark.asyncio
async def test_linkedin_prepare_session_returns_false_on_login_wall_non_tty(monkeypatch):
    """A login wall that needs a human is unresolved in a non-TTY run, so
    prepare_session must NOT report success (Codex #57 P1)."""
    from unittest.mock import AsyncMock as _AM
    from src.sources.linkedin import LinkedInScraper

    scraper = LinkedInScraper({})
    monkeypatch.setattr(scraper, "_start_browser", _AM(return_value=_AM()))
    monkeypatch.setattr(scraper, "_close_browser", _AM())
    monkeypatch.setattr(scraper, "_delay", _AM())
    monkeypatch.setattr(scraper, "_needs_login", _AM(return_value=True))  # needs manual login

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = await scraper.prepare_session({"url": "https://www.linkedin.com/jobs/view/1"})
    assert result is False


@pytest.mark.asyncio
async def test_indeed_prepare_session_returns_true_on_autologin_non_tty(monkeypatch):
    """Codex #57 P2b: credential auto-login is a verified success without human
    input, so prepare_session must report success even under launchd (no TTY)."""
    from unittest.mock import AsyncMock as _AM
    from src.sources.indeed import IndeedScraper

    scraper = IndeedScraper({})
    monkeypatch.setattr(scraper, "_start_browser", _AM(return_value=_AM()))
    monkeypatch.setattr(scraper, "_close_browser", _AM())
    monkeypatch.setattr(scraper, "_delay", _AM())
    monkeypatch.setattr(scraper, "_on_login_page", lambda page: True)  # login wall present
    monkeypatch.setattr(scraper, "_auto_login", _AM(return_value=True))  # creds work
    monkeypatch.setenv("INDEED_EMAIL", "a@b.co")
    monkeypatch.setenv("INDEED_PASSWORD", "secret")

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = await scraper.prepare_session({})
    assert result is True


@pytest.mark.asyncio
async def test_prepare_sessions_does_not_clear_when_prep_bails_early(monkeypatch):
    """Codex #57 P2: prepare_session() returns False when it can't reach a usable
    portal (e.g. no ATS URL found). Even in an interactive run, the block must NOT
    be cleared then — otherwise the next apply run bypasses the gates and burns an
    attempt on a portal that was never opened."""
    import json as _json
    from src import orchestrator as orch_mod

    def make_scraper(reached):
        class _Scraper:
            def __init__(self, config):
                pass

            async def prepare_session(self, job):
                return reached
        return _Scraper

    monkeypatch.setattr(orch_mod, "SOURCE_MAP", {
        "jobright": make_scraper(False),   # bailed — no usable portal
        "linkedin": make_scraper(True),    # reached the LinkedIn session surface
    })

    jobs = [
        {"job_id": "bailed", "source": "jobright", "title": "T", "company": "C",
         "url": "https://jobright.ai/jobs/info/1",
         "extra_json": _json.dumps({"apply_last_status": "workday_session_expired"})},
        {"job_id": "reached", "source": "linkedin", "title": "T2", "company": "C2",
         "url": "https://www.linkedin.com/jobs/view/2",
         "extra_json": _json.dumps({"apply_last_status": "linkedin_authwall"})},
    ]

    class _State:
        def __init__(self):
            self.cleared = []

        def get_approved_unapplied(self):
            return jobs

        def clear_session_block(self, job_id):
            self.cleared.append(job_id)

        def get_job(self, job_id):
            return None

    async def _noop(*args, **kwargs):
        return None

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.config = {}
    o.state = _State()
    o.load_credentials_from_dashboard = _noop
    o._pull_approved_from_cloud = _noop

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True  # interactive
        await o.prepare_sessions()
    # Only the job that reached a usable portal is cleared; the bailed one is not.
    assert o.state.cleared == ["reached"]


@pytest.mark.asyncio
async def test_prepare_sessions_resets_automated_reauth_circuit_breaker(monkeypatch, tmp_path):
    """A manual `prepare-sessions` success for an automated source (jobright/
    indeed/linkedin) must clear ReauthManager's consecutive-failure streak —
    otherwise a source that hit the circuit breaker's cap stays locked out of
    automated reauth forever even after the human fixes it by hand."""
    import json as _json
    from src import orchestrator as orch_mod
    from src.notifier import record_reauth_event, get_status

    monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
    for i in range(3):
        record_reauth_event("jobright", "automated", "failed", str(i))
    assert len(get_status()["reauth_events"]) == 3

    class _Scraper:
        def __init__(self, config):
            pass

        async def prepare_session(self, job):
            return True

    monkeypatch.setattr(orch_mod, "SOURCE_MAP", {"jobright": _Scraper})

    jobs = [
        {"job_id": "j1", "source": "jobright", "title": "T", "company": "C",
         "url": "https://jobright.ai/jobs/info/1",
         "extra_json": _json.dumps({"apply_last_status": "workday_session_expired"})},
    ]

    class _State:
        def get_approved_unapplied(self):
            return jobs

        def clear_session_block(self, job_id):
            pass

        def get_job(self, job_id):
            return None

    async def _noop(*args, **kwargs):
        return None

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.config = {}
    o.state = _State()
    o.load_credentials_from_dashboard = _noop
    o._pull_approved_from_cloud = _noop

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        await o.prepare_sessions()

    events = get_status()["reauth_events"]
    assert events[-1]["outcome"] == "success"
    assert events[-1]["source"] == "jobright"

    # The circuit breaker in ReauthManager._reauth_automated walks events from the
    # most recent and breaks on the first "success" regardless of mode, so the
    # prior 3 automated failures no longer count toward the cap.
    from src.reauth import ReauthManager
    mgr = ReauthManager(config={})
    with patch("src.reauth._get_source_map") as mock_get_map:
        mock_get_map.return_value = {}
        await mgr._reauth_automated("jobright")
        mock_get_map.assert_called_once()


@pytest.mark.asyncio
async def test_prepare_session_disables_extensions_for_known_teamtailor_portal(monkeypatch):
    """Codex #56 P2 / #57 P2: for a known Teamtailor portal, prepare_session must
    launch with extensions genuinely disabled — the Jobright extension's content
    scripts can crash the Teamtailor renderer. load_extensions=False alone is not
    enough (a Web-Store copy in the profile still loads), so disable_extensions
    must be set too."""
    from src.sources.jobright import JobrightScraper

    scraper = JobrightScraper({})
    captured = {}

    async def _start(load_extensions=False, disable_extensions=False):
        captured["load_extensions"] = load_extensions
        captured["disable_extensions"] = disable_extensions
        raise RuntimeError("stop before touching a real page")

    monkeypatch.setattr(scraper, "_start_browser", _start)

    job = {
        "job_id": "j1", "title": "T", "company": "C", "url": "https://example.com",
        "extra_json": {"ats_url": "https://acme.teamtailor.com/jobs/1/applications/new"},
    }
    with pytest.raises(RuntimeError):
        await scraper.prepare_session(job)
    assert captured["load_extensions"] is False
    assert captured["disable_extensions"] is True


@pytest.mark.asyncio
async def test_prepare_session_keeps_extensions_for_non_teamtailor_portal(monkeypatch):
    """Non-Teamtailor portals (or no recorded portal at all) keep the existing
    behavior — the extension is needed to dismiss Jobright's own popups when
    extracting the external URL from a native Jobright listing, and extensions
    are not force-disabled."""
    from src.sources.jobright import JobrightScraper

    scraper = JobrightScraper({})
    captured = {}

    async def _start(load_extensions=False, disable_extensions=False):
        captured["load_extensions"] = load_extensions
        captured["disable_extensions"] = disable_extensions
        raise RuntimeError("stop before touching a real page")

    monkeypatch.setattr(scraper, "_start_browser", _start)

    job = {
        "job_id": "j2", "title": "T", "company": "C", "url": "https://example.com",
        "extra_json": {"ats_url": "https://acme.wd1.myworkdayjobs.com/job/1"},
    }
    with pytest.raises(RuntimeError):
        await scraper.prepare_session(job)
    assert captured["load_extensions"] is True
    assert captured["disable_extensions"] is False


def test_clear_session_block_resets_status_so_readiness_becomes_ready(tmp_path):
    """Codex #56 P1 (state layer): without clear_session_block(), apply_last_status
    stays a session-blocking status forever (record_apply_attempt is the only
    writer, and prepare_sessions never called it), so _classify_apply_readiness
    — and therefore apply_approved()'s preflight, which skips needs-session/
    needs-portal-login/needs-review jobs outright — keeps the job blocked even
    after the human signs in via prepare_sessions."""
    from src.state_manager import StateManager
    from src.orchestrator import Orchestrator

    sm = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "jid1", "source": "linkedin", "title": "T", "company": "C",
        "location": "", "salary_raw": "", "remote_type": "", "url": "https://x",
        "description": "",
    }
    sm.upsert_job(job)
    sm.record_apply_attempt("jid1", "workday_session_expired", "auth wall")

    o = Orchestrator.__new__(Orchestrator)

    def _job_with_current_extra():
        row = sm._connect().execute(
            "SELECT extra_json FROM jobs WHERE job_id = ?", ("jid1",)
        ).fetchone()
        return {**job, "extra_json": row["extra_json"]}

    readiness, _ = o._classify_apply_readiness(_job_with_current_extra())
    assert readiness == "needs-session"

    sm.clear_session_block("jid1")

    readiness, _ = o._classify_apply_readiness(_job_with_current_extra())
    assert readiness == "ready"


def test_usajobs_prepared_session_becomes_ready(tmp_path):
    """Codex #57 P1: USAJobs returns needs-session at a source-specific early return
    before any status logic. The session-prepared marker must be honored ahead of
    that return, or a prepared USAJobs job can never be made retryable (its blocked
    path would then drop the marker, locking it forever)."""
    from src.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)

    base = {"job_id": "u1", "source": "usajobs", "title": "T", "company": "C",
            "url": "https://www.usajobs.gov/job/1"}
    # Unprepared: blocked as needs-session.
    readiness, _ = o._classify_apply_readiness(base)
    assert readiness == "needs-session"
    # After prepare-sessions stamps the marker: ready to retry.
    prepared = {**base, "extra_json": '{"session_prepared_at": "2026-07-19T00:00:00"}'}
    readiness, _ = o._classify_apply_readiness(prepared)
    assert readiness == "ready"
    # Codex #57: the marker overrides the session block but NOT URL validity — a
    # prepared job with a malformed URL must still route to hydration, never launch
    # the scraper against a bad URL.
    bad_url = {"job_id": "u1", "source": "usajobs", "title": "T", "company": "C",
               "url": "https://www./",
               "extra_json": '{"session_prepared_at": "2026-07-19T00:00:00"}'}
    readiness, _ = o._classify_apply_readiness(bad_url)
    assert readiness == "needs-hydration"


def test_clear_session_block_preserves_apply_last_status_for_funnel(tmp_path):
    """Codex #57 P2: clear_session_block() must NOT blank apply_last_status —
    get_apply_funnel() skips any job whose apply_last_status is empty, so doing
    that would silently drop the job from attempt/failure/per-source funnel
    stats until another apply attempt ran."""
    import json as _json
    from src.state_manager import StateManager

    sm = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "jid1", "source": "linkedin", "title": "T", "company": "C",
        "location": "", "salary_raw": "", "remote_type": "", "url": "https://x",
        "description": "",
    }
    sm.upsert_job(job)
    sm.record_apply_attempt("jid1", "workday_session_expired", "auth wall")

    sm.clear_session_block("jid1")

    row = sm._connect().execute(
        "SELECT extra_json FROM jobs WHERE job_id = ?", ("jid1",)
    ).fetchone()
    extra = _json.loads(row["extra_json"])
    assert extra["apply_last_status"] == "workday_session_expired"
    assert extra["apply_last_detail"] == "auth wall"
    assert extra["session_prepared_at"]

    funnel = sm.get_apply_funnel()
    assert funnel["attempts"] >= 1
    assert funnel["failure_histogram"].get("workday_session_expired", 0) >= 1


def test_record_apply_attempt_clears_stale_session_prepared_flag(tmp_path):
    """A fresh apply attempt must supersede any earlier clear_session_block()
    flag, or a stale flag from a prior sign-in could mask a brand-new block
    recorded by this attempt."""
    import json as _json
    from src.state_manager import StateManager

    sm = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "jid1", "source": "linkedin", "title": "T", "company": "C",
        "location": "", "salary_raw": "", "remote_type": "", "url": "https://x",
        "description": "",
    }
    sm.upsert_job(job)
    sm.record_apply_attempt("jid1", "workday_session_expired", "auth wall")
    sm.clear_session_block("jid1")

    row = sm._connect().execute(
        "SELECT extra_json FROM jobs WHERE job_id = ?", ("jid1",)
    ).fetchone()
    assert _json.loads(row["extra_json"])["session_prepared_at"]

    sm.record_apply_attempt("jid1", "workday_session_expired", "blocked again")

    row = sm._connect().execute(
        "SELECT extra_json FROM jobs WHERE job_id = ?", ("jid1",)
    ).fetchone()
    assert "session_prepared_at" not in _json.loads(row["extra_json"])


def test_clear_session_block_stamps_even_without_prior_status(tmp_path):
    """Codex #57 P2: a newly-approved USAJobs job is classified needs-session with
    no apply_last_status yet, so clear_session_block() must stamp the marker even
    when no prior attempt was recorded — otherwise prepare-sessions can't make it
    retryable without a wasted apply cycle first."""
    import json as _json
    from src.state_manager import StateManager

    sm = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "u1", "source": "usajobs", "title": "T", "company": "C",
        "location": "", "salary_raw": "", "remote_type": "",
        "url": "https://www.usajobs.gov/job/1", "description": "",
    }
    sm.upsert_job(job)  # never attempted — no apply_last_status

    sm.clear_session_block("u1")

    row = sm._connect().execute(
        "SELECT extra_json FROM jobs WHERE job_id = ?", ("u1",)
    ).fetchone()
    assert _json.loads(row["extra_json"] or "{}")["session_prepared_at"]


@pytest.mark.asyncio
async def test_preflight_block_keeps_portal_status_for_prepare_sessions(tmp_path, monkeypatch):
    """A scheduled preflight must not erase the portal status that selects prep."""
    import json as _json
    from src import orchestrator as orch_mod
    from src.state_manager import StateManager

    sm = StateManager(db_path=tmp_path / "jobs.db")
    job = {
        "job_id": "job1", "source": "jobright", "title": "T", "company": "C",
        "location": "", "salary_raw": "", "remote_type": "",
        "url": "https://jobright.ai/jobs/info/1", "description": "",
    }
    sm.upsert_job(job)
    sm.set_status("job1", "approved")
    sm.record_apply_attempt("job1", "workday_session_expired", "Workday sign-in required")

    prepared = []

    class _Jobright:
        def __init__(self, config):
            pass

        async def prepare_session(self, prepared_job):
            prepared.append(prepared_job["job_id"])
            return True

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(orch_mod, "SOURCE_MAP", {"jobright": _Jobright})
    monkeypatch.setattr(orch_mod, "preflight_session_check", lambda sources: None)

    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o.config = {}
    o.state = sm
    o.load_credentials_from_dashboard = _noop
    o._pull_approved_from_cloud = _noop
    o._push_apply_attempt_to_cloud = _noop
    o._log_credential_presence = lambda: None

    # First, a scheduled apply run detects the block. This must not overwrite
    # workday_session_expired with the generic needs-session readiness label.
    await o.apply_approved(auto_submit=True)
    extra = _json.loads(sm.get_job("job1")["extra_json"])
    assert extra["apply_last_status"] == "workday_session_expired"
    assert extra["apply_attempt_count"] == 1

    # Then session preparation must still select the job and stamp the one-shot
    # retry marker without losing the concrete outcome telemetry.
    await o.prepare_sessions()
    extra = _json.loads(sm.get_job("job1")["extra_json"])
    assert prepared == ["job1"]
    assert extra["apply_last_status"] == "workday_session_expired"
    assert extra["session_prepared_at"]


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

    async def _start(load_extensions=False, disable_extensions=False):
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

    async def _start(load_extensions=False, disable_extensions=False):
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

    async def _start(load_extensions=False, disable_extensions=False):
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
