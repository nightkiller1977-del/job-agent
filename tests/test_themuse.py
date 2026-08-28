import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.sources.themuse import (
    TheMuseScraper,
    THEMUSE_API_URL,
    _html_to_text,
    _infer_remote_type,
)


def _sample_item(job_id: int, url: str, title: str = "Senior Software Engineer",
                  company: str = "Acme Corp", location: str = "New York, NY",
                  contents: str = "<p>Join our <b>team</b> &amp; build great things.</p>") -> dict:
    return {
        "id": job_id,
        "name": title,
        "type": "external",
        "company": {"id": 1, "name": company},
        "locations": [{"name": location}] if location else [],
        "levels": [{"name": "Senior Level"}],
        "categories": [{"name": "Engineering"}],
        "refs": {"landing_page": url},
        "contents": contents,
    }


def _api_response(status_code: int, page: int = 0, page_count: int = 1, results=None) -> httpx.Response:
    request = httpx.Request("GET", THEMUSE_API_URL)
    body = {"page": page, "page_count": page_count, "total": len(results or []), "results": results or []}
    return httpx.Response(status_code, request=request, json=body)


def _fast_config(**search_settings) -> dict:
    settings = {"delay_min_seconds": 0, "delay_max_seconds": 0, "max_jobs_per_source": 50}
    settings.update(search_settings)
    return {"search_settings": settings}


# ----------------------------------------------------------------------
# Helpers / field mapping
# ----------------------------------------------------------------------

def test_html_to_text_strips_tags_and_unescapes_entities():
    assert _html_to_text("<p>Come join <b>Acme</b> &amp; friends</p>") == "Come join Acme & friends"


def test_html_to_text_handles_empty_input():
    assert _html_to_text("") == ""
    assert _html_to_text(None) == ""


def test_infer_remote_type_variants():
    assert _infer_remote_type("Software Engineer (Remote)", "San Francisco, CA", "Description here") == "remote"
    assert _infer_remote_type("Staff Engineer", "Austin, TX", "This is a hybrid role.") == "hybrid"
    assert _infer_remote_type("Manager", "In office, Miami", "No remote work.") == "onsite"
    assert _infer_remote_type("Principal Developer", "New York", "Generic listing") == "unknown"


def test_map_job_basic_fields():
    scraper = TheMuseScraper(config=_fast_config())
    item = _sample_item(111, "https://www.themuse.com/jobs/acme/senior-software-engineer")

    job = scraper._map_job(item, terms=[])

    assert job is not None
    assert job["source"] == "themuse"
    assert job["title"] == "Senior Software Engineer"
    assert job["company"] == "Acme Corp"
    assert job["location"] == "New York, NY"
    assert job["url"] == "https://www.themuse.com/jobs/acme/senior-software-engineer"
    assert job["description"] == "Join our team & build great things."
    assert job["salary_raw"] == ""
    assert job["themuse_id"] == 111
    assert job["job_id"] == scraper._make_job_id(item["refs"]["landing_page"])


def test_map_job_missing_landing_page_returns_none():
    scraper = TheMuseScraper(config=_fast_config())
    item = _sample_item(111, "")
    item["refs"] = {}

    assert scraper._map_job(item, terms=[]) is None


def test_map_job_keyword_filter_matches_title_or_description():
    scraper = TheMuseScraper(config=_fast_config())
    matching = _sample_item(1, "https://www.themuse.com/jobs/acme/eng-mgr", title="Engineering Manager")
    non_matching = _sample_item(2, "https://www.themuse.com/jobs/acme/sales", title="Sales Representative",
                                 contents="<p>Sell things.</p>")

    terms = ["engineering"]
    assert scraper._map_job(matching, terms) is not None
    assert scraper._map_job(non_matching, terms) is None


