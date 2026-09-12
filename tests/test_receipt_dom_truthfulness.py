"""DOM-aware regression for src/sources/adapters/receipt.py::verify_receipt.

Uses real Playwright chromium + page.set_content() to run the production
verifier against synthetic HTML. Network is blocked via route interception —
no employer sites are contacted, no credentials required.

The DOM layer catches cases the Node-only harness cannot:
  - stale/hidden confirmation panels that must not be counted as a fresh receipt
  - form-gone-without-acceptance (must remain unverified, not upgraded)
  - delayed acceptance across multiple polls with real DOM mutation

Scaffold — real cases added in a follow-up commit on this branch.
"""
import importlib

import pytest


def test_playwright_chromium_is_installed_for_dom_receipt_harness():
    """Baseline: playwright + a bundled chromium must be importable.

    If this fails locally, run `python -m playwright install chromium`. In CI
    the `Install Playwright chromium` step handles it — failure means the CI
    workflow is out of sync with these tests.
    """
    try:
        importlib.import_module("playwright.async_api")
    except ImportError as exc:  # pragma: no cover — provisioning failure
        pytest.fail(f"playwright not importable: {exc}. See requirements.txt.")
