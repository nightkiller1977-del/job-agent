"""DOM-aware regression for src/sources/adapters/receipt.py::verify_receipt.

Uses real Playwright chromium + page.set_content() to run the PRODUCTION
verifier against synthetic HTML. Network is blocked via route interception —
no employer sites contacted, no credentials required.

The DOM layer catches cases the Node-only harness cannot:
  - stale/hidden confirmation panels that must not be counted as receipts
  - form-gone-without-acceptance (must remain unverified, not upgraded)
  - delayed acceptance across multiple polls with real DOM mutation

If chromium is not installed locally, `python -m playwright install chromium`
provisions it. In CI the `Install Playwright chromium` step handles it —
a failure here means the CI workflow is out of sync with these tests.

NOTE: tests/conftest.py session-stubs `playwright.async_api` with an AsyncMock
so unit tests can import src.sources.* without the real package. This file
DELIBERATELY unstubs those modules before importing them so it gets the real
chromium runtime — the whole point of this suite is that the mock cannot
prove DOM-level behavior. Do not remove the sys.modules pops below.
"""
from __future__ import annotations

import sys as _sys

# Unstub playwright before importing. conftest.py uses sys.modules.setdefault,
# so removing the stubs and re-importing yields the real installed package.
for _mod in ("playwright", "playwright.async_api", "playwright.async_api._generated"):
    _sys.modules.pop(_mod, None)

import asyncio  # noqa: E402
import importlib  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import pytest  # noqa: E402

from src.sources.adapters.receipt import verify_receipt  # noqa: E402


# --------------------------------------------------------------------------- #
# CI-config guard
# --------------------------------------------------------------------------- #
def test_playwright_chromium_is_installed_for_dom_receipt_harness():
    """Baseline: playwright must be importable. Chromium binary is checked
    live by every test below when it launches the browser.
    """
    try:
        importlib.import_module("playwright.async_api")
    except ImportError as exc:  # pragma: no cover — provisioning failure
        pytest.fail(f"playwright not importable: {exc}. See requirements.txt.")


