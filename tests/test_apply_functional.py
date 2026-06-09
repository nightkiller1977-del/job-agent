import os
import pytest
import re
from pathlib import Path
from unittest.mock import patch, AsyncMock
from playwright.async_api import Page, async_playwright
import playwright.async_api._generated

from src.sources.linkedin import LinkedInScraper
from src.sources.indeed import IndeedScraper
from src.sources.jobright import JobrightScraper

# ── Headless override fixture for mock tests ────────────────────────────────
@pytest.fixture(autouse=True)
def force_headless(request):
    """Force Playwright tests to run headlessly unless they are marked as live."""
    # Check if this test is marked as live
    is_live = "live" in request.node.keywords
    if is_live:
        # For live tests, don't override headless settings (let them use the scraper default, i.e. headed)
        yield
        return

    original_launch = playwright.async_api._generated.BrowserType.launch_persistent_context

    async def mock_launch_persistent_context(self, user_data_dir, **kwargs):
        kwargs["headless"] = True
        kwargs["slow_mo"] = 0
        return await original_launch(self, user_data_dir, **kwargs)

    with patch("playwright.async_api._generated.BrowserType.launch_persistent_context", mock_launch_persistent_context):
        yield


# ── Option 1: Mock LinkedIn Easy Apply E2E Test ─────────────────────────────
@pytest.mark.asyncio
async def test_mock_linkedin_easy_apply():
    # Setup mock profile variables
    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
        "linkedin_profile": "mock_profile"
    }

    # Custom route handler for LinkedIn Easy Apply pages
    async def route_handler(route):
        url = route.request.url
        if "linkedin.com/jobs/view/12345/" in url:
            html = """
            <html>
            <head><title>Mock Job Listing</title></head>
            <body>
              <div class="top-card">
                <h1>Software Engineer @ MockCorp</h1>
                <button class="jobs-apply-button">Easy Apply</button>
              </div>
              <div id="modal-container"></div>
              <script>
                const applyBtn = document.querySelector('.jobs-apply-button');
                applyBtn.addEventListener('click', () => {
                  document.getElementById('modal-container').innerHTML = `
                    <div class="jobs-easy-apply-modal artdeco-modal">
                      <h2>Contact Info (1/2 pages)</h2>
                      <div class="question">
                        <label for="phone">Phone number</label>
                        <input type="tel" id="phone" name="phone" value="">
                      </div>
                      <button aria-label="Continue to next step" id="next-btn">Next</button>
                    </div>
                  `;
                  
                  // Setup next button event listener
                  document.getElementById('next-btn').addEventListener('click', () => {
                    document.getElementById('modal-container').innerHTML = `
                      <div class="jobs-easy-apply-modal artdeco-modal">
                        <h2>Resume (2/2 pages)</h2>
                        <div class="question">
                          <label for="resume">Upload Resume</label>
                          <input type="file" id="resume" name="resume">
                        </div>
                        <button aria-label="Submit application" id="submit-btn">Submit application</button>
                      </div>
                    `;
                    
                    // Setup submit event listener
                    document.getElementById('submit-btn').addEventListener('click', () => {
                      document.getElementById('modal-container').innerHTML = `
                        <div class="jobs-easy-apply-modal artdeco-modal">
                          <h2>Application Submitted!</h2>
                        </div>
                      `;
                    });
                  });
                });
              </script>
            </body>
            </html>
            """
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await route.abort()

    # Subclass the scraper to inject our request interceptor immediately after page creation
    class InterceptingLinkedInScraper(LinkedInScraper):
        async def _start_browser(self, load_extensions: bool = False):
            page = await super()._start_browser(load_extensions)
            await page.route("**/*", route_handler)
            return page

        async def _tailor_resume_with_jobright(self, job: dict) -> str:
            return self._configured_resume_path()

    # Setup the scraper and mock inputs
    scraper = InterceptingLinkedInScraper(config)
    job = {
        "job_id": "li12345",
        "title": "Software Engineer",
        "company": "MockCorp",
        "url": "https://www.linkedin.com/jobs/view/12345/",
        "source": "linkedin"
    }

    # We mock the profile loader and answers so the test runs completely isolated
    with patch("src.resume_helper.ResumeFieldFixer.load_profile") as mock_load, \
         patch.dict("src.sources.linkedin.USER_ANSWERS", {"phone_default": "555-0199"}):
        
        # Run apply flow with auto_submit=True
        success = await scraper.apply(job, auto_submit=True)
        assert success is True
        assert scraper.last_apply_status == "applied" or scraper.last_apply_status == "submitted"


