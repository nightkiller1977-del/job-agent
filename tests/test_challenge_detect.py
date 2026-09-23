"""src/challenge_detect.py — HAS_VISIBLE_CHALLENGE_FRAME_JS is pure browser-
side JS; the only genuine way to verify it is to actually run it against
real DOM structures in a real (headless) browser, not mock it.

Every scenario runs in a FRESH PYTHON SUBPROCESS with real chromium — never
in-process. Reason (see tests/test_receipt_dom_truthfulness.py, the
established pattern this file follows): tests/conftest.py globally stubs
playwright.async_api in sys.modules for the whole pytest run so unit modules
can import src.sources.* without the browser stack. That stub is installed
once, process-wide, the moment conftest.py is first imported — before any
--run-live flag is even consulted (confirmed: marking these tests `live` and
passing --run-live still ran them against the mock, not real Chromium, since
the stub was already cached in sys.modules by then). A fresh subprocess never
loads conftest.py at all, so it gets the real playwright package
unconditionally — no flag, no skip, runs in every normal `pytest tests/`
invocation.

Trade-off: chromium launch ~1s per test. Acceptable to preserve isolation
and get real, deterministic, always-on coverage instead of a skipped test.

The iframe `src` values point at the reserved `.invalid` TLD (RFC 2606:
guaranteed never to resolve), so any inner navigation fails fast in the
background with no real network dependency — irrelevant anyway, since the
predicate only reads the iframe ELEMENT's own attribute/CSS/layout state,
never anything from its (unloaded) content document.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# subprocess harness — every scenario runs here, HTML in / bool out
# --------------------------------------------------------------------------- #
# The child script imports the PRODUCTION HAS_VISIBLE_CHALLENGE_FRAME_JS
# (never a copy), launches real chromium, loads the given synthetic HTML via
# set_content (no network), evaluates the predicate, and prints one JSON
# line to stdout. Any exception is captured and returned as an "error" field
# so the parent test fails with a clean message rather than a raw traceback.
_CHILD_SCRIPT = r"""
import asyncio, json, sys, traceback


async def _run(case):
    from playwright.async_api import async_playwright
    from src.challenge_detect import HAS_VISIBLE_CHALLENGE_FRAME_JS

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(case["html"])
            result = await page.evaluate(HAS_VISIBLE_CHALLENGE_FRAME_JS)
            return {"result": bool(result)}
        finally:
            await browser.close()


def main():
    try:
        case = json.loads(sys.stdin.read())
        result = asyncio.run(_run(case))
        sys.stdout.write(json.dumps(result))
    except Exception:
        sys.stdout.write(json.dumps({"error": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
"""


def _check(html: str) -> bool:
    """Run one scenario in a fresh Python subprocess. Fails the test with a
    clean message on subprocess error / timeout / non-JSON output."""
    repo_root = os.fspath(Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.run(  # noqa: S603 — args controlled, no shell
            [sys.executable, "-c", _CHILD_SCRIPT],
            input=json.dumps({"html": html}),
            capture_output=True, text=True, timeout=30, check=False,
            env={**os.environ, "PYTHONPATH": repo_root},
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"challenge_detect subprocess timed out: {exc}")

    if proc.returncode != 0 and not proc.stdout.strip():
        pytest.fail(
            f"challenge_detect subprocess exited {proc.returncode} with no stdout.\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        pytest.fail(
            f"challenge_detect subprocess produced non-JSON stdout: {exc}\n"
            f"stdout was: {proc.stdout!r}\nstderr:\n{proc.stderr}"
        )
    if "error" in parsed:
        pytest.fail(f"challenge_detect subprocess raised inside the child:\n{parsed['error']}")
    return parsed["result"]


def test_no_captcha_iframe_at_all():
    assert _check("<html><body><h1>Ordinary Careers Page</h1></body></html>") is False


def test_invisible_recaptcha_v3_anchor_is_not_a_challenge():
    """The exact false positive this fix addresses: a v3/Enterprise anchor
    iframe with size=invisible in its own src — present on ordinary pages,
    never a real blocking wall."""
    html = """<html><body>
        <h1>Ordinary Careers Page</h1>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?ar=1&k=xyz&size=invisible&co=abc"
                style="width:0; height:0; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is False


def test_visible_recaptcha_v2_checkbox_is_a_real_challenge():
    """A real v2 "I'm not a robot" checkbox — roughly 304x78, actually
    visible, no size=invisible marker. Must still be caught."""
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?ar=1&k=xyz&size=normal&co=abc"
                style="width:304px; height:78px; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is True


def test_full_page_interstitial_is_a_real_challenge():
    html = """<html><body>
        <iframe src="https://challenges.invalid/turnstile/v0/interstitial?x=1"
                style="width:100%; height:100vh; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is True


def test_tiny_badge_iframe_without_invisible_marker_is_not_a_challenge():
    """Defense in depth: even without an explicit size=invisible marker, an
    iframe too small to be an interactive challenge (a corner badge, a
    tracking pixel-sized frame) must not count."""
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/badge?k=xyz"
                style="width:28px; height:28px; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is False


def test_hidden_via_css_visibility_is_not_a_challenge():
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?size=normal"
                style="width:304px; height:78px; visibility:hidden;"></iframe>
    </body></html>"""
    assert _check(html) is False


def test_hidden_via_css_display_none_is_not_a_challenge():
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?size=normal"
                style="width:304px; height:78px; display:none;"></iframe>
    </body></html>"""
    assert _check(html) is False


def test_turnstile_iframe_still_detected_when_visible():
    html = """<html><body>
        <iframe src="https://challenges.invalid/turnstile/v0/api.js?render=explicit"
                style="width:300px; height:65px; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is True


def test_non_captcha_iframe_is_ignored_regardless_of_size():
    """Sanity check: an iframe that isn't a captcha/turnstile candidate at
    all (e.g. an embedded video) must never trip this, no matter its size."""
    html = """<html><body>
        <iframe src="https://video.invalid/embed/abc123"
                style="width:640px; height:360px; border:none;"></iframe>
    </body></html>"""
    assert _check(html) is False
