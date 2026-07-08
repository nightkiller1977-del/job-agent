import pytest
from unittest.mock import MagicMock, patch
from src.resume_helper import check_ats_readability, ATSReadabilityError

def test_ats_checker_success():
    """Verify that the check passes when all keywords are present in the PDF text layer."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "I am a Senior Software Engineer skilled in Python, Kubernetes, and Docker."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        # Keywords exist in text. Should run without throwing.
        check_ats_readability("dummy_resume.pdf", ["Python", "Docker", "Kubernetes"])

def test_ats_checker_missing_keywords():
    """Verify that missing keywords cause an ATSReadabilityError to be raised."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Senior developer with Python and Docker experience."
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        # "React" is missing. Should raise exception.
        with pytest.raises(ATSReadabilityError) as exc:
            check_ats_readability("dummy_resume.pdf", ["Python", "React"])
        assert "missing critical target keywords: React" in str(exc.value)

def test_ats_checker_unreadable_pdf():
    """Verify that an empty or unextractable text layer raises an ATSReadabilityError."""
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "   " # whitespace only
    mock_reader.pages = [mock_page]

    with patch("src.resume_helper.PdfReader", return_value=mock_reader), patch("os.path.exists", return_value=True):
        with pytest.raises(ATSReadabilityError) as exc:
            check_ats_readability("dummy_resume.pdf", ["Python"])
        assert "has no extractable text layer" in str(exc.value)
