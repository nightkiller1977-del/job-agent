import pytest
from unittest.mock import AsyncMock, MagicMock
from src.sources.jobright import JobrightScraper

@pytest.fixture
def scraper():
    s = JobrightScraper({"playwright": {"headless": True}})
    s._set_apply_outcome = MagicMock(return_value=False)
    s._detect_portal_family = AsyncMock(return_value="ashby")
    s._visible_controls_snapshot = AsyncMock(return_value=[])
    s._format_controls_snapshot = MagicMock(return_value="[]")
    s._run_pre_submission_validation = AsyncMock(return_value={'invalidCount': 0, 'requiredFilled': True})
    return s

@pytest.mark.asyncio
async def test_ashby_submit_not_found(scraper):
    page = AsyncMock()
    page.url = "https://jobs.ashbyhq.com/example/apply"
    # Phase 1 evaluate returns False (button not found)
    scraper._safe_evaluate = AsyncMock(return_value=False)
    
    result = await scraper._confirm_and_submit(page, {"title": "Test"}, auto_submit=True)
    
    # Should return False and set outcome to ashby_submit_not_found
    assert result is False
    scraper._set_apply_outcome.assert_called_with(
        "ashby_submit_not_found",
        "Submit button not found at https://jobs.ashbyhq.com/example/apply. Visible controls: []"
    )

@pytest.mark.asyncio
async def test_ashby_form_empty(scraper):
    page = AsyncMock()
    page.url = "https://jobs.ashbyhq.com/example/apply"
    # Button found
    scraper._safe_evaluate = AsyncMock(return_value=True)
    page.query_selector = AsyncMock(return_value=MagicMock())
    # Form empty
    page.evaluate = AsyncMock(return_value=False)
    
    result = await scraper._confirm_and_submit(page, {"title": "Test"}, auto_submit=True)
    
    assert result is False
    scraper._set_apply_outcome.assert_called_with(
        "form_empty_not_submitted",
        "Submit button found at https://jobs.ashbyhq.com/example/apply but form fields were empty."
    )

@pytest.mark.asyncio
async def test_ashby_submit_success(scraper):
    page = AsyncMock()
    page.url = "https://jobs.ashbyhq.com/example/apply"
    # Button found
    scraper._safe_evaluate = AsyncMock(return_value=True)
    btn = AsyncMock()
    page.query_selector = AsyncMock(return_value=btn)
    # Form filled
    page.evaluate = AsyncMock(return_value=True)
    
    # Verification succeeds
    page.wait_for_function = AsyncMock(return_value=True)
    
    result = await scraper._confirm_and_submit(page, {"title": "Test"}, auto_submit=True)
    
    assert result is True
    # Should click the button using evaluate
    page.evaluate.assert_any_call("btn => btn.click()", btn)
    # Should wait for verification signal
    page.wait_for_function.assert_called_once()
