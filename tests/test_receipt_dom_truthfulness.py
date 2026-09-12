"""DOM-aware regression for src/sources/adapters/receipt.py::verify_receipt.

Every scenario runs in a FRESH PYTHON SUBPROCESS with real chromium — never
in-process. Reason: tests/conftest.py globally stubs playwright.async_api in
sys.modules for the whole pytest run so unit modules can import src.sources.*
without the browser stack. An in-process `sys.modules.pop` unstub would be
order-dependent: any earlier-imported module already holding a reference to
the fake Playwright would keep it, and later tests could see either fake or
real depending on collection order. Running each scenario in a subprocess
gives every test a clean interpreter where the real playwright is imported
first and never shadowed.

Trade-off: chromium launch ~1–2s per test. Acceptable to preserve isolation.

Network is blocked via route interception in the child — no employer sites
contacted, no credentials required.

The DOM layer catches cases the Node-only harness cannot:
  - stale confirmation panels that must not be counted as receipts
    (display:none, visibility:hidden, AND visibly-rendered stale copy)
  - form-gone-without-acceptance (must remain unverified, not upgraded)
  - delayed acceptance across multiple polls with real DOM mutation

If chromium is not installed locally, `python -m playwright install chromium`
provisions it. In CI the `Install Playwright chromium` step handles it —
a failure here means the CI workflow is out of sync with these tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# CI-config guard — importable in-process (playwright module presence only)
# --------------------------------------------------------------------------- #
def test_playwright_is_installed_for_dom_receipt_harness():
    """Baseline: the real playwright package must be importable from a fresh
    subprocess. In-process `importlib.import_module` here would be misleading
    because conftest.py stubs sys.modules — subprocess is the only truthful
    check.
    """
    repo_root = os.fspath(Path(__file__).resolve().parent.parent)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c",
         "from playwright.async_api import async_playwright; print('OK')"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0 and "OK" in result.stdout, (
        f"real playwright not importable from fresh interpreter: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# subprocess harness — every DOM scenario runs here, JSON in / JSON out
# --------------------------------------------------------------------------- #
# The child script imports the PRODUCTION verify_receipt (never a copy),
# launches real chromium, serves synthetic HTML via a route interceptor that
# aborts anything not matching the fixture URL (network is blocked), runs the
# scenario, and prints one JSON line to stdout with the outcome. Any exception
# is captured and returned as an "error" field so the parent test can fail
# with a clean message rather than a Playwright stack trace.
_CHILD_SCRIPT = r"""
import asyncio, json, sys, traceback


async def _run(case):
    from playwright.async_api import async_playwright
    from src.sources.adapters.receipt import verify_receipt

    initial_html = case["initial_html"]
    base_url = case["base_url"]
    retries = int(case.get("retries", 0))
    delay = float(case.get("delay", 0.05))
    # Optional: after N ms, swap the DOM to `mutated_html`. Used for the
    # delayed-acceptance test — the mutation is scheduled by a real timer
    # so verify_receipt's polling loop absorbs a genuine async DOM change.
    mutate_after_ms = case.get("mutate_after_ms")
    mutated_html = case.get("mutated_html")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context()

            async def _route(route):
                if route.request.url.rstrip("/") == base_url.rstrip("/"):
                    await route.fulfill(
                        status=200, content_type="text/html", body=initial_html,
                    )
                else:
                    await route.abort()

            await ctx.route("**/*", _route)
            page = await ctx.new_page()
            await page.goto(base_url)

            if mutate_after_ms is not None and mutated_html is not None:
                # Schedule the DOM swap with a real browser-side timer so the
                # polling loop observes an actual async change (not a scripted
                # value swap). setTimeout runs in the page's event loop.
                await page.evaluate(
                    "(args) => { setTimeout(() => { document.open(); "
                    "document.write(args.html); document.close(); }, args.ms); }",
                    {"html": mutated_html, "ms": int(mutate_after_ms)},
                )

            ok, sig = await verify_receipt(page, retries=retries, delay=delay)
            return {"ok": bool(ok), "sig": str(sig or "")}
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