# ── Option 1: Mock Indeed Scraper with ATS Handoff Test ─────────────────────
@pytest.mark.asyncio
async def test_mock_indeed_ats_handoff():
    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
    }

    # Intercept indeed and greenhouse mock links
    async def route_handler(route):
        url = route.request.url
        if "indeed.com/viewjob" in url:
            html = """
            <html>
            <body>
              <h1>Software Architect</h1>
              <a href="https://mock-company.greenhouse.io/jobs/apply">Apply on company site</a>
            </body>
            </html>
            """
            await route.fulfill(status=200, content_type="text/html", body=html)
        elif "greenhouse.io/jobs/apply" in url:
            html = """
            <html>
            <body>
              <h1>Greenhouse Mock Form</h1>
              <button id="apply_button" onclick="this.style.display='none';">Apply</button>
              <form id="apply-form">
                <input type="text" id="first_name" name="first_name" placeholder="First Name">
                <input type="text" id="last_name" name="last_name" placeholder="Last Name">
                <input type="email" id="email" name="email" placeholder="Email">
                <input type="file" id="resume" name="resume">
                <button type="submit" data-automation-id="bottom-navigation-next-button">Submit Application</button>
              </form>
            </body>
            </html>
            """
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await route.abort()

    # Intercepting wrapper classes for Indeed and Jobright
    class InterceptingIndeedScraper(IndeedScraper):
        async def _start_browser(self, load_extensions: bool = False):
            page = await super()._start_browser(load_extensions)
            await page.route("**/*", route_handler)
            return page

    # We patch JobrightScraper's _start_browser as well, since IndeedScraper delegates to it!
    original_jobright_start = JobrightScraper._start_browser
    async def intercepting_jobright_start(self, load_extensions: bool = False):
        page = await original_jobright_start(self, load_extensions)
        await page.route("**/*", route_handler)
        return page

    # Setup the scraper and mock inputs
    scraper = InterceptingIndeedScraper(config)
    job = {
        "job_id": "ind123",
        "title": "Software Architect",
        "company": "GreenhouseCorp",
        "url": "https://www.indeed.com/viewjob?jk=123",
        "source": "indeed"
    }

    with patch("src.sources.jobright.JobrightScraper._start_browser", intercepting_jobright_start), \
         patch("src.resume_helper.ResumeFieldFixer.load_profile") as mock_load, \
         patch("src.resume_helper.ResumeFieldFixer.fix_fields", AsyncMock()) as mock_fix:
        
        success = await scraper.apply(job, auto_submit=True)
        assert success is True


@pytest.mark.asyncio
async def test_mock_linkedin_non_easy_apply_fast_path_delegates_to_external_ats():
    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
    }

    class FastPathLinkedInScraper(LinkedInScraper):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.easy_apply_selectors_checked = False
            self.external_extract_called = False
            self.external_apply_args = None

        async def _start_browser(self, load_extensions: bool = False):
            page = await super()._start_browser(load_extensions)
            await page.set_content("""
                <html>
                  <body>
                    <h1>Director Platform Engineering</h1>
                    <button class="jobs-apply-button">Apply on company website</button>
                  </body>
                </html>
            """)
            original_wait_for_selector = page.wait_for_selector

            async def tracking_wait_for_selector(selector, *args, **kwargs):
                if "Easy Apply" in selector or "jobs-apply-button" in selector:
                    self.easy_apply_selectors_checked = True
                return await original_wait_for_selector(selector, *args, **kwargs)

            page.wait_for_selector = tracking_wait_for_selector
            return page

        async def _tailor_resume_with_jobright(self, job: dict) -> str:
            return self._configured_resume_path()

        async def _extract_external_apply_url(self, page) -> str:
            self.external_extract_called = True
            return "https://boards.greenhouse.io/mock/jobs/123"

        async def _apply_external_ats(self, job: dict, external_url: str, resume_path: str, auto_submit: bool = False) -> bool:
            self.external_apply_args = {
                "job_id": job["job_id"],
                "external_url": external_url,
                "resume_path": resume_path,
                "auto_submit": auto_submit,
            }
            self.last_apply_status = "submitted"
            self.last_apply_detail = "mock ATS submitted"
            return True

    scraper = FastPathLinkedInScraper(config)
    job = {
        "job_id": "li-non-easy-1",
        "title": "Director Platform Engineering",
        "company": "Mock External Corp",
        "url": "https://www.linkedin.com/jobs/view/non-easy-1/",
        "source": "linkedin",
        "has_easy_apply": False,
    }

    success = await scraper.apply(job, auto_submit=True)

    assert success is True
    assert scraper.external_extract_called is True
    assert scraper.external_apply_args == {
        "job_id": "li-non-easy-1",
        "external_url": "https://boards.greenhouse.io/mock/jobs/123",
        "resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
        "auto_submit": True,
    }
    assert scraper.easy_apply_selectors_checked is False


