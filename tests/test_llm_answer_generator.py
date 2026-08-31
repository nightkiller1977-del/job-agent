"""Unit tests for Grounded LLM Answer Generator."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.answers.llm_answer_generator import LLMAnswerGenerator, normalize_question


def test_normalize_question():
    assert normalize_question("Why do you want to work here?") == "why do you want to work here"
    assert normalize_question("  What is your experience with Python?! * ") == "what is your experience with python"


def test_blacklist_blocks_sensitive_questions():
    generator = LLMAnswerGenerator(profile_data={})
    blacklisted = [
        "Are you legally authorized to work in the US?",
        "Will you now or in the future require visa sponsorship?",
        "What is your desired salary?",
        "Do you have a security clearance?",
        "Please identify your gender",
        "Are you willing to relocate?",
        "Have you ever been convicted of a felony?",
    ]
    for q in blacklisted:
        assert generator.is_blacklisted(q) is True
        assert generator.generate_answer(q) is None


def test_grounded_answer_generation_and_caching(tmp_path):
    cache_file = tmp_path / "test_cache.json"
    profile = {
        "work_history": [
            {
                "company": "Acme Corp",
                "role": "VP Engineering",
                "bullets": [
                    "Scaled engineering team from 10 to 50 engineers",
                    "Architected high-throughput cloud streaming service",
                ],
            }
        ],
        "skills": ["Python", "Go", "Distributed Systems"],
    }
    generator = LLMAnswerGenerator(profile_data=profile, cache_path=cache_file)

    mock_llm_response = "At Acme Corp, I scaled our engineering team from 10 to 50 engineers. I also architected a high-throughput distributed cloud streaming platform using Python and Go."

    with patch.object(generator, "_call_model_cascade", return_value=mock_llm_response) as mock_call:
        job = {"job_id": "job_123", "company": "TechCorp", "title": "Director of Engineering"}
        ans = generator.generate_answer("Tell us about a major engineering milestone you led?", job=job)
        assert ans is not None
        assert "Acme Corp" in ans
        assert mock_call.call_count == 1

        # Second call with normalized variation should hit cache
        ans2 = generator.generate_answer("Tell us about a major engineering milestone you led?!*", job=job)
        assert ans2 == ans
        assert mock_call.call_count == 1  # No second LLM call


def test_validator_rejects_hallucinated_employer(tmp_path):
    cache_file = tmp_path / "test_cache2.json"
    profile = {
        "work_history": [{"company": "Acme Corp", "role": "VP Engineering", "bullets": ["Led engineering"]}],
        "skills": ["Python"],
    }
    generator = LLMAnswerGenerator(profile_data=profile, cache_path=cache_file)
    # Model hallucinates working at Netflix and leading AI systems
    hallucinated_response = "At Netflix, I led the core video infrastructure team. We redesigned the recommendation pipeline."

    with patch.object(generator, "_call_model_cascade", return_value=hallucinated_response):
        ans = generator.generate_answer("Describe your engineering background?", job={"job_id": "j1"})
        assert ans is None  # Must reject unsourced entity Netflix


def test_validator_rejects_hallucinated_metrics(tmp_path):
    cache_file = tmp_path / "test_cache3.json"
    profile = {
        "work_history": [{"company": "Acme Corp", "role": "VP Engineering", "bullets": ["Improved performance"]}],
        "skills": ["Python"],
    }
    generator = LLMAnswerGenerator(profile_data=profile, cache_path=cache_file)
    # Model hallucinates 900% and $50M
    hallucinated_response = "At Acme Corp, I increased revenue by 900%. We managed a budget of $50M."

    with patch.object(generator, "_call_model_cascade", return_value=hallucinated_response):
        ans = generator.generate_answer("What was your impact?", job={"job_id": "j2"})
        assert ans is None  # Must reject unsourced 900% and $50M


def test_validator_rejects_more_than_three_sentences(tmp_path):
    cache_file = tmp_path / "test_cache4.json"
    profile = {
        "work_history": [{"company": "Acme Corp", "role": "VP Engineering", "bullets": ["Led engineering"]}],
        "skills": ["Python"],
    }
    generator = LLMAnswerGenerator(profile_data=profile, cache_path=cache_file)
    long_response = "Sentence one. Sentence two. Sentence three. Sentence four."

    with patch.object(generator, "_call_model_cascade", return_value=long_response):
        ans = generator.generate_answer("Tell us about your role?", job={"job_id": "j3"})
        assert ans is None  # Must strictly enforce <=3 sentences
