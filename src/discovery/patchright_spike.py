"""ACES-402 — 3-way engine benchmark: Playwright vs Patchright vs Fortress-CDP.

Spike / regression harness, not production code. Extends the original
Playwright-vs-Patchright benchmark with a third leg (a Fortress or
CloakBrowser-style kernel-level stealth Chromium, reached over CDP) and a
finer-grained failure taxonomy, so the question "does Fortress actually beat
Patchright on our real failing domains" can be answered from repeatable
evidence instead of vendor claims.

Gate this spike exists to answer: does the Fortress/CDP leg beat Patchright
on at least one of TEST_DOMAINS? See ACES-405, which only ships if this gate
passes.

Guardrail (Fortress's own docs, and ACES-402's own acceptance criteria):
JS-layer stealth patches (playwright-stealth) and a fixed User-Agent both
undo Fortress's kernel-level fingerprint patches — an engine "sabotaged on
day one" if either leaks into its context. So the CDP leg here:
  - never sets `user_agent=` on a context (Fortress owns that persona);
  - never calls `browser.new_context()` when the container already has a
    default context (`browser.contexts[0]`) — reuses that instead of a
    fresh incognito one, matching the connect_over_cdp guidance in
    Playwright's own docs. Only creates (and then closes) a fallback
    context on the rare occasion Fortress has none yet;
  - never calls `browser.close()` — a CDP-connected browser is a
    long-lived, externally-owned container, not a process this script
    launched (closing it would kill Fortress for anyone else using it).
The Playwright/Patchright legs are unaffected by any of this — they keep
their existing behavior (including the pre-existing fixed UA) unchanged.

Read-only tooling + docs artifact. No production path touched, no flag
flipped: this script never imports from src.sources or src.orchestrator,
and never writes to state/jobs.db.
"""
import asyncio
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

# Import both side-by-side
from playwright.async_api import async_playwright as playwright_async
from patchright.async_api import async_playwright as patchright_async

TEST_DOMAINS = [
    "https://jobs.northropgrumman.com",
    "https://jobs.jacksonhealth.org",
    "https://servpro.hrmdirect.com",
    "https://www.theapplicantmanager.com",
    "https://boards.greenhouse.io",
    "https://jobs.lever.co"
]

# Default Fortress/CDP endpoint (docker run -p 9222:9222 tilion/fortress:latest).
# Override with the FORTRESS_CDP_URL env var — never hardcode a different one.
DEFAULT_FORTRESS_CDP_URL = "http://localhost:9222"

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks"

# --------------------------------------------------------------------------- #
# Shared failure classification — reuses the same signals as
# GenericAtsAdapter._detect_blocker (src/sources/adapters/generic.py:225-239):
# a captcha/challenge iframe, or the same "checking your browser" style
# keyword set. Extended here with an HTTP-status check for WAF-style 403s and
# an explicit timeout category, splitting the old binary blocked/error into
# three named outcomes the benchmark table can report separately.
# --------------------------------------------------------------------------- #

_CAPTCHA_IFRAME_JS = (
    "() => !!document.querySelector("
    "'iframe[src*=\"captcha\" i], iframe[src*=\"recaptcha\" i], iframe[src*=\"turnstile\" i]'"
    ")"
)
_BLOCKED_TEXT_RE = re.compile(
    r"attention required|access denied|security check|"
    r"please confirm you are human|checking your browser|"
    r"verify you are human|verify your connection|cloudflare",
    re.IGNORECASE,
)

OUTCOMES = ("ok", "captcha", "waf_403", "timeout", "error")


async def classify_outcome(page, body_text: str, title: str, resp: Any) -> str:
    """Return one of OUTCOMES for a page that loaded without raising.

    Order matters (Copilot review, PR #140): a WAF 403 page commonly says
    "Access Denied" or mentions Cloudflare in its own body text — the exact
    words _BLOCKED_TEXT_RE matches for a captcha/JS-challenge page. Checking
    text before status meant every real 403 got mislabeled "captcha" and the
    waf_403 category never fired. Precedence now: an actual captcha iframe
    (unambiguous) > HTTP 403 (unambiguous WAF signal) > blocked-text heuristic
    (catches a text-only JS challenge that returns 200, e.g. some Cloudflare
    "checking your browser" interstitials)."""
    try:
        has_captcha_iframe = await page.evaluate(_CAPTCHA_IFRAME_JS)
    except Exception:
        has_captcha_iframe = False
    if has_captcha_iframe:
        return "captcha"
    status = getattr(resp, "status", None)
    if status == 403:
        return "waf_403"
    if _BLOCKED_TEXT_RE.search(f"{body_text} {title}"):
        return "captcha"
    return "ok"


def _is_timeout_error(exc: Exception) -> bool:
    return "timeout" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


