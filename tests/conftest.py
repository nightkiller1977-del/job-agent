import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock as _MM
import pytest

# Isolate the whole suite from the developer's REAL central secrets store
# (~/Library/Application Support/ai-command-center/.env). Without this, src.secrets
# would fill env vars from the machine's actual secrets, making credential-precedence
# tests non-deterministic and leaking real values into test env. Point it at an empty
# dir; tests that exercise the resolver override AICC_SECRETS_DIR themselves.
os.environ["AICC_SECRETS_DIR"] = tempfile.mkdtemp(prefix="aicc-secrets-test-")

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
