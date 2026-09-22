"""ACES-402 — unit tests for the engine-benchmark spike (src/discovery/patchright_spike.py).

No live network / no real browser: fakes stand in for Playwright's async API
so these run in CI without a Fortress container, real domains, or even
playwright/patchright installed at import time for the pure-logic pieces.
"""
from __future__ import annotations

import asyncio
import json
import time

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
async def test_classify_outcome_403_wins_over_overlapping_blocked_text():
    """Copilot review, PR #140: a real WAF 403 commonly says "Access Denied"
    or mentions Cloudflare in its own body — exactly the words the captcha
    text heuristic matches. Checking text before status meant every real 403
    got mislabeled "captcha" and waf_403 never fired. Status must win."""
    out = await spike.classify_outcome(
        _FakePage(), "Access Denied - Cloudflare", "403 Forbidden", _FakeResp(403)
    )
    assert out == "waf_403"


@pytest.mark.asyncio
async def test_classify_outcome_captcha_iframe_still_wins_over_403():
    # An actual captcha iframe is unambiguous — still highest precedence
    # even on a 403 response.
    out = await spike.classify_outcome(
        _FakePage(has_captcha_iframe=True), "", "", _FakeResp(403)
    )
    assert out == "captcha"


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
# test_domain_with_engine — the Playwright/Patchright baseline leg
# --------------------------------------------------------------------------- #

class _FakeEnginePage:
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


class _FakeEngineContext:
    async def new_page(self):
        return _FakeEnginePage()


class _BoomOnCloseBrowser:
    async def new_context(self, **kwargs):
        return _FakeEngineContext()

    async def close(self):
        raise RuntimeError("browser process crashed during shutdown")


class _FakeEngineChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, headless=True):
        return self._browser


class _FakeEnginePlaywright:
    def __init__(self, browser):
        self.chromium = _FakeEngineChromium(browser)


@pytest.mark.asyncio
async def test_engine_leg_resets_success_when_cleanup_fails_after_a_good_probe():
    """Copilot review, PR #140: browser.close() raising AFTER a successful
    probe must not leave success=True alongside outcome="error"/"timeout" —
    run_benchmark()'s win/loss comparison only checks the success flag."""
    p = _FakeEnginePlaywright(_BoomOnCloseBrowser())

    result = await spike.test_domain_with_engine(p, "playwright", "https://example.com")

    assert result["outcome"] in ("error", "timeout")
    assert result["success"] is False


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


@pytest.mark.asyncio
async def test_cdp_connect_failure_redacts_credentials_from_the_error_message(monkeypatch):
    """Codex review, PR #140: connect_over_cdp's own exception can echo the
    full endpoint it tried to reach — including any userinfo/token in
    FORTRESS_CDP_URL. _redact_cdp_url() alone doesn't protect this string."""
    secret_cdp_url = "http://user:supersecret@remote-fortress:9222"

    class _LeakyFailingPlaywright:
        class chromium:
            @staticmethod
            async def connect_over_cdp(url, timeout=5000):
                raise ConnectionRefusedError(f"Cannot connect to {url}")

    class _LeakyCtx:
        async def __aenter__(self):
            return _LeakyFailingPlaywright()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(spike, "playwright_async", lambda: _LeakyCtx())

    result = await spike.test_domain_with_cdp(secret_cdp_url, "https://example.com")

    assert "supersecret" not in result["error"]


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
        self.closed = False

    async def new_page(self):
        p = _FakeCDPPage()
        self.pages_created.append(p)
        return p

    async def close(self):
        self.closed = True


class _FakeCDPBrowser:
    def __init__(self, existing_contexts):
        self.contexts = existing_contexts
        self.new_context_calls = []
        self.created_contexts = []
        self.close_called = False

    async def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        ctx = _FakeCDPContext()
        self.created_contexts.append(ctx)
        return ctx

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
    assert default_ctx.closed is False  # never ours to close


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
async def test_cdp_closes_a_context_it_had_to_create_but_not_a_reused_one(monkeypatch):
    """Copilot review (PR #140): this function reconnects once per domain, so
    a harness-created fallback context left open on every run accumulates in
    a long-lived Fortress container. It must be closed — but only when WE
    created it; a reused default context (see the sibling test above) must
    be left alone."""
    browser = _FakeCDPBrowser(existing_contexts=[])  # forces the new_context() fallback

    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert len(browser.created_contexts) == 1
    assert browser.created_contexts[0].closed is True


