import pytest
from unittest.mock import MagicMock, patch
from src.resume_helper import (
    check_ats_readability,
    KeywordCoverageError,
    PDFTextLayerError,
)

def test_ats_checker_success():
    """Verify that the check passes when all keywords are present in the PDF text layer."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "I am a Senior Software Engineer skilled in Python, Kubernetes, and Docker."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        result = check_ats_readability("dummy_resume.pdf", ["Python", "Docker", "Kubernetes"])
        assert result.passed is True
        assert result.coverage == 1.0
        assert result.matched_keywords == ["Python", "Docker", "Kubernetes"]

def test_ats_checker_normalized_keyword_coverage_passes_above_threshold():
    """Matching keywords use normalized coverage, not all profile/missing terms."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Senior developer with Python, Docker, and Kubernetes experience."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        result = check_ats_readability("dummy_resume.pdf", ["Python", "Docker", "Kubernetes", "React"], minimum_coverage=0.70)
        assert result.passed is True
        assert result.coverage == pytest.approx(3 / 4)
        assert result.unmatched_keywords == ["React"]

def test_ats_checker_keyword_coverage_error():
    """Verify low coverage raises a typed KeywordCoverageError with metrics."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Senior developer with Python and Docker experience."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        with pytest.raises(KeywordCoverageError) as exc:
            check_ats_readability("dummy_resume.pdf", ["Python", "React", "Kubernetes"], minimum_coverage=0.70)
        assert exc.value.result.failure_type == "keyword_coverage"
        assert exc.value.result.coverage == pytest.approx(1 / 3)
        assert exc.value.result.unmatched_keywords == ["React", "Kubernetes"]

def test_ats_checker_unreadable_pdf():
    """Verify that an empty or unextractable text layer raises PDFTextLayerError."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "   " # whitespace only
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        with pytest.raises(PDFTextLayerError) as exc:
            check_ats_readability("dummy_resume.pdf", ["Python"])
        assert "has no extractable text layer" in str(exc.value)
        assert exc.value.result.failure_type == "pdf_text_layer"

def test_ats_checker_special_characters_keywords():
    """Verify that technology keywords with special characters (C++, .NET, C#) are correctly matched."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Experienced C++ developer working with .NET Core and C#."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        result = check_ats_readability(
            "dummy_resume.pdf",
            ["C++", ".NET", "C#", "Go", "Java"],
            minimum_coverage=0.60
        )
        assert result.passed is True
        assert result.coverage == pytest.approx(3 / 5)
        assert set(result.matched_keywords) == {"C++", ".NET", "C#"}
        assert set(result.unmatched_keywords) == {"Go", "Java"}

def test_ats_checker_does_not_match_long_keywords_inside_other_words():
    """Verify that single-token keywords like React do not match reactive/reactor text."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Built reactive systems and reactor tooling with Python."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        with pytest.raises(KeywordCoverageError) as exc:
            check_ats_readability("dummy_resume.pdf", ["React"], minimum_coverage=1.0)
        assert exc.value.result.matched_keywords == []
        assert exc.value.result.unmatched_keywords == ["React"]