def _redact_cdp_url(url: str) -> str:
    """scheme://host:port only — never userinfo, path, query, or fragment.

    Codex review (PR #140): a remote/authenticated Fortress endpoint can put
    credentials (userinfo, or a bearer token in the query string) in
    FORTRESS_CDP_URL. The full value is needed to actually connect, but the
    persisted docs/benchmarks/ JSON artifact is meant to be a diffable,
    shareable regression record — it must never carry that secret. Use the
    full url to connect; use this redacted form only in anything written to
    disk."""
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return f"{parsed.scheme}://{netloc}" if parsed.scheme else netloc
    except Exception:
        return "<redacted>"


def _redact_error(exc: Exception, cdp_url: str) -> str:
    """str(exc) with any trace of cdp_url's credentials scrubbed. A connect/
    transport exception can legitimately embed the full endpoint it was
    trying to reach — redact the exact URL if present, then defensively
    strip any scheme://user:pass@ pattern that might appear in a differently
    -formatted message (Copilot review, PR #140)."""
    msg = str(exc)
    if cdp_url and cdp_url in msg:
        msg = msg.replace(cdp_url, _redact_cdp_url(cdp_url))
    return re.sub(r'://[^/@\s]+@', '://', msg)


async def test_domain_with_engine(playwright_engine, engine_name: str, url: str) -> Dict[str, Any]:
    """Navigates to a URL with the given browser engine and returns detection and loading stats."""
    result: Dict[str, Any] = {
        "engine": engine_name,
        "outcome": "error",
        "success": False,
        "webdriver_val": None,
        "title": "",
        "error": None,
    }

    try:
        browser = await playwright_engine.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            try:
                resp = await page.goto(url, timeout=25000, wait_until="domcontentloaded")
                await asyncio.sleep(2)  # Let dynamic JS run

                title = await page.title()
                body_text = await page.locator("body").inner_text()
                webdriver_val = await page.evaluate("() => navigator.webdriver")

                result["title"] = title
                result["webdriver_val"] = webdriver_val
                result["outcome"] = await classify_outcome(page, body_text, title, resp)
                result["success"] = result["outcome"] == "ok"
            except Exception as e:
                result["outcome"] = "timeout" if _is_timeout_error(e) else "error"
                result["error"] = str(e)
        finally:
            await browser.close()
    except Exception as e:
        # Copilot review, PR #140: a browser.close() failure AFTER a
        # successful probe must not leave success=True alongside
        # outcome="error" — run_benchmark()'s win/loss comparison only
        # checks success, so an inconsistent pair would still count a
        # cleanup crash as a valid result.
        result["outcome"] = "timeout" if _is_timeout_error(e) else "error"
        result["success"] = False
        result["error"] = str(e)

    return result


async def test_domain_with_cdp(cdp_url: str, url: str) -> Dict[str, Any]:
    """Connect to a Fortress/CDP endpoint and test one domain.

    Never raises: if the container isn't running (or connect_over_cdp times
    out/refuses), this degrades to {"outcome": "unavailable"} rather than
    crashing the benchmark or blocking the other two engines — this is the
    "keep it dependency-light... CI never breaks" requirement from ACES-402.
    """
    result: Dict[str, Any] = {
        "engine": "fortress-cdp",
        "outcome": "unavailable",
        "success": False,
        "webdriver_val": None,
        "title": "",
        "error": None,
    }
    connected = False

    try:
        async with playwright_async() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url, timeout=5000)
            except Exception as e:
                # Copilot review, PR #140: a connect failure's own exception
                # message can echo the endpoint (host, and any userinfo/token
                # in it) — _redact_cdp_url() only protects the top-level
                # fortress_cdp_url field, not this string. Scrub it too.
                result["error"] = f"cdp_connect_failed: {type(e).__name__}: {_redact_error(e, cdp_url)}"
                return result
            connected = True

            # Guardrail: reuse Fortress's own default context — never a fresh
            # new_context(), and never a user_agent override here. Only when
            # the container has no default context yet do we create one —
            # and then WE own it: it must be closed (Copilot review, PR #140:
            # this function reconnects once per domain, so leaving a
            # harness-created context open on every run accumulates them in
            # a long-lived container). The pre-existing default context, and
            # the browser/container itself, are never ours to close.
            created_context = not browser.contexts
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            try:
                page = await context.new_page()
                try:
                    resp = await page.goto(url, timeout=25000, wait_until="domcontentloaded")
                    await asyncio.sleep(2)

                    title = await page.title()
                    body_text = await page.locator("body").inner_text()
                    webdriver_val = await page.evaluate("() => navigator.webdriver")

                    result["title"] = title
                    result["webdriver_val"] = webdriver_val
                    result["outcome"] = await classify_outcome(page, body_text, title, resp)
                    result["success"] = result["outcome"] == "ok"
                except Exception as e:
                    result["outcome"] = "timeout" if _is_timeout_error(e) else "error"
                    result["error"] = _redact_error(e, cdp_url)
                finally:
                    await page.close()
            finally:
                if created_context:
                    await context.close()
                # Deliberately NOT browser.close() — Fortress is a long-lived,
                # externally-owned container, not a process we launched.
    except Exception as e:
        # Copilot review, PR #140: this branch also catches failures AFTER a
        # successful CDP connect (context/page creation, cleanup) — those are
        # real engine errors, not "the container isn't running". Leaving
        # outcome at its "unavailable" default made run_benchmark() report a
        # genuine crash as a missing container and silently skip it from the
        # comparison. Only stays "unavailable" when we never connected at all.
        if connected:
            result["outcome"] = "timeout" if _is_timeout_error(e) else "error"
            result["success"] = False
        result["error"] = _redact_error(e, cdp_url)

    return result


