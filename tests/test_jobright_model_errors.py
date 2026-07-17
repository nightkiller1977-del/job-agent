import pytest
from unittest.mock import AsyncMock

from src.model_client import ModelCascadeError
from src.sources.jobright import JobrightScraper


@pytest.mark.asyncio
async def test_claude_ats_and_tailor_propagates_model_cascade_error():
    scraper = JobrightScraper(config={})
    scraper._mc = AsyncMock()
    scraper._mc.complete.side_effect = ModelCascadeError("all tiers failed")
    scraper._extract_resume_text = lambda: "Experienced engineering leader with cloud and AI delivery background."

    with pytest.raises(ModelCascadeError):
        await scraper._claude_ats_and_tailor(
            {"title": "Engineering Director", "company": "Acme"},
            "This is a sufficiently long job description requiring cloud leadership, AI delivery, "
            "platform engineering, stakeholder management, and measurable software outcomes.",
        )