def _run_dom_case(**case) -> dict:
    """Run one DOM scenario in a fresh Python subprocess. Returns the parsed
    JSON result dict `{"ok": bool, "sig": str}`. Fails the test with a clean
    message on subprocess error / timeout / non-JSON output.
    """
    repo_root = os.fspath(Path(__file__).resolve().parent.parent)
    try:
        proc = subprocess.run(  # noqa: S603 — args controlled, no shell
            [sys.executable, "-c", _CHILD_SCRIPT],
            input=json.dumps(case),
            capture_output=True, text=True, timeout=60, check=False,
            env={**os.environ, "PYTHONPATH": repo_root},
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"DOM harness subprocess timed out: {exc}")

    if proc.returncode != 0 and not proc.stdout.strip():
        pytest.fail(
            f"DOM harness subprocess exited {proc.returncode} with no stdout.\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        parsed = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        pytest.fail(
            f"DOM harness subprocess produced non-JSON stdout: {exc}\n"
            f"stdout was: {proc.stdout!r}\nstderr:\n{proc.stderr}"
        )
    if "error" in parsed:
        pytest.fail(
            f"DOM harness subprocess raised inside the child:\n{parsed['error']}"
        )
    return parsed


BASE_FORM_URL = "https://form.local/apply"
ASHBY_SPA_URL = "https://jobs.ashbyhq.com/scan-com/12345678-abcd-4000-8000-abcdef012345"


# --------------------------------------------------------------------------- #
# 1. Fresh acceptance panel replacing form → verified
# --------------------------------------------------------------------------- #
def test_fresh_acceptance_panel_verifies():
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
    r = _run_dom_case(initial_html=html, base_url=BASE_FORM_URL)
    assert r["ok"], f"fresh acceptance panel should verify; got sig={r['sig']!r}"
    assert r["sig"].startswith("t:"), (
        f"expected text-path signal, got {r['sig']!r} — URL regex should not fire "
        f"on the base URL {BASE_FORM_URL!r}."
    )


# --------------------------------------------------------------------------- #
# 2. Form-gone-without-acceptance → must NOT verify
# --------------------------------------------------------------------------- #
def test_form_gone_without_acceptance_does_not_verify():
    """The form disappears (e.g. SPA cleared the DOM), but no acceptance copy
    appears. This must REMAIN unverified — a missing form is not a receipt.
    Mirrors the existing ashby._gated_submit form-removed convention.
    """
    html = "<html><body><div id='app'></div></body></html>"
    r = _run_dom_case(initial_html=html, base_url=BASE_FORM_URL)
    assert r["ok"] is False and r["sig"] == "", (
        f"form-gone-without-acceptance must not verify; got {r!r}"
    )


# --------------------------------------------------------------------------- #
# 3. Stale panels — three shapes, all must NOT verify
# --------------------------------------------------------------------------- #
def test_stale_display_none_panel_does_not_verify():
    """A dashboard widget with 'Application submitted.' that is `display:none`
    on the current form page must NOT count as a receipt. `document.body.innerText`
    respects display:none per HTML spec, so this test proves the current
    behavior — a fix that switches to textContent would break it.

    This test proves ONLY the display:none case; a stale confirmation could
    remain technically rendered through other mechanisms (see the sibling
    tests for visibility:hidden and visible-stale-copy).
    """
    html = """
    <html><body>
      <div class="stale-widget" style="display:none">
        <p>Application submitted.</p>
        <p>Your application was received.</p>
      </div>
      <form id="apply"><input name="name"/><button type="submit">Submit</button></form>
    </body></html>
    """
    r = _run_dom_case(initial_html=html, base_url=BASE_FORM_URL)
    assert r["ok"] is False, (
        f"display:none stale panel must NOT verify; got {r!r}"
    )