@pytest.mark.asyncio
async def test_cdp_never_closes_the_externally_owned_browser(monkeypatch):
    """Guardrail: Fortress is a long-lived container, not a process we launched."""
    browser = _FakeCDPBrowser(existing_contexts=[_FakeCDPContext()])
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert browser.close_called is False


@pytest.mark.asyncio
async def test_cdp_post_connect_failure_is_not_misclassified_as_unavailable(monkeypatch):
    """Copilot review, PR #140: a failure AFTER a successful CDP connect
    (context/page creation, cleanup) is a real engine error, not "the
    container isn't running" — result["outcome"] must not silently stay at
    its "unavailable" default, or run_benchmark() hides a genuine crash
    behind the same label as a container that was never up."""
    class _BoomOnNewPageContext(_FakeCDPContext):
        async def new_page(self):
            raise RuntimeError("Target page, context or browser has been closed")

    browser = _FakeCDPBrowser(existing_contexts=[_BoomOnNewPageContext()])
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    result = await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert result["outcome"] != "unavailable"
    assert result["outcome"] == "error"


@pytest.mark.asyncio
async def test_cdp_resets_success_when_created_context_cleanup_fails(monkeypatch):
    """Copilot review, PR #140: same success/outcome inconsistency as the
    engine-leg test above, but for the CDP leg's own created-context cleanup
    (context.close() in the finally, only reached when this function had to
    create a fallback context — see test_cdp_closes_a_context_it_had_to_create...)."""
    class _BoomOnCloseContext(_FakeCDPContext):
        async def close(self):
            raise RuntimeError("context already closed")

    browser = _FakeCDPBrowser(existing_contexts=[])  # forces the new_context() fallback

    async def _new_context(**kwargs):
        browser.new_context_calls.append(kwargs)
        ctx = _BoomOnCloseContext()
        browser.created_contexts.append(ctx)
        return ctx

    browser.new_context = _new_context
    monkeypatch.setattr(spike, "playwright_async", lambda: _connectable_cdp_ctx(browser))

    result = await spike.test_domain_with_cdp("http://localhost:9222", "https://example.com")

    assert result["outcome"] in ("error", "timeout")
    assert result["success"] is False


# --------------------------------------------------------------------------- #
# run_benchmark — gate logic + JSON artifact
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_run_benchmark_gate_passes_when_fortress_wins_a_domain(monkeypatch, tmp_path):
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com", "https://b.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "captcha", "success": False,
                "title": "", "webdriver_val": None, "body_chars": 0, "error": None}

    async def _fake_cdp(cdp_url, url):
        # Fortress clears the first domain, still blocked on the second.
        if "a.example.com" in url:
            return {"engine": "fortress-cdp", "outcome": "ok", "success": True,
                    "title": "A Careers", "webdriver_val": None, "body_chars": 8000, "error": None}
        return {"engine": "fortress-cdp", "outcome": "captcha", "success": False,
                "title": "", "webdriver_val": None, "body_chars": 0, "error": None}

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
                "title": "A", "webdriver_val": None, "body_chars": 8000, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "unavailable", "success": False,
                "title": "", "webdriver_val": None, "error": "cdp_connect_failed"}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark()

    assert report["gate_passed"] is False
    assert report["fortress_unavailable"] is True


# --------------------------------------------------------------------------- #
# MIN_BODY_CHARS / _loaded — a load must show content, not just absence of error
# --------------------------------------------------------------------------- #

def test_loaded_rejects_ok_outcome_with_no_content():
    """The observed Fortress failure mode: outcome "ok" on a cookie banner.

    On jobs.northropgrumman.com Fortress-CDP returned outcome "ok" with 238
    chars of body (a consent banner) while Patchright rendered ~8600 chars of
    real navigation. `success` alone scored that a tie, hiding the difference.
    """
    shell = {"outcome": "ok", "success": True, "body_chars": 238}
    assert spike._loaded(shell) is False


def test_loaded_accepts_ok_outcome_with_real_content():
    page = {"outcome": "ok", "success": True, "body_chars": 8595}
    assert spike._loaded(page) is True


def test_loaded_is_exactly_the_content_floor_boundary():
    assert spike._loaded({"success": True, "body_chars": spike.MIN_BODY_CHARS}) is True
    assert spike._loaded({"success": True, "body_chars": spike.MIN_BODY_CHARS - 1}) is False


