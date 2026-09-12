"""Truthfulness regression for src/sources/adapters/receipt.py::_RECEIPT_JS.

Shells out to `node -e` with the PRODUCTION _RECEIPT_JS (imported, never copied)
so the browser-JS matcher is exercised byte-for-byte. Also covers the Python-side
_URL_CONFIRM_RE against unchanged-SPA-URL and posting-slug false-positive traps.

Missing Node or malformed harness output must fail this suite — never silently
skip a safety check. The dedicated CI step `Set up Node` ensures `node` is
present on the PATH; a local run without Node exits with a clear error.

Scaffold — real cases added in a follow-up commit on this branch.
"""
import shutil

import pytest


def test_node_is_available_for_receipt_regex_harness():
    """Baseline: `node` must be on PATH so the JS harness can run at all.

    If this fails locally, install Node 20+. In CI, the `Set up Node` step
    provisions it before pytest runs — a failure here means CI is misconfigured.
    """
    assert shutil.which("node") is not None, (
        "node executable not found on PATH; the receipt-matcher regression tests "
        "require Node to execute _RECEIPT_JS truthfully. In CI this is provisioned "
        "by actions/setup-node@v4 in .github/workflows/ci.yml."
    )
