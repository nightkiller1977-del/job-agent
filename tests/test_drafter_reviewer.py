import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.sources.jobright import JobrightScraper

@pytest.mark.asyncio
async def test_drafter_reviewer_pipeline():
    """Verify that the two-stage Drafter-Reviewer model pipeline executes both stages and uses the reviewer's output."""
    scraper = JobrightScraper(config={})
    
    # Mock ModelClient complete method
    mock_complete = AsyncMock()
    mock_complete.side_effect = [
        # 1st call: draft JSON response
        '{"ats_score": 75, "missing_keywords": ["Kubernetes"], "matching_keywords": ["Python"], "recommendation": "Draft advice", "tailored_summary": "Draft summary", "tailored_bullets": [], "cover_letter": "Draft cover letter"}',
        # 2nd call: reviewer corrected JSON response
        '{"ats_score": 90, "missing_keywords": ["Kubernetes"], "matching_keywords": ["Python", "Docker"], "recommendation": "Reviewed advice", "tailored_summary": "Reviewed summary", "tailored_bullets": [], "cover_letter": "Reviewed cover letter"}'
    ]
    scraper._model_client.complete = mock_complete
    
    # Mock text extraction methods to avoid file dependencies
    scraper._extract_resume_text = MagicMock(return_value="Candidate Profile: Python, Docker, no Kubernetes")
    
    # Mock model_span context manager using a real generator
    import contextlib
    @contextlib.contextmanager
    def mock_span_ctx(*args, **kwargs):
        yield {}
        
    with patch("src.sources.jobright.model_span", mock_span_ctx):
        job = {"title": "Senior Engineer", "company": "Tech Corp"}
        long_jd = "Job description requiring Python, Docker, and Kubernetes. The candidate will design scalable systems and deploy them. This description needs to be over one hundred characters to pass the guard clause."
        result = await scraper._claude_ats_and_tailor(job, long_jd)
        
        # Verify the final values are those returned by the Reviewer stage
        assert result["ats_score"] == 90
        assert result["tailored_summary"] == "Reviewed summary"
        assert result["cover_letter"] == "Reviewed cover letter"
        
        # Verify both completion stages were called
        assert mock_complete.call_count == 2
