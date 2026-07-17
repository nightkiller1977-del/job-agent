import pytest
from unittest.mock import AsyncMock, patch

from src.resume_helper import ATSValidationResult, PDFTextLayerError
from src.sources.linkedin import LinkedInScraper


@pytest.mark.asyncio
async def test_apply_external_ats_reraises_pdf_text_layer_error():
    """Regression (Codex review of PR #42): LinkedIn delegates external-ATS
    applies to JobrightScraper.apply_external_ats_job(). An unreadable/corrupt
    generated PDF must reach orchestrator.apply_approved() so it can pause the
    whole apply loop for self-healing — this must not be relabeled as a
    generic 'linkedin_external_apply_error' per-job outcome."""
    result = ATSValidationResult(
        passed=False, coverage=0.0, matched_keywords=[], unmatched_keywords=[],
        failure_type="pdf_text_layer", detail="Generated PDF has no extractable text layer.",
    )
    scraper = LinkedInScraper(config={})
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job",
        new=AsyncMock(side_effect=PDFTextLayerError(result)),
    ):
        with pytest.raises(PDFTextLayerError):
            await scraper._apply_external_ats(job, "https://boards.greenhouse.io/acme/jobs/1", "")


@pytest.mark.asyncio
async def test_apply_external_ats_still_swallows_other_errors():
    """Non-ATS errors from the delegated apply keep the old per-job behavior."""
    scraper = LinkedInScraper(config={})
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job",
        new=AsyncMock(side_effect=RuntimeError("portal timed out")),
    ):
        result = await scraper._apply_external_ats(job, "https://boards.greenhouse.io/acme/jobs/1", "")

    assert result is False
    assert scraper.last_apply_status == "linkedin_external_apply_error"
