"""Tests for the "ats" discovery source (src/sources/ats_api.py) and its
orchestrator registration."""
import pytest
from unittest.mock import patch, AsyncMock

from src.sources.ats_api import AtsApiScraper, _parse_board_entry


def _job(job_id, title, source="greenhouse"):
    return {
        "job_id": job_id,
        "source": source,
        "title": title,
        "company": "Acme",
        "location": "Remote",
        "salary_raw": "",
        "remote_type": "remote",
        "url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "description": "",
        "status": "discovered",
    }


def test_parse_board_entry_accepts_string_and_dict():
    assert _parse_board_entry("stripe") == ("stripe", None)
    assert _parse_board_entry({"token": "reddit", "company": "Reddit"}) == ("reddit", "Reddit")
    # Empty token → entry is skipped by scrape() regardless of company
    assert _parse_board_entry({"company": "NoToken"})[0] == ""
    assert _parse_board_entry(42) == ("", None)


@pytest.mark.asyncio
async def test_scrape_no_boards_configured_returns_empty():
    jobs = await AtsApiScraper({"search_settings": {}}).scrape()
    assert jobs == []


@pytest.mark.asyncio
@patch("src.sources.ats_api.fetch_greenhouse_jobs", new_callable=AsyncMock)
async def test_scrape_stamps_discovered_at_and_dedupes(mock_gh):
    # Same job returned by two configured boards → one survives
    mock_gh.side_effect = [
        [_job("aaa", "Engineering Manager"), _job("bbb", "Director of Engineering")],
        [_job("aaa", "Engineering Manager")],
    ]
    config = {"ats_boards": {"greenhouse": ["acme", "acme-alt"]}, "search_settings": {}}
    jobs = await AtsApiScraper(config).scrape()
    assert [j["job_id"] for j in jobs] == ["aaa", "bbb"]
    assert all(j["discovered_at"] for j in jobs)


@pytest.mark.asyncio
@patch("src.sources.ats_api.fetch_greenhouse_jobs", new_callable=AsyncMock)
async def test_scrape_title_include_filter(mock_gh):
    mock_gh.return_value = [
        _job("aaa", "Engineering Manager"),
        _job("bbb", "Software Engineer II"),
        _job("ccc", "Director, Platform"),
    ]
    config = {
        "ats_boards": {"greenhouse": ["acme"], "title_include": ["manager", "director"]},
        "search_settings": {},
    }
    jobs = await AtsApiScraper(config).scrape()
    assert [j["job_id"] for j in jobs] == ["aaa", "ccc"]


@pytest.mark.asyncio
@patch("src.sources.ats_api.fetch_lever_jobs", new_callable=AsyncMock)
@patch("src.sources.ats_api.fetch_greenhouse_jobs", new_callable=AsyncMock)
async def test_one_board_failure_does_not_sink_batch(mock_gh, mock_lever):
    mock_gh.side_effect = RuntimeError("boom")
    mock_lever.return_value = [_job("ddd", "VP of Engineering", source="lever")]
    config = {"ats_boards": {"greenhouse": ["acme"], "lever": ["acme"]}, "search_settings": {}}
    jobs = await AtsApiScraper(config).scrape()
    assert [j["job_id"] for j in jobs] == ["ddd"]


def test_orchestrator_registration():
    from src.orchestrator import SOURCE_MAP, DEFAULT_DISCOVERY_SOURCES
    from src.sources.jobright import JobrightScraper

    assert SOURCE_MAP["ats"] is AtsApiScraper
    # ATS-discovered jobs apply through the external-ATS flow
    for vendor in ("greenhouse", "lever", "ashby"):
        assert SOURCE_MAP[vendor] is JobrightScraper
    # Source-first routing: ats runs before browser sources
    assert DEFAULT_DISCOVERY_SOURCES[0] == "ats"


@pytest.mark.asyncio
async def test_adapter_registry_default_is_on(monkeypatch, tmp_path):
    """With USE_ADAPTER_REGISTRY unset, apply_external_ats_job must route via
    ExternalApplySession (the adapter registry is the default path)."""
    from src.sources import jobright as jr

    monkeypatch.delenv("USE_ADAPTER_REGISTRY", raising=False)

    class _Result:
        status = "submitted"
        detail = ""
        analytics = {}
        submitted = True

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def apply(self, job, auto_submit=False):
            return _Result()

    monkeypatch.setattr("src.sources.adapters.session.ExternalApplySession", _FakeSession)
    scraper = jr.JobrightScraper({"search_settings": {}})
    submitted = await scraper.apply_external_ats_job(
        {"job_id": "x", "title": "t", "company": "c"},
        "https://boards.greenhouse.io/acme/jobs/1",
    )
    assert submitted is True
    assert scraper.last_apply_status == "submitted"


@pytest.mark.asyncio
async def test_adapter_registry_opt_out(monkeypatch):
    """USE_ADAPTER_REGISTRY=0 must NOT construct ExternalApplySession."""
    from src.sources import jobright as jr

    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "0")

    class _Explode:
        def __init__(self, *a, **kw):
            raise AssertionError("legacy path must not construct ExternalApplySession")

    monkeypatch.setattr("src.sources.adapters.session.ExternalApplySession", _Explode)
    scraper = jr.JobrightScraper({"search_settings": {}})
    # Legacy body launches a real browser; stub it out — reaching it (instead of
    # ExternalApplySession) is the assertion.
    async def _no_browser(*a, **kw):
        raise RuntimeError("stop before browser")

    scraper._start_browser = _no_browser
    with pytest.raises(RuntimeError, match="stop before browser"):
        await scraper.apply_external_ats_job(
            {"job_id": "x", "title": "t", "company": "c"},
            "https://boards.greenhouse.io/acme/jobs/1",
        )
