from __future__ import annotations

import json

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.sources.base import JobExpiredError
from src.sources.builtin import (
    BuiltInScraper,
    BUILTIN_BASE,
    BUILTIN_JOBS_URL,
    _extract_job_post_init,
    _extract_ld_json_posting,
    _format_location,
    _format_salary,
    _looks_like_cf_challenge,
)


def _fast_config(**search_settings) -> dict:
    settings = {"delay_min_seconds": 0, "delay_max_seconds": 0, "max_jobs_per_source": 50}
    settings.update(search_settings)
    return {"search_settings": settings}


def _listing_html(paths: list[str]) -> str:
    cards = "".join(f'<a data-alias="{p}" href="{p}">Job</a>' for p in paths)
    return f"<html><body><div class='jobs-list'>{cards}</div></body></html>"


def _job_post_init_blob(job_id: int, company: str, how_to_apply: str, is_easy_apply: bool = True,
                         extra_nested: dict | None = None) -> str:
    job = {
        "id": job_id,
        "companyName": company,
        "howToApply": how_to_apply,
        "isEasyApply": is_easy_apply,
        "isApplyFormEnabled": False,
    }
    if extra_nested is not None:
        job["salesforceData"] = extra_nested
    return f"Builtin.jobPostInit({json.dumps({'job': job})});"


def _ld_json_posting(title: str, description: str, job_location, entity_encoded: bool = False,
                      base_salary: dict | None = None, wrap_graph: bool = True) -> str:
    posting = {"@type": "JobPosting", "title": title, "description": description, "jobLocation": job_location}
    if base_salary is not None:
        posting["baseSalary"] = base_salary
    payload = {"@context": "https://schema.org", "@graph": [posting]} if wrap_graph else posting
    type_attr = "application/ld&#x2B;json" if entity_encoded else "application/ld+json"
    return f'<script type="{type_attr}">{json.dumps(payload)}</script>'


def _detail_html(job_id: int, company: str, how_to_apply: str, title: str, job_location,
                  description: str = "<p>Great role.</p>", base_salary=None,
                  entity_encoded: bool = False, wrap_graph: bool = True,
                  extra_nested: dict | None = None) -> str:
    return (
        "<html><head>"
        + _ld_json_posting(title, description, job_location, entity_encoded, base_salary, wrap_graph)
        + "</head><body><script>"
        + _job_post_init_blob(job_id, company, how_to_apply, extra_nested=extra_nested)
        + "</script></body></html>"
    )


def _cf_challenge_response(url: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        200, request=request, headers={"cf-mitigated": "challenge"},
        text="<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>",
    )


# ----------------------------------------------------------------------
# Blob extraction
# ----------------------------------------------------------------------

def test_extract_job_post_init_parses_nested_braces():
    html_text = _job_post_init_blob(
        123, "Acme", "https://boards.greenhouse.io/acme/jobs/456",
        extra_nested={"orgId": "x", "nested": {"a": 1}},
    )

    data = _extract_job_post_init(html_text)

    assert data is not None
    assert data["job"]["id"] == 123
    assert data["job"]["companyName"] == "Acme"
    assert data["job"]["howToApply"] == "https://boards.greenhouse.io/acme/jobs/456"
    assert data["job"]["salesforceData"] == {"orgId": "x", "nested": {"a": 1}}


def test_extract_job_post_init_returns_none_when_absent():
    assert _extract_job_post_init("<html>no blob here</html>") is None


def test_extract_ld_json_posting_handles_entity_encoded_type_attr():
    html_text = _ld_json_posting(
        "Senior Engineer", "<p>Build things.</p>",
        {"address": {"addressLocality": "New York", "addressRegion": "NY", "addressCountry": "US"}},
        entity_encoded=True,
    )

    posting = _extract_ld_json_posting(html_text)

    assert posting.get("title") == "Senior Engineer"
    assert posting.get("jobLocation", {}).get("address", {}).get("addressLocality") == "New York"


