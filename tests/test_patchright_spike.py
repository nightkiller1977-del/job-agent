"""ACES-402 — unit tests for the engine-benchmark spike (src/discovery/patchright_spike.py).

No live network / no real browser: fakes stand in for Playwright's async API
so these run in CI without a Fortress container, real domains, or even
playwright/patchright installed at import time for the pure-logic pieces.
"""
from __future__ import annotations

import json

import pytest

from src.discovery import patchright_spike as spike


# --------------------------------------------------------------------------- #
# classify_outcome — captcha / waf_403 / ok
# --------------------------------------------------------------------------- #

class _FakePage:
    def __init__(self, has_captcha_iframe=False):
        self._has_captcha_iframe = has_captcha_iframe

    async def evaluate(self, script):
        assert "captcha" in script or "recaptcha" in script or "turnstile" in script
        return self._has_captcha_iframe


class _FakeResp:
    def __init__(self, status):
        self.status = status


@pytest.mark.asyncio
async def test_classify_outcome_detects_captcha_iframe():
    out = await spike.classify_outcome(_FakePage(has_captcha_iframe=True), "", "", None)
    assert out == "captcha"


@pytest.mark.asyncio
async def test_classify_outcome_detects_captcha_keyword_in_body():
    out = await spike.classify_outcome(
        _FakePage(), "Please confirm you are human before continuing.", "", None
    )
    assert out == "captcha"


@pytest.mark.asyncio
async def test_classify_outcome_detects_captcha_keyword_in_title():
    out = await spike.classify_outcome(_FakePage(), "", "Attention Required! | Cloudflare", None)
    assert out == "captcha"


@pytest.mark.asyncio
async def test_classify_outcome_detects_waf_403():
    out = await spike.classify_outcome(_FakePage(), "Forbidden", "403", _FakeResp(403))
    assert out == "waf_403"


@pytest.mark.asyncio
async def test_classify_outcome_ok_when_nothing_matches():
    out = await spike.classify_outcome(_FakePage(), "Welcome to Acme Careers", "Acme Careers",
                                       _FakeResp(200))
    assert out == "ok"


@pytest.mark.asyncio
async def test_classify_outcome_ok_when_probe_raises():
    class _BoomPage:
        async def evaluate(self, script):
            raise RuntimeError("boom")

    out = await spike.classify_outcome(_BoomPage(), "normal page text", "Normal Title", None)
    assert out == "ok"  # captcha probe failure must not itself count as blocked


# --------------------------------------------------------------------------- #
# _is_timeout_error
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("exc", [
    TimeoutError("Timeout 25000ms exceeded"),
    RuntimeError("Timeout 25000ms exceeded while waiting for event \"load\""),
])
def test_is_timeout_error_detects_timeout_message(exc):
    assert spike._is_timeout_error(exc) is True


def test_is_timeout_error_false_for_unrelated_error():
    assert spike._is_timeout_error(RuntimeError("DNS lookup failed")) is False


# --------------------------------------------------------------------------- #
# test_domain_with_cdp — graceful degradation + the anti-sabotage guardrail
# --------------------------------------------------------------------------- #

class _FailingCDPPlaywright:
    """chromium.connect_over_cdp always raises — simulates no Fortress running."""

    class chromium:
        @staticmethod
        async def connect_over_cdp(url, timeout=5000):
            raise ConnectionRefusedError("nobody listening on 9222")


class _FailingCDPCtx:
    async def __aenter__(self):
        return _FailingCDPPlaywright()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_cdp_degrades_gracefully_when_container_not_running(monkeypatch):
    monkeypatch.setattr(spike, "playwright_async", lambda: _FailingCDPCtx())

    result = await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert result["outcome"] == "unavailable"
    assert result["success"] is False
    assert "cdp_connect_failed" in result["error"]


class _FakeCDPPage:
    def __init__(self):
        self.closed = False

    async def goto(self, url, timeout=None, wait_until=None):
        return _FakeResp(200)

    async def title(self):
        return "Acme Careers"

    def locator(self, sel):
        return self

    async def inner_text(self):
        return "Welcome to Acme"

    async def evaluate(self, script):
        if "captcha" in script:
            return False
        return None  # navigator.webdriver

    async def close(self):
        self.closed = True


class _FakeCDPContext:
    def __init__(self):
        self.pages_created = []

    async def new_page(self):
        p = _FakeCDPPage()
        self.pages_created.append(p)
        return p


class _FakeCDPBrowser:
    def __init__(self, existing_contexts):
        self.contexts = existing_contexts
        self.new_context_calls = []
        self.close_called = False

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return _FakeCDPContext()

    async def close(self):
        self.close_called = True


def _connectable_cdp_ctx(browser):
    class _Playwright:
        class chromium:
            @staticmethod
            async def connect_over_cdp(url, timeout=5000):
                return browser
    class _Ctx:
        async def __aenter__(self):
            return _Playwright()
        async def __aexit__(self, *a):
            return False
    return _Ctx()


@pytest.mark.asyncio
async def test_cdp_reuses_default_context_not_a_fresh_one(monkeypatch):
    """Guardrail: never new_context() when Fortress already has one — a fresh
    incognito context would drop the container's fingerprint persona."""
    default_ctx = _FakeCDPContext()
    browser = _FakeCDPBrowser(existing_contexts=[default_ctx])
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    result = await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert result["outcome"] == "ok"
    assert browser.new_context_calls == []  # never called — reused contexts[0]
    assert len(default_ctx.pages_created) == 1


@pytest.mark.asyncio
async def test_cdp_never_sets_user_agent_when_it_must_create_a_context(monkeypatch):
    """Guardrail: a fixed UA on the CDP path undoes Fortress's own fingerprint."""
    browser = _FakeCDPBrowser(existing_contexts=[])  # forces the new_context() fallback
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    result = await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert result["outcome"] == "ok"
    assert len(browser.new_context_calls) == 1
    assert "user_agent" not in browser.new_context_calls[0]


@pytest.mark.asyncio
async def test_cdp_never_closes_the_externally_owned_browser(monkeypatch):
    """Guardrail: Fortress is a long-lived container, not a process we launched."""
    browser = _FakeCDPBrowser(existing_contexts=[_FakeCDPContext()])
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert browser.close_called is False


# --------------------------------------------------------------------------- #
# run_benchmark — gate logic + JSON artifact
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_run_benchmark_gate_passes_when_fortress_wins_a_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com", "https://b.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "captcha", "success": False,
                "title": "", "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        # Fortress clears the first domain, still blocked on the second.
        if "a.example.com" in url:
            return {"engine": "fortress-cdp", "outcome": "ok", "success": True,
                    "title": "A Careers", "webdriver_val": None, "error": None}
        return {"engine": "fortress-cdp", "outcome": "captcha", "success": False,
                "title": "", "webdriver_val": None, "error": None}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark(fortress_cdp_url="http://localhost:9222")

    assert report["gate_passed"] is True
    assert report["fortress_wins"] == 1
    assert report["fortress_unavailable"] is False

    out_file = tmp_path / "aces-402-engine-benchmark-results.json"
    assert out_file.exists()
    on_disk = json.loads(out_file.read_text())
    assert on_disk["gate_passed"] is True


@pytest.mark.asyncio
async def test_run_benchmark_gate_fails_and_flags_unavailable_when_fortress_never_reachable(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "ok", "success": True,
                "title": "A", "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "unavailable", "success": False,
                "title": "", "webdriver_val": None, "error": "cdp_connect_failed"}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark()

    assert report["gate_passed"] is False
    assert report["fortress_unavailable"] is True