def test_loaded_rejects_blocked_or_unavailable_results():
    assert spike._loaded({"outcome": "captcha", "success": False, "body_chars": 9000}) is False
    assert spike._loaded({"outcome": "unavailable", "success": False, "body_chars": None}) is False


def test_loaded_tolerates_missing_body_chars_key():
    """Older result dicts and any hand-built fake must not raise."""
    assert spike._loaded({"outcome": "ok", "success": True}) is False


def test_fmt_reports_empty_when_ok_but_content_starved():
    assert spike._fmt({"outcome": "ok", "success": True, "title": "x", "body_chars": 238}) == "EMPTY (238 chars)"


def test_fmt_reports_measured_chars_for_a_real_load():
    assert spike._fmt({"outcome": "ok", "success": True, "title": "x", "body_chars": 8595}) == "OK (8595 chars)"


def test_fmt_does_not_claim_ok_for_an_unknown_char_count():
    assert spike._fmt({"outcome": "ok", "success": True, "title": "x"}) == "OK (? chars)"


@pytest.mark.asyncio
async def test_run_benchmark_does_not_tie_a_content_starved_fortress_run(monkeypatch, tmp_path):
    """Both engines report outcome "ok"; only one rendered the page.

    Before the content floor this was counted a tie. The gate must reflect the
    measured difference instead of scoring it neutral.
    """
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "ok", "success": True,
                "title": "Real", "body_chars": 8595, "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "ok", "success": True,
                "title": "Banner", "body_chars": 238, "webdriver_val": None, "error": None}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark(fortress_cdp_url="http://localhost:9222")

    assert report["gate_passed"] is False
    assert report["fortress_wins"] == 0
    assert report["patchright_wins"] == 1
    assert report["ties"] == 0


@pytest.mark.asyncio
async def test_run_benchmark_still_ties_when_both_render_comparable_pages(monkeypatch, tmp_path):
    """The proportional rule must not turn a genuine tie into a loss."""
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "ok", "success": True,
                "title": "Real", "body_chars": 8595, "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "ok", "success": True,
                "title": "Real", "body_chars": 8600, "webdriver_val": None, "error": None}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark(fortress_cdp_url="http://localhost:9222")

    assert report["ties"] == 1
    assert report["fortress_wins"] == 0
    assert report["patchright_wins"] == 0


# --------------------------------------------------------------------------- #
# _body_text — bounded read must degrade, never raise
# --------------------------------------------------------------------------- #

class _RaisingBody:
    async def inner_text(self, timeout=None):
        raise RuntimeError("no stable body element")


class _OkBody:
    async def inner_text(self, timeout=None):
        return "real page content"


class _FakeLocatorPage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, selector):
        return self._locator


@pytest.mark.asyncio
async def test_body_text_returns_content_when_available():
    page = _FakeLocatorPage(_OkBody())
    assert await spike._body_text(page) == "real page content"


@pytest.mark.asyncio
async def test_body_text_degrades_to_empty_string_instead_of_raising():
    """An unbounded inner_text() raises on a gated page; that used to surface
    as outcome "error" and made one engine look worse for a harness reason."""
    page = _FakeLocatorPage(_RaisingBody())
    assert await spike._body_text(page) == ""


@pytest.mark.asyncio
async def test_body_text_passes_a_bounded_timeout():
    seen = {}

    class _Recording:
        async def inner_text(self, timeout=None):
            seen["timeout"] = timeout
            return "x"

    page = _FakeLocatorPage(_Recording())
    await spike._body_text(page, timeout_ms=1234)
    assert seen["timeout"] == 1234


# --------------------------------------------------------------------------- #
# _body_text fallback deadline + gate ratio guards (Copilot review, PR #142)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_body_text_fallback_is_time_bounded(monkeypatch):
    """Copilot review, PR #142: page.evaluate() takes no timeout, so an
    unresponsive renderer hung the whole benchmark via the fallback path."""
    hung = {"cancelled": False}

    class _HangingBody:
        async def inner_text(self, timeout=None):
            raise RuntimeError("no stable body")

    class _HangingEvaluatePage:
        def locator(self, selector):
            return _HangingBody()

        async def evaluate(self, script):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                hung["cancelled"] = True
                raise
            return "unreachable"

    start = time.monotonic()
    result = await spike._body_text(_HangingEvaluatePage(), timeout_ms=200)
    elapsed = time.monotonic() - start

    assert result == ""
    assert elapsed < 5, f"fallback was not bounded (took {elapsed:.1f}s)"
    assert hung["cancelled"] is True


