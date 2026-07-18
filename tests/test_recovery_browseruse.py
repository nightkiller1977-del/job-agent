import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from src.sources.adapters.context import AtsApplyContext, AtsApplyResult
from src.sources.adapters.recovery_browseruse import BrowserUseRecovery
from src.sources.adapters.policy import AutoSubmitPolicy, DenyAllPolicy

# Mark as async tests
pytestmark = pytest.mark.asyncio

@patch("src.sources.adapters.recovery_browseruse.ModelClient")
async def test_recovery_skills_replay(mock_mc_class, tmp_path):
    # Setup mock page
    mock_page = MagicMock()
    mock_page.url = "https://jobs.lever.co/mycompany/123"
    mock_page.locator.return_value.inner_text = AsyncMock(return_value="Thanks for applying!")
    
    ctx = AtsApplyContext(
        page=mock_page,
        job={"url": "https://jobs.lever.co/mycompany/123"},
        profile={},
        resume_path="/tmp/resume.pdf",
        url="https://jobs.lever.co/mycompany/123",
        auto_submit=True, attempt_id="t1", policy=AutoSubmitPolicy(allow=True),
    )
    
    recovery = BrowserUseRecovery()
    # Configure custom path for testing skills mapping
    skills_file = tmp_path / "skills.json"
    recovery.SKILLS_FILE = skills_file
    
    # Save a fake skill
    recovery._save_skills("jobs.lever.co", [
        {"action": "fill", "selector": "input#name", "value": "John Doe"}
    ])
    
    # Mock page element execution
    mock_page.wait_for_selector = AsyncMock()
    mock_page.fill = AsyncMock()
    
    res = await recovery.apply(ctx)
    
    # Verify replay was called
    mock_page.wait_for_selector.assert_called_once_with("input#name", timeout=8000)
    mock_page.fill.assert_called_once_with("input#name", "John Doe")
    assert res.submitted is True
    assert res.status == "applied"

@patch("src.sources.adapters.recovery_browseruse.ModelClient")
async def test_recovery_llm_loop(mock_mc_class, tmp_path):
    # Setup mock ModelClient response
    mock_mc = MagicMock()
    mock_mc_class.return_value = mock_mc
    
    # Step 1: fill email. Step 2: click apply. Step 3: declare done.
    mock_mc.complete = AsyncMock(side_effect=[
        '{"action": "fill", "selector": "input#email", "value": "test@example.com", "explanation": "fill email"}',
        '{"action": "done", "selector": "", "value": "", "explanation": "finished form"}'
    ])
    
    mock_page = MagicMock()
    mock_page.url = "https://boards.greenhouse.io/myco/jobs/1"
    mock_page.locator.return_value.inner_text = AsyncMock(return_value="Submit application")
    mock_page.title = AsyncMock(return_value="Careers")
    
    mock_page.fill = AsyncMock()
    
    ctx = AtsApplyContext(
        page=mock_page,
        job={"url": "https://boards.greenhouse.io/myco/jobs/1"},
        profile={"personal_info": {"email": "test@example.com"}},
        resume_path=None,
        url="https://boards.greenhouse.io/myco/jobs/1",
        auto_submit=True, attempt_id="t2", policy=AutoSubmitPolicy(allow=True),
    )

    recovery = BrowserUseRecovery()
    recovery.SKILLS_FILE = tmp_path / "skills.json"

    res = await recovery.apply(ctx)

    # Verify fill was called
    mock_page.fill.assert_called_once_with("input#email", "test@example.com")
    # Phase 0.1: LLM "done" without a verified receipt is readiness, NOT a submission.
    assert res.submitted is False
    assert res.status == "review_ready"
    
    # Verify skills were saved dynamically
    skills = recovery._load_skills("boards.greenhouse.io")
    assert len(skills) == 1
    assert skills[0]["action"] == "fill"
    assert skills[0]["selector"] == "input#email"


@patch("src.sources.adapters.recovery_browseruse.ModelClient")
async def test_recovery_refuses_without_policy_authorization(mock_mc_class, tmp_path):
    """Phase 0.3: the LLM agent must not run (and cannot submit) unless the policy
    authorizes submission."""
    mock_mc = MagicMock()
    mock_mc.complete = AsyncMock()
    mock_mc_class.return_value = mock_mc
    mock_page = MagicMock()
    mock_page.url = "https://boards.greenhouse.io/myco/jobs/1"

    ctx = AtsApplyContext(
        page=mock_page,
        job={"url": "https://boards.greenhouse.io/myco/jobs/1"},
        profile={},
        url="https://boards.greenhouse.io/myco/jobs/1",
        auto_submit=True, attempt_id="t3", policy=DenyAllPolicy(),
    )
    res = await BrowserUseRecovery().apply(ctx)
    assert res.submitted is False
    assert res.status == "submit_denied_by_policy"
    mock_mc.complete.assert_not_called()   # never even asked the LLM for an action
