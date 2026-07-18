"""Tests for jobright._confirm_and_submit — the two-phase detect/validate/click/verify
submit path. Asserts on the real `last_apply_status` outcome (not a mocked
_set_apply_outcome) so we also cover the ApplyOutcomeCode + portal_family override
and the "no success signal ⇒ NOT applied" submission-truth guarantee.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.sources.jobright import JobrightScraper
from src.apply_outcome import ApplyOutcomeCode

DESC = {"tag": "button", "text": "Submit Application", "aria": "", "data_automation": ""}


@pytest.fixture
def scraper():
    s = JobrightScraper({"playwright": {"headless": True}})
    s._detect_portal_family = AsyncMock(return_value="ashby")
    s._visible_controls_snapshot = AsyncMock(return_value=[])
    s._format_controls_snapshot = MagicMock(return_value="[]")
    s._run_pre_submission_validation = AsyncMock(return_value={"invalidCount": 0, "requiredFilled": True})
    s._delay = AsyncMock(return_value=None)
    s.last_apply_status = None
    return s


def _page():
    page = AsyncMock()
    page.url = "https://jobs.ashbyhq.com/example/apply"
    return page


@pytest.mark.asyncio
async def test_submit_not_found_sets_outcome_and_returns_false(scraper):
    """Phase 1 finds no button; non-interactive/auto run must skip cleanly."""
    page = _page()
    scraper._safe_evaluate = AsyncMock(return_value=None)  # no descriptor

    result = await scraper._confirm_and_submit(page, {"title": "T", "company": "C"}, auto_submit=True)

    assert result is False
    assert scraper.last_apply_status == ApplyOutcomeCode.SUBMIT_NOT_FOUND.value
    # portal_family kwarg must be accepted by the override (regression guard)
    assert scraper.last_portal_family == "ashby"


@pytest.mark.asyncio
async def test_empty_form_is_not_submitted(scraper):
    """Descriptor found but no fields filled ⇒ refuse to submit."""
    page = _page()
    scraper._safe_evaluate = AsyncMock(return_value=DESC)
    page.evaluate = AsyncMock(return_value=False)  # has_filled_fields → False

    result = await scraper._confirm_and_submit(page, {"title": "T", "company": "C"}, auto_submit=True)

    assert result is False
    assert scraper.last_apply_status == ApplyOutcomeCode.FORM_EMPTY_NOT_SUBMITTED.value


@pytest.mark.asyncio
async def test_success_requires_verified_signal(scraper):
    """Button found, fields filled, click succeeds, success signal seen ⇒ applied."""
    page = _page()
    scraper._safe_evaluate = AsyncMock(return_value=DESC)

    async def fake_evaluate(js, *args):
        if isinstance(js, str) and js.strip() == "document.body.innerText.toLowerCase()":
            return "old body text"
        if args and isinstance(args[0], dict):
            return True  # the descriptor-matched click
        return True      # has_filled_fields
    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    page.wait_for_function = AsyncMock(return_value=True)  # success signal

    result = await scraper._confirm_and_submit(page, {"title": "T", "company": "C"}, auto_submit=True)

    assert result is True
    page.wait_for_function.assert_called_once()


@pytest.mark.asyncio
async def test_no_success_signal_is_unverified_not_applied(scraper):
    """The submission-truth case: clicked but no confirmation ⇒ NOT applied."""
    page = _page()
    scraper._safe_evaluate = AsyncMock(return_value=DESC)

    async def fake_evaluate(js, *args):
        if isinstance(js, str) and js.strip() == "document.body.innerText.toLowerCase()":
            return "old body text"
        if args and isinstance(args[0], dict):
            return True
        return True
    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    page.wait_for_function = AsyncMock(side_effect=Exception("timeout"))  # no signal

    result = await scraper._confirm_and_submit(page, {"title": "T", "company": "C"}, auto_submit=True)

    assert result is False
    assert scraper.last_apply_status == ApplyOutcomeCode.SUBMISSION_UNVERIFIED.value


@pytest.mark.asyncio
async def test_ambiguous_click_target_is_not_applied(scraper):
    """Descriptor no longer matches exactly one element at click time ⇒ not applied."""
    page = _page()
    scraper._safe_evaluate = AsyncMock(return_value=DESC)

    async def fake_evaluate(js, *args):
        if isinstance(js, str) and js.strip() == "document.body.innerText.toLowerCase()":
            return "old body text"
        if args and isinstance(args[0], dict):
            return False  # click could not resolve a unique target
        return True       # has_filled_fields
    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    result = await scraper._confirm_and_submit(page, {"title": "T", "company": "C"}, auto_submit=True)

    assert result is False
    assert scraper.last_apply_status == ApplyOutcomeCode.SUBMIT_NOT_FOUND.value
