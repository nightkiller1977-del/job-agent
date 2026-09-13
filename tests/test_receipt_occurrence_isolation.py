"""Isolated regression for receipt freshness by occurrence count.

This exists separately from the broader Chromium DOM fixture because that
fixture also renders additional success copy. The invariant here is narrower:
if the exact same recognized receipt text already existed before submit, a
second occurrence of that same text must still count as fresh evidence — and
nothing else may be responsible for the match-count increase.
"""
from __future__ import annotations

import pytest

from src.sources.adapters.receipt import capture_receipt_evidence, verify_receipt


class _OccurrencePage:
    def __init__(self):
        self.url = "https://form.local/apply"
        self.signal = "t:application submitted"
        self.count = 1

    async def evaluate(self, script, *args):
        if "sentinel: acceptance-matcher harness" in script:
            return self.signal
        if "sentinel: acceptance-count harness" in script:
            return self.count
        return None


@pytest.mark.asyncio
async def test_identical_receipt_text_is_fresh_only_when_occurrence_count_increases():
    page = _OccurrencePage()
    baseline = await capture_receipt_evidence(page)

    # Same text, same occurrence count: stale evidence from before this attempt.
    ok, signal = await verify_receipt(page, baseline=baseline)
    assert ok is False
    assert signal == ""

    # The only change is a second occurrence of the exact same receipt text.
    # There is no second success phrase or reference-id channel in this fake.
    page.count = 2
    ok, signal = await verify_receipt(page, baseline=baseline)

    assert ok is True
    assert signal == "t:application submitted"
