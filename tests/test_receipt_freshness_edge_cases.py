"""Edge-case receipt freshness regressions added during PR #121 review.

These tests target gaps that remained after the first freshness implementation:
Python-owned baseline state across navigation/body replacement, polling past a
stale-but-valid signal until fresh evidence appears, freshness for URL and
reference-id channels, fail-closed baseline capture, and common confirmation
phrases that the tightened matcher accidentally dropped.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from src.sources.adapters import receipt as receipt_mod


class FreshnessPage:
    """Small page fake for freshness semantics, not regex semantics."""

    def __init__(self, *, url="https://form.local/apply", signal=None, count=0):
        self.url = url
        self.signal = signal
        self.count = count
        self.stored_count = 0
        self.fail_next_count = False

    async def evaluate(self, script, *args):
        if "sentinel: acceptance-matcher harness" in script:
            return self.signal
        if "sentinel: acceptance-count harness" in script:
            if self.fail_next_count:
                self.fail_next_count = False
                raise RuntimeError("synthetic count-evaluation failure")
            return self.count
        if "receiptBaselineCount = String" in script:
            self.stored_count = int((args[0] if args else 0) or 0)
            return None
        if "parseInt(document.body.dataset.receiptBaselineCount" in script:
            return self.stored_count
        return None


async def _capture_baseline(page):
    """Use the new explicit capture API when present; current PR head falls
    back to its baseline-return convention so the tests fail behaviorally rather
    than merely on a missing symbol.
    """
    capture = getattr(receipt_mod, "capture_receipt_evidence", None)
    if capture is not None:
        return await capture(page)
    return await receipt_mod.verify_receipt(page, retries=0)


@pytest.mark.asyncio
async def test_body_replacement_does_not_erase_stale_baseline():
    page = FreshnessPage(signal="t:application submitted", count=1)
    baseline = await _capture_baseline(page)

    # A full navigation/body replacement destroys DOM-owned dataset state. The
    # visible stale confirmation itself is unchanged and therefore must NOT
    # become fresh merely because the document node changed.
    page.stored_count = 0
    ok, _ = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is False


@pytest.mark.asyncio
async def test_stale_baseline_is_polled_until_fresh_signal_appears():
    page = FreshnessPage(signal="t:application submitted", count=1)
    baseline = await _capture_baseline(page)
    slept = 0

    async def _sleep(_delay):
        nonlocal slept
        slept += 1
        # The stale confirmation is replaced by a genuinely new success signal
        # during the configured polling window.
        page.signal = "t:thanks for applying to acme"
        page.count = 1

    ok, sig = await receipt_mod.verify_receipt(
        page, retries=2, delay=0.01, sleep=_sleep, baseline=baseline,
    )
    assert slept >= 1, "freshness rejection must not terminate polling early"
    assert ok is True and sig == "t:thanks for applying to acme"


@pytest.mark.asyncio
async def test_new_confirmation_url_is_fresh_even_with_stale_text_baseline():
    page = FreshnessPage(signal="t:application submitted", count=1)
    baseline = await _capture_baseline(page)

    page.url = "https://form.local/apply/thank-you"
    page.signal = None
    page.count = 0
    ok, sig = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is True and sig.startswith("url:")


@pytest.mark.asyncio
async def test_same_preexisting_confirmation_url_is_not_fresh():
    page = FreshnessPage(url="https://form.local/apply/thank-you", signal=None, count=0)
    baseline = await _capture_baseline(page)
    ok, _ = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is False


@pytest.mark.asyncio
async def test_new_reference_id_is_fresh_even_when_text_match_count_is_zero():
    page = FreshnessPage(signal="ref:OLD1234", count=0)
    baseline = await _capture_baseline(page)

    page.signal = "ref:NEW5678"
    ok, sig = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is True and sig == "ref:NEW5678"


@pytest.mark.asyncio
async def test_baseline_evaluation_failure_fails_closed_not_open():
    page = FreshnessPage(signal="t:application submitted", count=1)
    page.fail_next_count = True
    baseline = await _capture_baseline(page)

    # Same stale evidence after submit. A failed baseline-count read must never
    # be treated as zero and thereby make this unchanged signal appear fresh.
    ok, _ = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is False


def _run_receipt_js(body_text: str, js: str | None = None):
    node = shutil.which("node")
    assert node is not None, "Node is required by the receipt truthfulness CI job"
    wrapper = (
        "const fs = require('fs');\n"
        "const body = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "globalThis.document = { body: { innerText: body } };\n"
        f"const fn = ({js or receipt_mod._RECEIPT_JS});\n"
        "process.stdout.write(JSON.stringify(fn()));\n"
    )
    proc = subprocess.run(
        [node, "-e", wrapper], input=json.dumps(body_text), capture_output=True,
        text=True, timeout=10, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "body",
    [
        "Thank you for your application.",
        "Thanks for applying.",
        "Application submitted",
    ],
)
def test_common_success_phrases_remain_recognized(body):
    result = _run_receipt_js(body)
    if isinstance(result, list):
        assert result, f"valid confirmation was not recognized: {body!r}"
    else:
        assert result, f"valid confirmation was not recognized: {body!r}"


@pytest.mark.asyncio
async def test_identical_receipt_text_is_fresh_only_when_occurrence_count_increases():
    """A second occurrence of the same recognized receipt text is fresh evidence.

    Kept away from the Chromium fixture in test_receipt_dom_truthfulness.py,
    whose mutation also injects a different success phrase and so could pass for
    the wrong reason. The counts are produced by the production counter rather
    than hand-set, so a regression in _COUNT_JS fails here instead of silently
    downgrading a real submission to unverified.
    """
    one = _run_receipt_js("Application submitted.", receipt_mod._COUNT_JS)
    two = _run_receipt_js(
        "Application submitted.\nApplication submitted.", receipt_mod._COUNT_JS
    )
    assert two > one, "production counter must see the second occurrence"

    page = FreshnessPage(signal="t:application submitted", count=one)
    baseline = await _capture_baseline(page)

    # Same text, same occurrence count: stale evidence from before this attempt.
    ok, signal = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is False
    assert signal == ""

    # The only change is a second occurrence of the exact same receipt text.
    # There is no second success phrase or reference-id channel in this fake.
    page.count = two
    ok, signal = await receipt_mod.verify_receipt(page, baseline=baseline)

    assert ok is True
    assert signal == "t:application submitted"


@pytest.mark.asyncio
async def test_post_submit_count_failure_fails_closed_not_open():
    """Mirror of the baseline-side guard: an unreadable post-submit count must
    not reach the comparison, which would raise TypeError and abort the apply
    run instead of degrading to unverified.
    """
    page = FreshnessPage(signal="t:application submitted", count=1)
    baseline = await _capture_baseline(page)

    page.count = 2
    page.fail_next_count = True
    ok, signal = await receipt_mod.verify_receipt(page, baseline=baseline)
    assert ok is False
    assert signal == ""