# --------------------------------------------------------------------------- #
# fixture — real chromium page with network blocked, synthetic HTML only
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _synthetic_page(initial_html: str, base_url: str = "https://form.local/apply"):
    """Yield a live Playwright page with network blocked and initial_html loaded.

    `base_url` sets what `page.url` returns — the receipt URL check reads it,
    so tests that pin the URL (unchanged-SPA vs confirmation redirect) work.

    Mechanics: a route interceptor fulfills `base_url` with `initial_html` and
    aborts everything else — no real network reaches the internet, and the
    page's origin is set to `base_url` (unlike a data: URL, whose origin is
    'null' and blocks history.replaceState to a real origin).
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()

            async def _handle(route):
                if route.request.url.rstrip("/") == base_url.rstrip("/"):
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=initial_html,
                    )
                else:
                    # Any other request means the fixture leaked — abort loudly.
                    await route.abort()

            await context.route("**/*", _handle)
            page = await context.new_page()
            await page.goto(base_url)
            yield page
        finally:
            await browser.close()


# --------------------------------------------------------------------------- #
# 1. Fresh acceptance panel replacing form → verified
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fresh_acceptance_panel_verifies():
    """After successful submit, the page shows a fresh acceptance panel.
    verify_receipt MUST return True and cite the text signal.
    """
    html = """
    <html><body>
      <main>
        <h1>Application submitted.</h1>
        <p>Thank you for applying to Acme. We will be in touch.</p>
      </main>
    </body></html>
    """
    async with _synthetic_page(html) as page:
        ok, sig = await verify_receipt(page)
        assert ok, f"fresh acceptance panel should verify; got sig={sig!r}"
        assert sig.startswith("t:"), (
            f"expected text-path signal, got {sig!r} — the URL regex should not "
            f"be firing on the base URL 'https://form.local/apply'."
        )


# --------------------------------------------------------------------------- #
# 2. Form-gone-without-acceptance → must NOT verify
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_form_gone_without_acceptance_does_not_verify():
    """The form disappears (e.g. SPA cleared the DOM), but no acceptance copy
    appears. This must REMAIN unverified — a missing form is not a receipt.
    Mirrors the existing ashby._gated_submit form-removed convention.
    """
    html = """
    <html><body>
      <div id="app"></div>
      <!-- form intentionally absent; no confirmation copy either -->
    </body></html>
    """
    async with _synthetic_page(html) as page:
        ok, sig = await verify_receipt(page)
        assert ok is False and sig == "", (
            f"form-gone-without-acceptance must not verify; got ok={ok} sig={sig!r}"
        )


# --------------------------------------------------------------------------- #
# 3. Stale/hidden panel on a form page → must NOT verify
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stale_hidden_acceptance_panel_does_not_verify():
    """A dashboard widget or leftover element containing 'Application submitted.'
    that is CSS-hidden (display:none) on the current form page must NOT be
    counted as a receipt — the user is still on the form.

    `document.body.innerText` respects visibility (per the HTML spec), so a
    correctly-hidden element should not appear in the innerText the matcher
    reads. This test locks in that behavior: if the matcher ever stops using
    innerText (e.g. switches to textContent or querying all nodes), the DOM
    would leak and this test would fail.
    """
    html = """
    <html><body>
      <div class="stale-widget" style="display:none">
        <p>Application submitted.</p>
        <p>Your application was received.</p>
      </div>
      <form id="apply">
        <input type="text" name="name" />
        <button type="submit">Submit application</button>
      </form>
    </body></html>
    """
    async with _synthetic_page(html) as page:
        ok, sig = await verify_receipt(page)
        assert ok is False, (
            f"stale hidden panel must NOT verify; got ok={ok} sig={sig!r}. "
            f"A visible form page with a hidden 'submitted' widget is not a receipt."
        )


# --------------------------------------------------------------------------- #
# 4. Delayed acceptance across polls (real DOM mutation) → verified
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delayed_acceptance_verifies_within_retries():
    """The page shows a form for the first N polls, then a background action
    (simulating an async SPA render) swaps in the acceptance panel via
    page.evaluate — verify_receipt must succeed within its retry budget.

    This is different from the mock-preset test in test_adapter_reliability.py:
    it exercises the REAL DOM (not a fake page.evaluate return value), so
    the polling loop's interaction with actual DOM state is under test.
    """
    initial = """
    <html><body>
      <form id="apply">
        <input type="text" name="name" />
        <button type="submit">Submit application</button>
      </form>
    </body></html>
    """
    async with _synthetic_page(initial) as page:
        # Custom sleep hook: on the 3rd wait (i.e. before the 4th check),
        # swap the DOM to the acceptance panel. Fires within the retry budget.
        call_count = {"n": 0}

        async def mutating_sleep(delay: float) -> None:
            call_count["n"] += 1
            if call_count["n"] == 3:
                await page.evaluate("""
                    () => {
                        document.body.innerHTML = `
                            <div class="thank-you">
                                <h1>Application submitted.</h1>
                                <p>Thanks for applying to Acme.</p>
                            </div>
                        `;
                    }
                """)
            # Do NOT actually sleep — keep the test fast.
            return None

        ok, sig = await verify_receipt(page, retries=5, delay=0.01, sleep=mutating_sleep)
        assert ok, (
            f"delayed acceptance should verify within retries; got ok={ok} sig={sig!r} "
            f"after {call_count['n']} poll cycles."
        )
        assert sig.startswith("t:"), (
            f"expected text-path signal, got {sig!r}"
        )
        assert call_count["n"] >= 3, (
            f"acceptance panel was injected on cycle 3 but verify_receipt returned "
            f"after only {call_count['n']} sleeps — polling window may be too tight."
        )


# --------------------------------------------------------------------------- #
# 5. Unchanged URL + valid text → verified via text path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unchanged_url_with_valid_text_verifies_via_text():
    """SPA that doesn't navigate on submit (like Ashby's jobs.ashbyhq.com/*/uuid).
    URL stays on the form page, but body has acceptance copy. Must verify
    via the text path.
    """
    html = """
    <html><body>
      <main><h1>Application submitted.</h1></main>
    </body></html>
    """
    async with _synthetic_page(
        html, base_url="https://jobs.ashbyhq.com/scan-com/12345678-abcd-4000-8000-abcdef012345"
    ) as page:
        ok, sig = await verify_receipt(page)
        assert ok and sig.startswith("t:"), (
            f"unchanged Ashby SPA URL with valid text should verify via text path; "
            f"got ok={ok} sig={sig!r}"
        )


# --------------------------------------------------------------------------- #
# 6. Unchanged URL + no acceptance text → NOT verified
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unchanged_url_no_text_does_not_verify():
    """SPA that doesn't navigate, and no acceptance copy either. Must stay
    unverified — this is the exact Scan.com scenario that produced the
    submission_unverified log line.
    """
    html = """
    <html><body>
      <form id="apply">
        <input type="text" name="name" />
        <button type="submit">Submit application</button>
      </form>
    </body></html>
    """
    async with _synthetic_page(
        html, base_url="https://jobs.ashbyhq.com/scan-com/12345678-abcd-4000-8000-abcdef012345"
    ) as page:
        ok, sig = await verify_receipt(page)
        assert ok is False and sig == "", (
            f"Ashby SPA with no acceptance copy must NOT verify; got ok={ok} sig={sig!r}"
        )


# --------------------------------------------------------------------------- #
# 7. False-positive DOM: instructional copy visible on the form page
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_instructional_copy_on_form_page_does_not_verify():
    """A form page that has instructional copy visibly saying 'You will see
    Application submitted after completing the form.' must NOT verify. This
    complements the Node-harness test — proves the false positive survives
    into the real DOM with real innerText computation.
    """
    html = """
    <html><body>
      <aside class="help-text">
        <p>You will see "Application submitted." after completing the form.</p>
      </aside>
      <form id="apply">
        <input type="text" name="name" />
        <button type="submit">Submit application</button>
      </form>
    </body></html>
    """
    async with _synthetic_page(html) as page:
        ok, sig = await verify_receipt(page)
        assert ok is False, (
            f"instructional copy on a form page must NOT verify; got ok={ok} sig={sig!r}. "
            f"This is the same failure exposed by the Node harness — reproduced in "
            f"the real DOM to prove it is not a harness artifact."
        )
