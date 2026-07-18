import pytest
from src.adapters_patterns.ats_selectors import SELECTORS, QUESTION_PATTERNS

def test_selectors_structure():
    # Verify presence of major platforms
    assert "greenhouse" in SELECTORS
    assert "lever" in SELECTORS
    assert "ashby" in SELECTORS
    assert "workday" in SELECTORS
    
    # Check Greenhouse elements
    gh = SELECTORS["greenhouse"]
    assert "submit_button" in gh
    assert "file_inputs" in gh
    assert "fields" in gh
    assert "first_name" in gh["fields"]
    
    # Check Lever elements
    lever = SELECTORS["lever"]
    assert "submit_button" in lever
    assert "file_inputs" in lever
    assert "fields" in lever
    assert "email" in lever["fields"]

    # Check Ashby elements
    ashby = SELECTORS["ashby"]
    assert "submit_button" in ashby
    assert "fields" in ashby

    # Check Workday elements
    workday = SELECTORS["workday"]
    assert "submit_button" in workday
    assert "fields" in workday

def test_question_patterns_structure():
    assert "work_auth" in QUESTION_PATTERNS
    assert "sponsorship" in QUESTION_PATTERNS
    assert "salary" in QUESTION_PATTERNS
    assert "eeo_gender" in QUESTION_PATTERNS
    assert "eeo_race" in QUESTION_PATTERNS
    assert "eeo_veteran" in QUESTION_PATTERNS
    assert "eeo_disability" in QUESTION_PATTERNS
    
    # Ensure patterns are regex compiled or raw strings list
    for category, patterns in QUESTION_PATTERNS.items():
        assert isinstance(patterns, list)
        assert len(patterns) > 0
        for pat in patterns:
            assert isinstance(pat, str)


def test_submit_buttons_exclude_cta_entry_classes():
    # entry CTAs like `.careersite-button` (Teamtailor Apply) must never sit in a
    # vendor's submit_button list — the live path prepends these before final-submit
    # candidates and can click a non-final CTA, marking a job submitted prematurely.
    banned = ("careersite-button", "apply-button", "btn-apply")
    for vendor, spec in SELECTORS.items():
        for sel in spec.get("submit_button", []):
            low = sel.lower()
            assert not any(b in low for b in banned), f"{vendor}: {sel}"
