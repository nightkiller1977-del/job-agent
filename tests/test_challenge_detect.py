"""src/challenge_detect.py — HAS_VISIBLE_CHALLENGE_FRAME_JS is pure browser-
side JS; the only genuine way to verify it is to actually run it against
real DOM structures in a real (headless) browser, not mock it. These tests
use synthetic iframe elements via page.set_content() — the iframe `src`
values point at the reserved `.invalid` TLD (RFC 2606: guaranteed never to
resolve), so navigation fails fast in the background with no real network
dependency. That's fine: the predicate only reads the iframe ELEMENT's own
attribute/CSS/layout state, never anything from its (unloaded) content
document, so a failed inner navigation doesn't affect what's being tested.

Marked `live` (skipped without --run-live), matching this repo's existing
convention (see test_apply_functional.py) for tests that need a real
browser rather than the session-wide playwright.async_api stub conftest.py
installs into sys.modules for every other test. Caveat found while writing
this: that stub is installed once, process-wide, the moment conftest.py is
first imported — before any --run-live flag is even consulted — so it is
not obvious --run-live actually restores real Playwright within the same
pytest process; this file does not attempt to fix or work around that
pre-existing test-infra question. The logic these tests describe was
independently confirmed for real during development, both against
synthetic HTML (9/9 cases) and live against jobs.northropgrumman.com (the
exact false positive this fix addresses) via standalone `python -m` scripts
run outside pytest entirely — see the PR description for those results.
These tests exist so the same cases are checked automatically by anyone who
DOES have a way to run them for real, and as executable documentation of
the intended behavior either way.
"""
from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from src.challenge_detect import HAS_VISIBLE_CHALLENGE_FRAME_JS


async def _check(html: str) -> bool:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            return await page.evaluate(HAS_VISIBLE_CHALLENGE_FRAME_JS)
        finally:
            await browser.close()


@pytest.mark.live
@pytest.mark.asyncio
async def test_no_captcha_iframe_at_all():
    assert await _check("<html><body><h1>Ordinary Careers Page</h1></body></html>") is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_invisible_recaptcha_v3_anchor_is_not_a_challenge():
    """The exact false positive this fix addresses: a v3/Enterprise anchor
    iframe with size=invisible in its own src — present on ordinary pages,
    never a real blocking wall."""
    html = """<html><body>
        <h1>Ordinary Careers Page</h1>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?ar=1&k=xyz&size=invisible&co=abc"
                style="width:0; height:0; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_visible_recaptcha_v2_checkbox_is_a_real_challenge():
    """A real v2 "I'm not a robot" checkbox — roughly 304x78, actually
    visible, no size=invisible marker. Must still be caught."""
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?ar=1&k=xyz&size=normal&co=abc"
                style="width:304px; height:78px; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is True


@pytest.mark.live
@pytest.mark.asyncio
async def test_full_page_interstitial_is_a_real_challenge():
    html = """<html><body>
        <iframe src="https://challenges.invalid/turnstile/v0/interstitial?x=1"
                style="width:100%; height:100vh; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is True


@pytest.mark.live
@pytest.mark.asyncio
async def test_tiny_badge_iframe_without_invisible_marker_is_not_a_challenge():
    """Defense in depth: even without an explicit size=invisible marker, an
    iframe too small to be an interactive challenge (a corner badge, a
    tracking pixel-sized frame) must not count."""
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/badge?k=xyz"
                style="width:28px; height:28px; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_hidden_via_css_visibility_is_not_a_challenge():
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?size=normal"
                style="width:304px; height:78px; visibility:hidden;"></iframe>
    </body></html>"""
    assert await _check(html) is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_hidden_via_css_display_none_is_not_a_challenge():
    html = """<html><body>
        <iframe src="https://recaptcha.invalid/recaptcha/api2/anchor?size=normal"
                style="width:304px; height:78px; display:none;"></iframe>
    </body></html>"""
    assert await _check(html) is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_turnstile_iframe_still_detected_when_visible():
    html = """<html><body>
        <iframe src="https://challenges.invalid/turnstile/v0/api.js?render=explicit"
                style="width:300px; height:65px; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is True


@pytest.mark.live
@pytest.mark.asyncio
async def test_non_captcha_iframe_is_ignored_regardless_of_size():
    """Sanity check: an iframe that isn't a captcha/turnstile candidate at
    all (e.g. an embedded video) must never trip this, no matter its size."""
    html = """<html><body>
        <iframe src="https://video.invalid/embed/abc123"
                style="width:640px; height:360px; border:none;"></iframe>
    </body></html>"""
    assert await _check(html) is False
