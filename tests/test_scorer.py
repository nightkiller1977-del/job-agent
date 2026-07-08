"""
Unit tests for src/scorer.py

Covers:
  - _quick_ic_check: IC detection, word-boundary safety (no false positives)
  - _parse_response: valid JSON, markdown fenced, deepseek <think> blocks, bad JSON
  - _build_profile_from_config: config values propagate to prompt
  - _smart_excerpt: truncation strategy
  - score(): IC fast-path, model cascade fallback
  - batch_score(): concurrency, exception isolation
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.scorer import (
    JobScorer,
    _build_profile_from_config,
    _smart_excerpt,
)


# ---------------------------------------------------------------------------
# _quick_ic_check
# ---------------------------------------------------------------------------

def _make_scorer() -> JobScorer:
    with patch("src.scorer.ModelClient"):
        return JobScorer(config={})


@pytest.mark.parametrize("title,expected_reject", [
    # Should reject (pure IC)
    ("Software Engineer", True),
    ("Senior Software Engineer", True),   # "senior" is NOT a seniority bypass word
    ("Staff Engineer", True),
    ("Principal Engineer", True),
    ("Data Engineer", True),
    ("DevOps Engineer", True),
    ("Site Reliability Engineer", True),
    ("SRE", True),                         # word-boundary regex catches "SRE" at end
    ("ML Engineer", True),
    ("Frontend Engineer", True),
    ("Backend Engineer", True),
    ("Full Stack Engineer", True),
    # Should NOT reject (has management/seniority word)
    ("Director of Software Engineering", False),
    ("VP of Engineering", False),
    ("Engineering Manager", False),
    ("Head of DevOps", False),
    ("Senior SRE Manager", False),         # "Manager" saves it
    ("Lead Software Engineer", False),     # "Lead" saves it
    ("GS-15 Software Engineer", False),    # no seniority BUT no IC pattern match without word
    # Unrelated
    ("Program Manager", False),
    ("CTO", False),
])
def test_quick_ic_check(title: str, expected_reject: bool) -> None:
    scorer = _make_scorer()
    result = scorer._quick_ic_check({"title": title})
    if expected_reject:
        assert result is not None, f"Expected IC rejection for '{title}'"
        assert "IC role" in result
    else:
        assert result is None, f"Unexpected IC rejection for '{title}': {result}"


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

GOOD_RESPONSE = json.dumps({
    "score": 85,
    "reason": "Strong match: Director-level role with remote option.",
    "flags": "CLEARED_ROLE",
    "recommended_action": "apply",
})


def test_parse_good_json() -> None:
    scorer = _make_scorer()
    score, reason, flags, action = scorer._parse_response(GOOD_RESPONSE)
    assert score == 85
    assert "Director" in reason
    assert flags == "CLEARED_ROLE"
    assert action == "apply"


def test_parse_fenced_json() -> None:
    scorer = _make_scorer()
    fenced = f"```json\n{GOOD_RESPONSE}\n```"
    score, reason, _, action = scorer._parse_response(fenced)
    assert score == 85
    assert action == "apply"


def test_parse_deepseek_think_block() -> None:
    scorer = _make_scorer()
    raw = f"<think>Let me evaluate this role carefully...</think>\n{GOOD_RESPONSE}"
    score, _, _, action = scorer._parse_response(raw)
    assert score == 85
    assert action == "apply"


def test_parse_embedded_json() -> None:
    scorer = _make_scorer()
    raw = f"Here is my analysis:\n{GOOD_RESPONSE}\nDone."
    score, _, _, _ = scorer._parse_response(raw)
    assert score == 85


def test_parse_invalid_json_returns_flag() -> None:
    scorer = _make_scorer()
    score, reason, flags, action = scorer._parse_response("not json at all")
    assert score == 50
    assert flags == "FLAG_FOR_REVIEW"
    assert action == "review"


def test_parse_score_clamped() -> None:
    scorer = _make_scorer()
    # score above 100 should be clamped to 100
    raw = json.dumps({"score": 150, "reason": "x", "flags": "", "recommended_action": "apply"})
    score, _, _, _ = scorer._parse_response(raw)
    assert score == 100


# ---------------------------------------------------------------------------
# _build_profile_from_config
# ---------------------------------------------------------------------------

def test_build_profile_from_config_full() -> None:
    cfg = {
        "user_profile": {
            "current_title": "VP Engineering",
            "years_experience": 20,
            "clearance": "Top Secret",
            "location": "Miami, FL",
            "open_to_relocation": ["remote"],
            "us_citizen": True,
        },
        "target_roles": ["Director", "VP"],
        "reject_roles": ["Software Engineer"],
        "compensation_thresholds": {
            "remote": {"min_comp": 200000},
            "federal_ses": {"action": "always_apply"},
        },
    }
    user_profile, target_roles, reject_roles, comp_rules = _build_profile_from_config(cfg)
    assert "VP Engineering" in user_profile
    assert "Top Secret" in user_profile
    assert "Director" in target_roles
    assert "Software Engineer" in reject_roles
    assert "200,000" in comp_rules
    assert "ALWAYS APPLY" in comp_rules


def test_build_profile_empty_config_uses_defaults() -> None:
    """Empty config falls back to sensible defaults — no KeyError."""
    user_profile, target_roles, reject_roles, comp_rules = _build_profile_from_config({})
    assert len(user_profile) > 10
    assert len(target_roles) > 10
    assert len(reject_roles) > 10
    assert len(comp_rules) > 10


# ---------------------------------------------------------------------------
# _smart_excerpt
# ---------------------------------------------------------------------------

def test_smart_excerpt_short_passthrough() -> None:
    text = "A short description."
    assert _smart_excerpt(text) == text


def test_smart_excerpt_long_uses_head_tail() -> None:
    text = "A" * 2000 + "B" * 1000
    result = _smart_excerpt(text, max_chars=2500)
    assert len(result) <= 2500 + 30  # allow for ellipsis marker
    assert result.startswith("A" * 100)
    assert "B" in result  # tail is included


def test_smart_excerpt_empty() -> None:
    assert _smart_excerpt("") == ""


# ---------------------------------------------------------------------------
# score() — IC fast-path and model error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_ic_role_fast_path() -> None:
    scorer = _make_scorer()
    score, reason, flags, action = await scorer.score({"title": "Software Engineer", "description": ""})
    assert score == 5
    assert "IC_ROLE" in flags
    assert action == "skip"


@pytest.mark.asyncio
async def test_score_model_unavailable_returns_review() -> None:
    scorer = _make_scorer()
    scorer._model_client.complete = AsyncMock(return_value="No model available: Ollama is not running")
    score, reason, flags, action = await scorer.score({"title": "Director of Engineering", "description": ""})
    assert score == 50
    assert flags == "FLAG_FOR_REVIEW"


# ---------------------------------------------------------------------------
# batch_score() — concurrency and exception isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_score_parallel() -> None:
    scorer = _make_scorer()
    call_count = 0

    async def mock_score(job):
        nonlocal call_count
        call_count += 1
        return 75, "Good fit", "CLEARED_ROLE", "apply"

    scorer.score = mock_score  # type: ignore[method-assign]
    jobs = [{"title": f"Job {i}", "description": ""} for i in range(10)]
    results = await scorer.batch_score(jobs, concurrency=5)
    assert len(results) == 10
    assert call_count == 10
    assert all(j["score"] == 75 for j in results)


@pytest.mark.asyncio
async def test_batch_score_isolates_exceptions() -> None:
    scorer = _make_scorer()
    call_count = 0

    async def mock_score(job):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise RuntimeError("transient failure")
        return 80, "OK", "", "apply"

    scorer.score = mock_score  # type: ignore[method-assign]
    jobs = [{"title": f"Job {i}", "description": ""} for i in range(5)]
    results = await scorer.batch_score(jobs, concurrency=5)
    assert len(results) == 5
    failed = [j for j in results if j.get("flags") == "FLAG_FOR_REVIEW"]
    assert len(failed) == 1