def test_stale_visibility_hidden_panel_does_not_verify():
    """Same shape, but `visibility:hidden` instead of `display:none`. innerText
    per HTML spec ALSO excludes visibility:hidden content; verify that holds
    for our matcher chain.
    """
    html = """
    <html><body>
      <div class="stale-widget" style="visibility:hidden">
        <p>Application submitted.</p>
      </div>
      <form id="apply"><input name="name"/><button type="submit">Submit</button></form>
    </body></html>
    """
    r = _run_dom_case(initial_html=html, base_url=BASE_FORM_URL)
    assert r["ok"] is False, (
        f"visibility:hidden stale panel must NOT verify; got {r!r}"
    )


def test_visibly_rendered_stale_copy_must_not_verify():
    """The important semantic boundary: instructional or stale copy that IS
    visibly rendered on the form page must NOT be counted as a receipt.
    innerText DOES include this content — this is where the fix must add
    context sensitivity (a real receipt is a fresh signal contextual to the
    current attempt, not any acceptance phrase anywhere on the page).

    Baseline behavior: this fails today (the current matcher fires on the
    substring). It stays RED until the fix lands.
    """
    html = """
    <html><body>
      <aside class="help-text">
        <p>You will see "Application submitted." after completing the form.</p>
      </aside>
      <form id="apply">
        <input name="name"/>
        <button type="submit">Submit application</button>
      </form>
    </body></html>
    """
    r = _run_dom_case(initial_html=html, base_url=BASE_FORM_URL)
    assert r["ok"] is False, (
        f"visibly-rendered stale/instructional copy must NOT verify; got {r!r}. "
        f"This is the same false positive the Node harness exposes, reproduced "
        f"through real chromium innerText to prove it is not a harness artifact."
    )


# --------------------------------------------------------------------------- #
# 4. Delayed acceptance across polls (real browser timer + real DOM mutation)
# --------------------------------------------------------------------------- #
def test_delayed_acceptance_verifies_within_retries():
    """The page shows a form initially, then a REAL browser timer swaps in the
    acceptance panel via `document.write`. verify_receipt's polling loop must
    absorb the async change and succeed.

    Retries=8 × delay=0.05s ≈ 400ms budget; mutation fires at 100ms.
    """
    initial = """
    <html><body>
      <form id="apply"><input name="name"/><button type="submit">Submit</button></form>
    </body></html>
    """
    mutated = """
    <html><body>
      <div class="thank-you"><h1>Application submitted.</h1></div>
    </body></html>
    """
    r = _run_dom_case(
        initial_html=initial, mutated_html=mutated, mutate_after_ms=100,
        base_url=BASE_FORM_URL, retries=8, delay=0.05,
    )
    assert r["ok"], (
        f"delayed acceptance should verify within retries; got {r!r}. "
        f"Polling budget was 400ms; mutation scheduled at 100ms."
    )
    assert r["sig"].startswith("t:")


# --------------------------------------------------------------------------- #
# 5. Ashby unchanged URL + valid text → verified via text path
# --------------------------------------------------------------------------- #
def test_unchanged_ashby_url_with_valid_text_verifies_via_text():
    """SPA that doesn't navigate on submit. URL stays on the form page, but
    body has acceptance copy. Must verify via the text path (URL regex won't
    match the SPA URL).
    """
    html = "<html><body><main><h1>Application submitted.</h1></main></body></html>"
    r = _run_dom_case(initial_html=html, base_url=ASHBY_SPA_URL)
    assert r["ok"] and r["sig"].startswith("t:"), (
        f"unchanged Ashby SPA URL with valid text should verify via text path; got {r!r}"
    )


# --------------------------------------------------------------------------- #
# 6. Ashby unchanged URL + no text → NOT verified (the exact Scan.com case)
# --------------------------------------------------------------------------- #
def test_unchanged_ashby_url_no_text_does_not_verify():
    """SPA that doesn't navigate, and no acceptance copy either. Must stay
    unverified — this is the exact Scan.com scenario that produced the
    submission_unverified log line in production.
    """
    html = """
    <html><body>
      <form id="apply"><input name="name"/><button type="submit">Submit</button></form>
    </body></html>
    """
    r = _run_dom_case(initial_html=html, base_url=ASHBY_SPA_URL)
    assert r["ok"] is False and r["sig"] == "", (
        f"Ashby SPA with no acceptance copy must NOT verify; got {r!r}"
    )