@pytest.mark.asyncio
async def test_mock_linkedin_extracts_external_url_from_interstitial_query_param():
    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
    }

    destination = "https%3A%2F%2Fjobs.lever.co%2Fmock-company%2Fabc123"
    interstitial_url = f"https://www.linkedin.com/jobs/apply/externalApply?url={destination}"

    class InterstitialLinkedInScraper(LinkedInScraper):
        async def _start_browser(self, load_extensions: bool = False):
            page = await super()._start_browser(load_extensions)
            await page.set_content(f"""
                <html>
                  <body>
                    <h1>VP Engineering</h1>
                    <button id="external-apply">Apply on company website</button>
                    <script>
                      document.getElementById('external-apply').addEventListener('click', () => {{
                        window.location.href = "{interstitial_url}";
                      }});
                    </script>
                  </body>
                </html>
            """)
            return page

    scraper = InterstitialLinkedInScraper(config)
    page = await scraper._start_browser()
    try:
        external_url = await scraper._extract_external_apply_url(page)
    finally:
        await scraper._close_browser()

    assert external_url == "https://jobs.lever.co/mock-company/abc123"


# ── Option 2: Live Opt-in Dry-Run Tests ─────────────────────────────────────
@pytest.mark.live
@pytest.mark.asyncio
async def test_live_linkedin_dry_run():
    """Live LinkedIn Easy Apply dry-run test (Requires --run-live flag and credentials)."""
    url = os.environ.get("LIVE_TEST_LINKEDIN_URL")
    if not url:
        pytest.skip("LIVE_TEST_LINKEDIN_URL not set in environment.")

    # Load real credentials from env
    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not email or not password:
        pytest.skip("LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be configured for live dry-run.")

    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
    }
    scraper = LinkedInScraper(config)
    job = {
        "job_id": "live-li-test",
        "title": "Test Job",
        "company": "Test Company",
        "url": url,
        "source": "linkedin"
    }

    # Run with auto_submit=False (dry-run check)
    success = await scraper.apply(job, auto_submit=False)
    
    # Assert it completed without error and was stopped before final click
    assert success is False
    assert scraper.last_apply_status == "submission_cancelled"


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_indeed_dry_run():
    """Live Indeed/Jobright ATS dry-run test (Requires --run-live flag and credentials)."""
    url = os.environ.get("LIVE_TEST_INDEED_URL")
    if not url:
        pytest.skip("LIVE_TEST_INDEED_URL not set in environment.")

    config = {
        "local_resume_path": str(Path(__file__).parent / "dummy_resume.pdf"),
    }
    scraper = IndeedScraper(config)
    job = {
        "job_id": "live-indeed-test",
        "title": "Test Job",
        "company": "Test Company",
        "url": url,
        "source": "indeed"
    }

    # Run with auto_submit=False (dry-run check)
    success = await scraper.apply(job, auto_submit=False)
    
    # Assert it completed without error and was stopped before final click
    assert success is False
    assert scraper.last_apply_status in ("submission_cancelled", "blocked")