# ----------------------------------------------------------------------
# scrape()
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_paginates_until_page_count(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    resp0 = _api_response(200, page=0, page_count=2, results=[
        _sample_item(1, "https://www.themuse.com/jobs/acme/job-1"),
    ])
    resp1 = _api_response(200, page=1, page_count=2, results=[
        _sample_item(2, "https://www.themuse.com/jobs/acme/job-2"),
    ])
    mock_client.get = AsyncMock(side_effect=[resp0, resp1])

    scraper = TheMuseScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert mock_client.get.call_count == 2
    assert len(jobs) == 2


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_respects_max_jobs_per_source(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    resp = _api_response(200, page=0, page_count=1, results=[
        _sample_item(1, "https://www.themuse.com/jobs/acme/job-1"),
        _sample_item(2, "https://www.themuse.com/jobs/acme/job-2"),
        _sample_item(3, "https://www.themuse.com/jobs/acme/job-3"),
    ])
    mock_client.get = AsyncMock(return_value=resp)

    scraper = TheMuseScraper(config=_fast_config(max_jobs_per_source=1))
    jobs = await scraper.scrape()

    assert len(jobs) == 1


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_applies_keyword_filter(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    resp = _api_response(200, page=0, page_count=1, results=[
        _sample_item(1, "https://www.themuse.com/jobs/acme/eng-mgr", title="Engineering Manager"),
        _sample_item(2, "https://www.themuse.com/jobs/acme/sales", title="Sales Representative",
                     contents="<p>Sell things.</p>"),
    ])
    mock_client.get = AsyncMock(return_value=resp)

    scraper = TheMuseScraper(config=_fast_config(keywords="engineering"))
    jobs = await scraper.scrape()

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Engineering Manager"


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_deduplicates_by_job_id(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    same_url = "https://www.themuse.com/jobs/acme/job-1"
    resp = _api_response(200, page=0, page_count=1, results=[
        _sample_item(1, same_url),
        _sample_item(2, same_url),  # duplicate landing page -> duplicate job_id
    ])
    mock_client.get = AsyncMock(return_value=resp)

    scraper = TheMuseScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert len(jobs) == 1


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_stops_gracefully_on_429(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    resp = _api_response(429, page=0, page_count=1, results=[])
    mock_client.get = AsyncMock(return_value=resp)

    scraper = TheMuseScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert jobs == []


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_includes_api_key_when_env_set(mock_client_class, monkeypatch):
    monkeypatch.setenv("THEMUSE_API_KEY", "abc123")
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=_api_response(200, results=[]))

    scraper = TheMuseScraper(config=_fast_config())
    await scraper.scrape()

    _, kwargs = mock_client.get.call_args
    assert kwargs["params"]["api_key"] == "abc123"


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_omits_api_key_when_env_unset(mock_client_class, monkeypatch):
    monkeypatch.delenv("THEMUSE_API_KEY", raising=False)
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=_api_response(200, results=[]))

    scraper = TheMuseScraper(config=_fast_config())
    await scraper.scrape()

    _, kwargs = mock_client.get.call_args
    assert "api_key" not in kwargs["params"]


# ----------------------------------------------------------------------
# apply() delegation contract
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_via_ats_delegates_to_jobright_and_propagates_status():
    scraper = TheMuseScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme",
           "url": "https://www.themuse.com/jobs/acme/engineer"}

    async def fake_apply(self, job, external_url, *, auto_submit=False, **kwargs):
        self.last_apply_status = "submitted"
        self.last_apply_detail = "Application submitted."
        self.last_apply_ats_url = external_url
        self._apply_analytics = {"ats_score": 91}
        return True

    with patch("src.sources.jobright.JobrightScraper.apply_external_ats_job", new=fake_apply):
        result = await scraper._apply_via_ats(
            job, "https://acme.myworkday.com/careers/job/123", auto_submit=False
        )

    assert result is True
    assert scraper.last_apply_status == "submitted"
    assert scraper.last_apply_detail == "Application submitted."
    assert scraper.last_apply_ats_url == "https://acme.myworkday.com/careers/job/123"
    assert scraper._apply_analytics == {"ats_score": 91}


@pytest.mark.asyncio
async def test_apply_via_ats_no_ext_url_returns_blocked_status():
    scraper = TheMuseScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme",
           "url": "https://www.themuse.com/jobs/acme/engineer"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job", new=AsyncMock()
    ) as mocked:
        result = await scraper._apply_via_ats(job, "", auto_submit=False)

    assert result is False
    assert scraper.last_apply_status == "themuse_no_ats_url"
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_apply_via_ats_reraises_pdf_text_layer_error():
    from src.resume_helper import ATSValidationResult, PDFTextLayerError

    result = ATSValidationResult(
        passed=False, coverage=0.0, matched_keywords=[], unmatched_keywords=[],
        failure_type="pdf_text_layer", detail="Generated PDF has no extractable text layer.",
    )
    scraper = TheMuseScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job",
        new=AsyncMock(side_effect=PDFTextLayerError(result)),
    ):
        with pytest.raises(PDFTextLayerError):
            await scraper._apply_via_ats(job, "https://boards.greenhouse.io/acme/jobs/1", auto_submit=False)


@pytest.mark.asyncio
async def test_apply_via_ats_swallows_other_errors():
    scraper = TheMuseScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job",
        new=AsyncMock(side_effect=RuntimeError("portal timed out")),
    ):
        result = await scraper._apply_via_ats(job, "https://boards.greenhouse.io/acme/jobs/1", auto_submit=False)

    assert result is False
    assert scraper.last_apply_status == "themuse_external_apply_error"
