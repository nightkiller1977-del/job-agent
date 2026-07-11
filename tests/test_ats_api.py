import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from src.discovery.ats_api import (
    fetch_greenhouse_jobs,
    fetch_lever_jobs,
    fetch_ashby_jobs,
    _infer_remote_type,
    _extract_salary_from_text,
    canonicalize_url,
)

def test_canonicalize_url():
    # Strip tracking params
    assert canonicalize_url("https://boards.greenhouse.io/myco/jobs/123?utm_source=linkedin&ref=123") == "https://boards.greenhouse.io/myco/jobs/123"
    assert canonicalize_url("https://jobs.lever.co/myco/abc-123?lever-source=linkedin&subscription_id=abc") == "https://jobs.lever.co/myco/abc-123"
    # Keep legitimate params
    assert canonicalize_url("https://boards.greenhouse.io/embed/job_board?token=myco&id=123") == "https://boards.greenhouse.io/embed/job_board?token=myco&id=123"
    # Strip trailing slashes
    assert canonicalize_url("https://jobs.lever.co/myco/abc-123/") == "https://jobs.lever.co/myco/abc-123"
    # Case insensitivity for params
    assert canonicalize_url("https://jobs.ashbyhq.com/myco/123?UTM_SOURCE=indeed") == "https://jobs.ashbyhq.com/myco/123"

def test_infer_remote_type():
    assert _infer_remote_type("Software Engineer (Remote)", "San Francisco, CA", "Description here") == "remote"
    assert _infer_remote_type("Staff Engineer", "Austin, TX", "This is a hybrid role.") == "hybrid"
    assert _infer_remote_type("Manager", "In office, Miami", "No remote work.") == "onsite"
    assert _infer_remote_type("Principal Developer", "New York", "Generic listing") == "unknown"

def test_extract_salary_from_text():
    desc_html = "<p>The salary range for this role is $120,000 - $180,000 per year.</p>"
    assert _extract_salary_from_text(desc_html) == "$120,000 - $180,000"
    
    desc_k = "We offer $150k to $200k base salary."
    assert _extract_salary_from_text(desc_k) == "$150k to $200k"
    
    desc_none = "Great benefits but no compensation listed."
    assert _extract_salary_from_text(desc_none) == ""

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_greenhouse_jobs(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    mock_response = MagicMock()
    # Provide two jobs with identical canonicalized URLs (one has tracking query, one doesn't)
    mock_response.json.return_value = {
        "jobs": [
            {
                "id": 12345,
                "title": "Director of Engineering",
                "location": {"name": "Remote, USA"},
                "absolute_url": "https://boards.greenhouse.io/mycompany/jobs/12345?utm_source=linkedin",
                "content": "<h1>Job Description</h1><p>Salary range: $180,000 - $220,000</p>"
            },
            {
                "id": 12345,
                "title": "Director of Engineering (Duplicate)",
                "location": {"name": "Remote, USA"},
                "absolute_url": "https://boards.greenhouse.io/mycompany/jobs/12345",
                "content": "<h1>Job Description</h1>"
            }
        ]
    }
    mock_client.get = AsyncMock(return_value=mock_response)
    
    jobs = await fetch_greenhouse_jobs("mycompany")
    # Verify deduplication has filtered the second duplicate job out
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "greenhouse"
    assert job["title"] == "Director of Engineering"
    assert job["company"] == "Mycompany"
    assert job["remote_type"] == "remote"
    assert job["salary_raw"] == "$180,000 - $220,000"
    # Verify url is canonicalized
    assert job["url"] == "https://boards.greenhouse.io/mycompany/jobs/12345"

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_lever_jobs(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "id": "lever-abc",
            "title": "VP of Product",
            "categories": {
                "location": "San Francisco",
                "allLocations": ["San Francisco", "Remote"]
            },
            "descriptionHtml": "<p>VP of Product hybrid role</p>",
            "hostedUrl": "https://jobs.lever.co/myco/lever-abc?ref=123",
            "salary": {
                "min": 190000,
                "max": 240000,
                "currency": "USD"
            }
        }
    ]
    mock_client.get = AsyncMock(return_value=mock_response)
    
    jobs = await fetch_lever_jobs("myco")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "lever"
    assert job["title"] == "VP of Product"
    assert job["company"] == "Myco"
    assert job["remote_type"] == "hybrid"
    assert job["salary_raw"] == "USD 190000 - 240000"
    assert job["url"] == "https://jobs.lever.co/myco/lever-abc"

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_fetch_ashby_jobs(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "jobs": [
            {
                "id": "ashby-xyz",
                "title": "Engineering Manager",
                "location": "Remote - Europe",
                "descriptionHtml": "<p>Description plain text</p>",
                "jobUrl": "https://jobs.ashbyhq.com/myboard/ashby-xyz?ashby_source=indeed",
                "compensation": {
                    "compensationTierSummary": "$180K – $210K"
                }
            }
        ]
    }
    mock_client.get = AsyncMock(return_value=mock_response)
    
    jobs = await fetch_ashby_jobs("myboard")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "ashby"
    assert job["title"] == "Engineering Manager"
    assert job["company"] == "Myboard"
    assert job["remote_type"] == "remote"
    assert job["salary_raw"] == "$180K – $210K"
    assert job["url"] == "https://jobs.ashbyhq.com/myboard/ashby-xyz"
