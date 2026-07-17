import pytest
from src.sources.jobright import JobrightScraper

@pytest.mark.asyncio
async def test_resume_state_bleed():
    """Ensure state from one job doesn't bleed into the next."""
    scraper = JobrightScraper({"playwright": {"headless": True}})
    
    # Simulate Job 1 having a high ATS score and missing keywords
    scraper._last_ats_score = 95
    scraper._last_ats_missing_keywords = ["Python", "Docker"]
    scraper._last_tailored_resume_path = "/tmp/resume1.pdf"
    
    # Mock apply init
    scraper.auto_submit = False
    
    # When we start a new apply, these should be cleared
    job = {"title": "Test 2", "company": "Co 2", "url": "https://jobright.ai/test2"}
    
    # Just run the initialization part of apply (up to browser start)
    # We will just patch _start_browser to raise an exception so it stops early
    class StopEarly(Exception): pass
    
    async def mock_start_browser(*args, **kwargs):
        raise StopEarly("Stop")
        
    scraper._start_browser = mock_start_browser
    
    try:
        await scraper.apply(job)
    except StopEarly:
        pass
        
    # Assert state is cleared
    assert scraper._last_ats_score == 0
    assert scraper._last_ats_missing_keywords == []
    assert scraper._last_tailored_resume_path == ""
