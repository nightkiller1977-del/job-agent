"""
Base scraper class — all source scrapers extend this.

Session persistence: on first run the user logs in manually inside the
Playwright window; the session (cookies + localStorage) is then saved to
state/sessions/<source>.json so every subsequent run loads it automatically.
"""
from __future__ import annotations

import asyncio
import json
import random
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page


class JobExpiredError(Exception):
    """Exception raised when a job posting is no longer active or listed."""
    pass


# Where session state files are stored
SESSIONS_DIR = Path(__file__).parent.parent.parent / "state" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Where browser extensions are stored
EXT_DIR = Path(__file__).parent.parent.parent / "state" / "extensions"


class BaseScraper(ABC):
    """
    Abstract base class for all job source scrapers.
    Subclasses must implement: scrape() and apply().
    """

    name: str = "base"

    def __init__(self, config: dict):
        self.config = config
        self.search_settings = config.get("search_settings", {})
        self.delay_min = self.search_settings.get("delay_min_seconds", 1)
        self.delay_max = self.search_settings.get("delay_max_seconds", 3)
        self.max_jobs = self.search_settings.get("max_jobs_per_source", 50)
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._playwright = None
        self.last_apply_status = ""
        self.last_apply_detail = ""

    def _set_apply_outcome(self, status: str, detail: str) -> bool:
        """Record why an apply attempt did or did not submit."""
        self.last_apply_status = status
        self.last_apply_detail = detail
        return False

    @property
    def _session_file(self) -> Path:
        return SESSIONS_DIR / f"{self.name}.json"

    @property
    def _profile_dir(self) -> Path:
        """Persistent Chromium profile directory for this source (survives restarts)."""
        d = SESSIONS_DIR / f"{self.name}_profile"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _clear_profile_locks(self) -> None:
        """Remove stale Chromium lock files that prevent re-launching the profile."""
        for lockfile in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = self._profile_dir / lockfile
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    async def _start_browser(self, load_extensions: bool = False) -> Page:
        """
        Launch a persistent Chromium context backed by a per-source profile dir.
        On first run the browser opens to the site and the user logs in once.
        All cookies/storage are saved automatically in the profile dir so every
        subsequent run is already authenticated — no re-login needed.

        load_extensions=True: also loads the Jobright Autofill extension from
        state/extensions/jobright-autofill so it can fill company ATS forms.
        """
        # Remove stale lock files so Playwright can acquire the profile
        self._clear_profile_locks()

        self._playwright = await async_playwright().start()

        args = [
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
        ]

        if load_extensions:
            ext_path = EXT_DIR / "jobright-autofill"
            if ext_path.exists():
                ext_str = str(ext_path)
                args += [
                    f"--load-extension={ext_str}",
                    f"--disable-extensions-except={ext_str}",
                ]
            else:
                print(f"[warning] Extension not found at {ext_path} — skipping extension load")

        # launch_persistent_context keeps cookies/localStorage across restarts
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=False,
            accept_downloads=True,
            args=args,
            slow_mo=80,
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Get or create a page
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        # Bring window to front
        await self._page.bring_to_front()
        return self._page

    async def _save_session(self) -> None:
        """No-op — persistent context saves automatically."""
        pass

    async def _close_browser(self, save_session: bool = True) -> None:
        # Persistent context: close the context (saves automatically)
        # Wrap each step so a crashed browser doesn't raise in the finally block.
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None

    async def _delay(self, extra_min: float = 0, extra_max: float = 0) -> None:
        """Random human-like delay between actions."""
        lo = self.delay_min + extra_min
        hi = self.delay_max + extra_max
        await asyncio.sleep(random.uniform(lo, hi))

    async def _safe_click(self, page: Page, selector: str, timeout: int = 5000) -> bool:
        """Click an element safely. Returns False if not found."""
        try:
            await page.click(selector, timeout=timeout)
            await self._delay()
            return True
        except Exception:
            return False

    async def _safe_fill(self, page: Page, selector: str, value: str, timeout: int = 5000) -> bool:
        """Fill an input safely. Returns False if not found."""
        try:
            await page.fill(selector, value, timeout=timeout)
            await self._delay(0, 0.5)
            return True
        except Exception:
            return False

    def _check_logged_in_redirect(self, page: Page, expected_domain: str) -> bool:
        """Returns True if the page URL contains the expected domain."""
        return expected_domain in page.url

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """
        Scrape job listings from this source.
        Returns a list of raw job dicts (not yet scored).
        Each dict must have at minimum: job_id, title, source, url.
        """
        ...

    @abstractmethod
    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        """
        Execute the application for a pre-approved job.
        Optionally bypasses the final manual submit confirmation if auto_submit is True.
        Returns True if application was submitted.
        """
        ...

    def _make_job_id(self, url: str) -> str:
        """Generate a stable job_id from a URL."""
        import hashlib
        return hashlib.md5(url.encode()).hexdigest()[:16]
