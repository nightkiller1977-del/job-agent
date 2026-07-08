from __future__ import annotations
import os
from unittest.mock import AsyncMock, patch, MagicMock
from pathlib import Path
import pytest
from src.latex_compiler import LaTeXCompiler

@pytest.mark.asyncio
async def test_latex_compiler_fallback_html():
    """Test that LaTeXCompiler uses the HTML fallback when no LaTeX tools are found."""
    compiler = LaTeXCompiler()
    
    with patch("shutil.which", return_value=None):
        assert compiler.has_latex_compiler() is None
        
        # Mock Playwright Page and context structure
        mock_page = AsyncMock()
        mock_pdf_page = AsyncMock()
        mock_page.context.new_page.return_value = mock_pdf_page
        
        profile = {
            "personal_info": {
                "first_name": "John",
                "last_name": "Doe",
                "email": "john.doe@example.com",
                "phone": "123-456-7890",
                "city": "Miami",
                "state": "FL"
            },
            "skills": ["Python", "FastAPI"],
            "education": [
                {
                    "school_name": "Test University",
                    "degree": "B.S.",
                    "major": "Computer Science",
                    "start_date": "2018",
                    "end_date": "2022"
                }
            ],
            "work_history": []
        }
        
        tailored = {
            "tailored_summary": "Tailored executive summary.",
            "tailored_bullets": [
                {
                    "role": "Software Engineer",
                    "bullets": ["Developed FastAPI microservices.", "Optimized queries."]
                }
            ],
            "missing_keywords": ["Docker", "Kubernetes"]
        }
        
        temp_pdf_path = "state/tailored_resumes/test_fallback.pdf"
        
        result = await compiler.compile_cv(profile, tailored, temp_pdf_path, page=mock_page)
        
        assert result is True
        mock_page.context.new_page.assert_called_once()
        mock_pdf_page.set_content.assert_called_once()
        mock_pdf_page.pdf.assert_called_once_with(
            path=temp_pdf_path,
            format="Letter",
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"}
        )
        mock_pdf_page.close.assert_called_once()

@pytest.mark.asyncio
async def test_latex_cover_letter_fallback_html():
    """Test that cover letter generation falls back to HTML correctly."""
    compiler = LaTeXCompiler()
    
    with patch("shutil.which", return_value=None):
        mock_page = AsyncMock()
        mock_pdf_page = AsyncMock()
        mock_page.context.new_page.return_value = mock_pdf_page
        
        profile = {
            "personal_info": {"first_name": "John", "last_name": "Doe", "email": "john@doe.com"}
        }
        tailored = {
            "cover_letter": "This is a cover letter body."
        }
        job = {
            "title": "Staff Engineer",
            "company": "Tech Corp",
            "id": "12345"
        }
        
        temp_pdf_path = "state/tailored_resumes/test_cl_fallback.pdf"
        result = await compiler.compile_cover_letter(profile, tailored, job, temp_pdf_path, page=mock_page)
        
        assert result is True
        mock_pdf_page.set_content.assert_called_once()
        mock_pdf_page.pdf.assert_called_once()
