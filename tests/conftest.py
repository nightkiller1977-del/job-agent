import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock as _MM
import pytest

# Isolate the whole suite from the developer's REAL central secrets store
# (~/Library/Application Support/ai-command-center/.env). Without this, src.secret_store
# would fill env vars from the machine's actual secrets, making credential-precedence
# tests non-deterministic and leaking real values into test env. Point it at an empty
# dir; tests that exercise the resolver override AICC_SECRETS_DIR themselves.
os.environ["AICC_SECRETS_DIR"] = tempfile.mkdtemp(prefix="aicc-secrets-test-")

# Stub the telemetry stack (openlit / logging_loki) — not installed in the test
# env; telemetry.setup() is a no-op without them and model_span degrades cleanly.
sys.modules.setdefault("openlit", _MM())
sys.modules.setdefault("logging_loki", _MM())

# Stub playwright at session scope — lets any test import src.sources.*
# without needing the real package. Per-test reauth mocking is done via
# dependency injection (_reauth_cls=) rather than sys.modules mutation.
#
# async_playwright() must be awaitable, so we give it an AsyncMock that
# returns a context-manager-like mock; otherwise base._start_browser() blows
# up with "MagicMock can't be used in 'await' expression".
def _make_playwright_stub():
    stub = _MM()

    # page → context → browser → playwright chain, all awaitable where needed
    # AsyncMock so any page.method() call is automatically awaitable
    page_mock = AsyncMock()

    context_mock = _MM()
    context_mock.pages = []  # triggers new_page() path in base._start_browser
    context_mock.new_page = AsyncMock(return_value=page_mock)
    context_mock.storage_state = AsyncMock(return_value={})
    context_mock.close = AsyncMock()

    browser_mock = _MM()
    browser_mock.new_context = AsyncMock(return_value=context_mock)
    browser_mock.close = AsyncMock()

    pw_instance = _MM()
    pw_instance.chromium = _MM()
    pw_instance.chromium.launch = AsyncMock(return_value=browser_mock)
    pw_instance.chromium.launch_persistent_context = AsyncMock(return_value=context_mock)
    pw_instance.stop = AsyncMock()

    # async_playwright() is sync; .start() is the coroutine
    launcher = _MM()
    launcher.start = AsyncMock(return_value=pw_instance)

    stub.async_playwright = _MM(return_value=launcher)
    stub.Page = _MM
    return stub

_pw_stub = _make_playwright_stub()
sys.modules.setdefault("playwright", _MM())
sys.modules.setdefault("playwright.async_api", _pw_stub)
sys.modules.setdefault("playwright.async_api._generated", _MM())

def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run live integration tests against real sites"
    )

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: mark test as requiring a live site and active session/credentials"
    )

def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="need --run-live option to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_browser_pipeline_lock(tmp_path, monkeypatch):
    """Isolate every test's advisory browser-pipeline lock from the real
    repo's state/ dir. commander.attempt_fix() and main.py's discover/apply/
    prepare-sessions/heartbeat all take browser_pipeline_lock.pipeline_lock().
    Without this, any test that reaches attempt_fix() could spuriously fail
    if a real job-agent process (a live launchd apply/discover run, or a
    `prepare-sessions` triggered by a phone deep-link) happens to hold the
    real state/browser-pipeline.lock at the same moment — observed during
    PR #79 review: a genuine `prepare-sessions --source jobright` process
    running on the dev machine made several unrelated attempt_fix() tests
    fail because they weren't isolated from real machine state."""
    import src.browser_pipeline_lock as _lock_mod
    monkeypatch.setattr(_lock_mod, "PROJECT_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _attempt_scope_legacy_vendor_receipt_fakes(request, monkeypatch):
    """Keep the older vendor-adapter fake pages faithful to receipt freshness.

    test_ats_vendor_adapters predates attempt-scoped receipt verification and
    models a configured receipt as visible for the entire page lifetime. That
    makes its positive submit cases indistinguishable from a stale pre-submit
    banner, which production now correctly rejects. Patch only those legacy
    fakes so their configured receipt becomes visible after the modeled final
    submit click. Intermediate Ashby Next actions use evaluate(), not element
    click(), so they do not accidentally create a receipt.
    """
    if request.module.__name__.split(".")[-1] != "test_ats_vendor_adapters":
        return

    specs = (
        ("_AshbyEl", "FakeAshbyPage"),
        ("_GhElement", "GhPage"),
        ("_LeverElement", "LeverFakePage"),
    )

    for element_name, page_name in specs:
        element_cls = getattr(request.module, element_name)
        page_cls = getattr(request.module, page_name)
        original_click = element_cls.click
        original_evaluate = page_cls.evaluate

        async def click(self, _original=original_click):
            await _original(self)
            self.page._submit_dispatched = True

        async def evaluate(self, script, *args, _original=original_evaluate):
            dispatched = getattr(self, "_submit_dispatched", False)
            if "sentinel: acceptance-matcher harness" in script or "thank you for" in script:
                if not dispatched:
                    return None
            if "sentinel: acceptance-count harness" in script and not dispatched:
                return 0
            return await _original(self, script, *args)

        monkeypatch.setattr(element_cls, "click", click)
        monkeypatch.setattr(page_cls, "evaluate", evaluate)
