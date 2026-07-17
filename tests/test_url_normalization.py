import pytest
from src.sources.jobright import JobrightScraper

def test_url_normalization():
    scraper = JobrightScraper({"playwright": {"headless": True}})
    
    base = "https://jobright.ai"
    
    # Absolute URL should remain unchanged
    assert scraper._normalize_url("https://jobright.ai/jobs/123", base) == "https://jobright.ai/jobs/123"
    
    # Relative URL with leading slash
    assert scraper._normalize_url("/jobs/123", base) == "https://jobright.ai/jobs/123"
    
    # Relative URL without leading slash
    assert scraper._normalize_url("jobs/123", base) == "https://jobright.ai/jobs/123"
    
    # Empty URL
    assert scraper._normalize_url("", base) == ""
    
    # External absolute URL
    assert scraper._normalize_url("https://external-ats.com/apply", base) == "https://external-ats.com/apply"