def _fmt(res: Dict[str, Any]) -> str:
    if res["outcome"] == "unavailable":
        return "N/A (container not running)"
    if res["outcome"] == "timeout":
        return "TIMEOUT"
    if res["outcome"] == "error":
        return f"ERR ({(res['error'] or '')[:30]})"
    if res["outcome"] == "captcha":
        return "BLOCKED (captcha)"
    if res["outcome"] == "waf_403":
        return "BLOCKED (waf_403)"
    return f"OK ({res['title'][:20]})"


async def run_benchmark(fortress_cdp_url: Optional[str] = None) -> Dict[str, Any]:
    cdp_url = fortress_cdp_url or os.environ.get("FORTRESS_CDP_URL", DEFAULT_FORTRESS_CDP_URL)

    print("================================================================")
    print("     PLAYWRIGHT vs PATCHRIGHT vs FORTRESS-CDP ENGINE BENCHMARK  ")
    print("================================================================")

    results: Dict[str, Dict[str, Any]] = {}

    for url in TEST_DOMAINS:
        domain = urllib.parse.urlparse(url).netloc
        print(f"\nEvaluating domain: {domain}...")

        print(" -> Testing with Standard Playwright...")
        async with playwright_async() as p_std:
            std_res = await test_domain_with_engine(p_std, "playwright", url)

        print(" -> Testing with Patchright...")
        async with patchright_async() as p_patch:
            patch_res = await test_domain_with_engine(p_patch, "patchright", url)

        print(f" -> Testing with Fortress-CDP ({_redact_cdp_url(cdp_url)})...")
        fortress_res = await test_domain_with_cdp(cdp_url, url)

        results[domain] = {
            "playwright": std_res,
            "patchright": patch_res,
            "fortress_cdp": fortress_res,
        }

    # Render final report table
    print("\n\n================================================================")
    print("                      BENCHMARK REPORT                          ")
    print("================================================================")
    print(f"{'Domain':<28} | {'Playwright':<22} | {'Patchright':<22} | {'Fortress-CDP':<22}")
    print("-" * 100)

    fortress_wins = 0
    patchright_wins = 0
    other_ties = 0
    fortress_unavailable = False

    for domain, res in results.items():
        std, pat, fort = res["playwright"], res["patchright"], res["fortress_cdp"]
        print(f"{domain:<28} | {_fmt(std):<22} | {_fmt(pat):<22} | {_fmt(fort):<22}")

        if fort["outcome"] == "unavailable":
            fortress_unavailable = True
            continue

        pat_ok, fort_ok = pat["success"], fort["success"]
        if fort_ok and not pat_ok:
            fortress_wins += 1
        elif pat_ok and not fort_ok:
            patchright_wins += 1
        else:
            other_ties += 1

    print("-" * 100)
    if fortress_unavailable:
        print("Fortress-CDP container was not reachable for at least one domain "
              f"(tried {_redact_cdp_url(cdp_url)}) — gate cannot be fully evaluated this run.")
    print(f"Fortress-CDP wins over Patchright: {fortress_wins} | "
          f"Patchright wins over Fortress-CDP: {patchright_wins} | Ties: {other_ties}")
    gate_passed = fortress_wins > 0
    print(f"ACES-402 gate (does Fortress beat Patchright on >=1 domain?): "
          f"{'PASS' if gate_passed else 'FAIL'}")
    print("================================================================")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fortress_cdp_url": _redact_cdp_url(cdp_url),
        "fortress_unavailable": fortress_unavailable,
        "gate_passed": gate_passed,
        "fortress_wins": fortress_wins,
        "patchright_wins": patchright_wins,
        "ties": other_ties,
        "domains": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "aces-402-engine-benchmark-results.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out_path}")

    return report


if __name__ == "__main__":
    asyncio.run(run_benchmark())