def test_extract_ld_json_posting_handles_bare_dict_without_graph():
    html_text = _ld_json_posting(
        "Remote Engineer", "desc",
        {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
        wrap_graph=False,
    )

    posting = _extract_ld_json_posting(html_text)

    assert posting.get("title") == "Remote Engineer"


def test_extract_ld_json_posting_handles_location_list():
    locations = [
        {"address": {"addressLocality": "Austin", "addressRegion": "TX"}},
        {"address": {"addressLocality": "Denver", "addressRegion": "CO"}},
    ]
    html_text = _ld_json_posting("Multi-city Engineer", "desc", locations)

    posting = _extract_ld_json_posting(html_text)

    assert _format_location(posting.get("jobLocation")) == "Austin, TX; Denver, CO"


def test_format_location_handles_missing_data():
    assert _format_location(None) == "Not specified"
    assert _format_location({}) == "Not specified"


def test_format_salary_formats_range_and_single_value():
    assert _format_salary({"currency": "USD", "value": {"minValue": 180000, "maxValue": 220000, "unitText": "YEAR"}}) \
        == "USD 180,000 - 220,000/YEAR"
    assert _format_salary({"currency": "USD", "value": {"minValue": 90000, "unitText": "YEAR"}}) \
        == "USD 90,000/YEAR"
    assert _format_salary(None) == ""
    assert _format_salary({"value": {}}) == ""


def test_looks_like_cf_challenge_detects_block_markers():
    resp = _cf_challenge_response("https://builtin.com/jobs?page=1")
    assert _looks_like_cf_challenge(resp) is True

    request = httpx.Request("GET", "https://builtin.com/job/engineer/1")
    real_resp = httpx.Response(200, request=request, text=_detail_html(
        1, "Acme", "https://boards.greenhouse.io/acme/jobs/1", "Engineer",
        {"address": {"addressLocality": "NYC"}},
    ))
    assert _looks_like_cf_challenge(real_resp) is False


# ----------------------------------------------------------------------
# Detail-page field mapping
# ----------------------------------------------------------------------

def test_parse_detail_page_maps_fields_correctly():
    scraper = BuiltInScraper(config=_fast_config())
    url = "https://builtin.com/job/senior-engineer/42"
    html_text = _detail_html(
        42, "Acme Corp", "https://boards.greenhouse.io/acme/jobs/42", "Senior Engineer",
        {"address": {"addressLocality": "New York", "addressRegion": "NY"}},
        description="<p>Build <b>great</b> things.</p>",
        base_salary={"currency": "USD", "value": {"minValue": 180000, "maxValue": 220000, "unitText": "YEAR"}},
    )

    job = scraper._parse_detail_page(url, html_text)

    assert job is not None
    assert job["source"] == "builtin"
    assert job["title"] == "Senior Engineer"
    assert job["company"] == "Acme Corp"
    assert job["location"] == "New York, NY"
    assert job["description"] == "Build great things."
    assert job["salary_raw"] == "USD 180,000 - 220,000/YEAR"
    assert job["ats_url"] == "https://boards.greenhouse.io/acme/jobs/42"
    assert job["builtin_job_id"] == 42
    assert job["url"] == url
    assert job["job_id"] == scraper._make_job_id(url)


def test_parse_detail_page_returns_none_when_title_or_company_missing():
    scraper = BuiltInScraper(config=_fast_config())
    # No jobPostInit blob at all -> no companyName, no ld+json -> no title
    assert scraper._parse_detail_page("https://builtin.com/job/x/1", "<html>nothing here</html>") is None


# ----------------------------------------------------------------------
# scrape()
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_paginates_and_dedupes(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    request_page = lambda n: httpx.Request("GET", f"{BUILTIN_JOBS_URL}?page={n}")
    page1 = httpx.Response(200, request=request_page(1), text=_listing_html(["/job/a/1", "/job/b/2"]))
    page2 = httpx.Response(200, request=request_page(2), text=_listing_html(["/job/b/2", "/job/c/3"]))
    page3 = httpx.Response(200, request=request_page(3), text=_listing_html(["/job/a/1", "/job/b/2", "/job/c/3"]))

    detail_a = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_BASE}/job/a/1"), text=_detail_html(
        1, "Acme", "https://boards.greenhouse.io/acme/jobs/1", "Engineer A",
        {"address": {"addressLocality": "NYC"}},
    ))
    detail_b = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_BASE}/job/b/2"), text=_detail_html(
        2, "Acme", "https://boards.greenhouse.io/acme/jobs/2", "Engineer B",
        {"address": {"addressLocality": "NYC"}},
    ))
    detail_c = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_BASE}/job/c/3"), text=_detail_html(
        3, "Acme", "https://boards.greenhouse.io/acme/jobs/3", "Engineer C",
        {"address": {"addressLocality": "NYC"}},
    ))

    mock_client.get = AsyncMock(side_effect=[page1, page2, page3, detail_a, detail_b, detail_c])

    scraper = BuiltInScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert mock_client.get.call_count == 6  # 3 listing pages + 3 unique detail fetches (b/2 not re-fetched)
    assert len(jobs) == 3
    assert {j["title"] for j in jobs} == {"Engineer A", "Engineer B", "Engineer C"}


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_stops_on_cloudflare_challenge(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=_cf_challenge_response(f"{BUILTIN_JOBS_URL}?page=1"))

    scraper = BuiltInScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert jobs == []


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_scrape_aborts_after_consecutive_challenges(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    page1 = httpx.Response(
        200, request=httpx.Request("GET", f"{BUILTIN_JOBS_URL}?page=1"),
        text=_listing_html(["/job/a/1", "/job/b/2", "/job/c/3", "/job/d/4"]),
    )
    page2 = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_JOBS_URL}?page=2"), text=_listing_html([]))

    challenge = lambda n: _cf_challenge_response(f"{BUILTIN_BASE}/job/x/{n}")
    good_detail = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_BASE}/job/d/4"), text=_detail_html(
        4, "Acme", "https://boards.greenhouse.io/acme/jobs/4", "Engineer D",
        {"address": {"addressLocality": "NYC"}},
    ))

    mock_client.get = AsyncMock(side_effect=[page1, page2, challenge(1), challenge(2), challenge(3), good_detail])

    scraper = BuiltInScraper(config=_fast_config())
    jobs = await scraper.scrape()

    assert jobs == []
    # 2 listing fetches + 3 challenge detail fetches, then abort -> good_detail never requested
    assert mock_client.get.call_count == 5


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_no_disallowed_query_params_ever_constructed(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    page1 = httpx.Response(
        200, request=httpx.Request("GET", f"{BUILTIN_JOBS_URL}?page=1"),
        text=_listing_html(["/job/a/1"]),
    )
    page2 = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_JOBS_URL}?page=2"), text=_listing_html([]))
    detail_a = httpx.Response(200, request=httpx.Request("GET", f"{BUILTIN_BASE}/job/a/1"), text=_detail_html(
        1, "Acme", "https://boards.greenhouse.io/acme/jobs/1", "Engineer A",
        {"address": {"addressLocality": "NYC"}},
    ))
    mock_client.get = AsyncMock(side_effect=[page1, page2, detail_a])

    scraper = BuiltInScraper(config=_fast_config())
    await scraper.scrape()

    disallowed_markers = ["search=", "trending", "region_id=", "seattle", "san-francisco"]
    for call in mock_client.get.call_args_list:
        url = call.args[0]
        assert url.startswith(f"{BUILTIN_BASE}/jobs?page=") or url.startswith(f"{BUILTIN_BASE}/job/")
        assert not any(m in url for m in disallowed_markers)