@pytest.mark.asyncio
async def test_run_benchmark_does_not_award_a_win_between_two_blocked_pages(monkeypatch, tmp_path):
    """Copilot review, PR #142: the ratio branch used to run even when neither
    engine loaded, so challenge-page text volume could score a false win."""
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "captcha", "success": False,
                "title": "", "body_chars": 1000, "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "captcha", "success": False,
                "title": "", "body_chars": 100, "webdriver_val": None, "error": None}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark(fortress_cdp_url="http://localhost:9222")

    assert report["fortress_wins"] == 0
    assert report["patchright_wins"] == 0
    assert report["ties"] == 1


@pytest.mark.asyncio
async def test_run_benchmark_treats_an_exact_double_as_a_loss(monkeypatch, tmp_path):
    """Copilot review, PR #142: an exact 2x difference was scored a tie even
    though the stated rule is a 2x margin."""
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "ok", "success": True,
                "title": "Real", "body_chars": 1000, "webdriver_val": None, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "ok", "success": True,
                "title": "Half", "body_chars": 500, "webdriver_val": None, "error": None}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    report = await spike.run_benchmark(fortress_cdp_url="http://localhost:9222")

    assert report["patchright_wins"] == 1
    assert report["ties"] == 0


def test_redact_cdp_url_strips_userinfo():
    assert spike._redact_cdp_url("http://user:secrettoken@remote-fortress:9222") == \
        "http://remote-fortress:9222"


def test_redact_cdp_url_strips_query_string_tokens():
    assert spike._redact_cdp_url("http://remote-fortress:9222?token=abc123") == \
        "http://remote-fortress:9222"


def test_redact_cdp_url_keeps_scheme_host_port_for_plain_localhost():
    assert spike._redact_cdp_url("http://localhost:9222") == "http://localhost:9222"


def test_redact_cdp_url_never_raises_on_garbage_input():
    assert spike._redact_cdp_url("not a url at all") != None  # noqa: E711 - just must not raise


# --------------------------------------------------------------------------- #
# _redact_error — scrub credentials out of an exception message too, not
# just the top-level fortress_cdp_url field (Codex review, PR #140)
# --------------------------------------------------------------------------- #

def test_redact_error_strips_the_exact_cdp_url_when_echoed_verbatim():
    cdp_url = "http://user:supersecret@remote-fortress:9222"
    exc = ConnectionRefusedError(f"Cannot connect to {cdp_url}")
    out = spike._redact_error(exc, cdp_url)
    assert "supersecret" not in out
    assert "remote-fortress:9222" in out  # host still useful for debugging


def test_redact_error_strips_userinfo_even_in_a_differently_formatted_message():
    exc = RuntimeError("connection failed: scheme://admin:hunter2@somehost/path")
    out = spike._redact_error(exc, "http://different-url:9222")
    assert "hunter2" not in out
    assert "admin" not in out


def test_redact_error_leaves_a_plain_message_unchanged():
    exc = RuntimeError("plain timeout, nothing sensitive here")
    assert spike._redact_error(exc, "http://localhost:9222") == str(exc)


@pytest.mark.asyncio
async def test_run_benchmark_never_persists_the_raw_cdp_url(monkeypatch, tmp_path):
    monkeypatch.setattr(spike, "TEST_DOMAINS", ["https://a.example.com"])
    monkeypatch.setattr(spike, "RESULTS_DIR", tmp_path)

    async def _fake_engine(playwright_engine, engine_name, url):
        return {"engine": engine_name, "outcome": "ok", "success": True,
                "title": "A", "webdriver_val": None, "body_chars": 8000, "error": None}

    async def _fake_cdp(cdp_url, url):
        return {"engine": "fortress-cdp", "outcome": "unavailable", "success": False,
                "title": "", "webdriver_val": None, "error": "cdp_connect_failed"}

    monkeypatch.setattr(spike, "test_domain_with_engine", _fake_engine)
    monkeypatch.setattr(spike, "test_domain_with_cdp", _fake_cdp)

    secret_url = "http://user:supersecret@remote-fortress:9222"
    report = await spike.run_benchmark(fortress_cdp_url=secret_url)

    assert "supersecret" not in json.dumps(report)
    out_file = tmp_path / "aces-402-engine-benchmark-results.json"
    assert "supersecret" not in out_file.read_text()
