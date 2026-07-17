import json

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


@pytest.mark.asyncio
async def test_claude_ats_and_tailor_falls_back_to_draft_when_reviewer_cascade_fails():
    """Regression (Codex review of PR #42): a ModelCascadeError on the reviewer/
    grounding step must fall back to the drafter's output, not discard an
    already-usable draft and fail the whole job as model_timeout."""
    draft = {
        "ats_score": 82,
        "missing_keywords": ["Kubernetes"],
        "matching_keywords": ["Python", "AWS"],
        "recommendation": "Strong match",
        "tailored_summary": "Engineering leader with cloud delivery background.",
        "tailored_bullets": [{"role": "Director", "bullets": ["Led cloud migration"]}],
        "cover_letter": "Dear Hiring Manager, ...",
    }
    scraper = JobrightScraper(config={})
    scraper._mc = AsyncMock()
    scraper._mc.complete.side_effect = [json.dumps(draft), ModelCascadeError("reviewer tier exhausted")]
    scraper._extract_resume_text = lambda: "Experienced engineering leader with cloud and AI delivery background."

    result = await scraper._claude_ats_and_tailor(
        {"title": "Engineering Director", "company": "Acme"},
        "This is a sufficiently long job description requiring cloud leadership, AI delivery, "
        "platform engineering, stakeholder management, and measurable software outcomes.",
    )

    assert result["ats_score"] == 82
    assert result["matching_keywords"] == ["Python", "AWS"]
    assert result["cover_letter"] == "Dear Hiring Manager, ..."