# ----------------------------------------------------------------------
# apply()
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_apply_delegates_to_jobright_external_ats(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    detail_html = _detail_html(
        42, "Acme", "https://boards.greenhouse.io/acme/jobs/42", "Engineer",
        {"address": {"addressLocality": "NYC"}},
    )
    mock_client.get = AsyncMock(return_value=httpx.Response(
        200, request=httpx.Request("GET", "https://builtin.com/job/engineer/42"), text=detail_html,
    ))

    scraper = BuiltInScraper(config=_fast_config())
    job = {
        "job_id": "j1", "title": "Engineer", "company": "Acme",
        "url": "https://builtin.com/job/engineer/42",
        "ats_url": "https://boards.greenhouse.io/acme/jobs/stale",
    }

    async def fake_apply(self, job, external_url, *, auto_submit=False, **kwargs):
        self.last_apply_status = "submitted"
        self.last_apply_detail = "ok"
        self.last_apply_ats_url = external_url
        self._apply_analytics = {"ats_score": 88}
        return True

    with patch("src.sources.jobright.JobrightScraper.apply_external_ats_job", new=fake_apply):
        result = await scraper.apply(job, auto_submit=False)

    assert result is True
    assert scraper.last_apply_status == "submitted"
    # Fresh URL from the re-fetch wins over the stale stashed one.
    assert scraper.last_apply_ats_url == "https://boards.greenhouse.io/acme/jobs/42"


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_apply_falls_back_to_stashed_ats_url_on_refetch_failure(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=_cf_challenge_response("https://builtin.com/job/engineer/42"))

    scraper = BuiltInScraper(config=_fast_config())
    job = {
        "job_id": "j1", "title": "Engineer", "company": "Acme",
        "url": "https://builtin.com/job/engineer/42",
        "extra_json": json.dumps({"ats_url": "https://boards.greenhouse.io/acme/jobs/from-db"}),
    }

    async def fake_apply(self, job, external_url, *, auto_submit=False, **kwargs):
        self.last_apply_status = "submitted"
        self.last_apply_ats_url = external_url
        return True

    with patch("src.sources.jobright.JobrightScraper.apply_external_ats_job", new=fake_apply):
        result = await scraper.apply(job, auto_submit=False)

    assert result is True
    assert scraper.last_apply_ats_url == "https://boards.greenhouse.io/acme/jobs/from-db"


@pytest.mark.asyncio
@patch("httpx.AsyncClient")
async def test_apply_raises_job_expired_on_404(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get = AsyncMock(return_value=httpx.Response(
        404, request=httpx.Request("GET", "https://builtin.com/job/gone/1"),
    ))

    scraper = BuiltInScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme", "url": "https://builtin.com/job/gone/1"}

    with pytest.raises(JobExpiredError):
        await scraper.apply(job, auto_submit=False)


@pytest.mark.asyncio
async def test_apply_via_ats_no_ext_url_returns_blocked_status():
    scraper = BuiltInScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme", "url": "https://builtin.com/job/engineer/1"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job", new=AsyncMock()
    ) as mocked:
        result = await scraper._apply_via_ats(job, "", auto_submit=False)

    assert result is False
    assert scraper.last_apply_status == "builtin_no_ats_url"
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_apply_via_ats_reraises_pdf_text_layer_error():
    from src.resume_helper import ATSValidationResult, PDFTextLayerError

    result = ATSValidationResult(
        passed=False, coverage=0.0, matched_keywords=[], unmatched_keywords=[],
        failure_type="pdf_text_layer", detail="Generated PDF has no extractable text layer.",
    )
    scraper = BuiltInScraper(config=_fast_config())
    job = {"job_id": "j1", "title": "Engineer", "company": "Acme"}

    with patch(
        "src.sources.jobright.JobrightScraper.apply_external_ats_job",
        new=AsyncMock(side_effect=PDFTextLayerError(result)),
    ):
        with pytest.raises(PDFTextLayerError):
            await scraper._apply_via_ats(job, "https://boards.greenhouse.io/acme/jobs/1", auto_submit=False)
