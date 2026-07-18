"""Production wiring for the adapter path (deferred Codex items):
- runtime.py process-scoped singletons (one RunLog/Dispatcher/router per batch)
- jobright.py flag-gate injects the shared runtime into ExternalApplySession
- orchestrator readiness portal-set covers the new vendor login statuses
"""
import sys
from unittest.mock import MagicMock

import pytest

# telemetry.py imports openlit at module top; stub it (like conftest stubs playwright)
# so jobright/orchestrator import in this env without the otel stack.
sys.modules.setdefault("openlit", MagicMock())
sys.modules.setdefault("logging_loki", MagicMock())

from src.sources.adapters import runtime


@pytest.fixture(autouse=True)
def _fresh_runtime():
    runtime.reset()
    yield
    runtime.reset()


def test_runtime_singletons_are_shared():
    rl1, rl2 = runtime.get_run_log(), runtime.get_run_log()
    assert rl1 is rl2 and rl1 is not None
    d1, d2 = runtime.get_dispatcher(), runtime.get_dispatcher()
    assert d1 is d2 and d1 is not None
    r1, r2 = runtime.get_reauth_router({}), runtime.get_reauth_router({})
    assert r1 is r2 and r1 is not None


def test_runtime_reset_drops_singletons():
    rl1 = runtime.get_run_log()
    runtime.reset()
    assert runtime.get_run_log() is not rl1


@pytest.mark.asyncio
async def test_jobright_flag_gate_injects_shared_runtime(monkeypatch, tmp_path):
    """USE_ADAPTER_REGISTRY=1 must construct ExternalApplySession with the shared
    RunLog/Dispatcher/router — not per-call defaults."""
    from src.sources.jobright import JobrightScraper
    import src.sources.adapters.session as session_mod
    from src.sources.adapters.context import AtsApplyResult

    captured = {}

    class RecordingSession:
        def __init__(self, config, **kwargs):
            captured.update(kwargs)

        async def apply(self, job, auto_submit=False):
            captured["applied_job"] = job
            return AtsApplyResult.blocked("review_ready", "test")

    monkeypatch.setattr(session_mod, "ExternalApplySession", RecordingSession)
    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "1")

    scraper = JobrightScraper({"search_settings": {}})
    ok = await scraper.apply_external_ats_job(
        {"job_id": "j1", "title": "SWE"}, "https://boards.greenhouse.io/acme/jobs/1",
    )
    assert ok is False
    assert scraper.last_apply_status == "review_ready"
    # the shared singletons were injected
    assert captured["run_log"] is runtime.get_run_log()
    assert captured["dispatcher"] is runtime.get_dispatcher()
    assert captured["reauth_router"] is runtime.get_reauth_router(None)


def test_readiness_blocks_new_vendor_login_statuses():
    import json
    from src.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)  # method uses no instance state
    for status in ("smartrecruiters_login_required", "teamtailor_login_required",
                   "microsoft_login_required", "workday_session_expired"):
        job = {"job_id": "j1", "url": "https://x.example/apply",
               "extra_json": json.dumps({"apply_last_status": status})}
        readiness, _detail = orch._classify_apply_readiness(job)
        assert readiness == "needs-session", status
