"""
Base scraper class — all source scrapers extend this.

Session persistence: on first run the user logs in manually inside the
Playwright window; the session (cookies + localStorage) is then saved to
state/sessions/<source>.json so every subsequent run loads it automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from rich.console import Console

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

console = Console()

_log = logging.getLogger(__name__)

# Collects {accept, name, label} for every file input in one round-trip.
# Label resolution: explicit <label for=id>, then up to 4 ancestors' text,
# then aria-label / data-automation-id. Returned in document order so results
# align with page.query_selector_all('input[type="file"]').
_FILE_INPUT_METADATA_JS = """
() => Array.from(document.querySelectorAll('input[type="file"]')).map(node => {
    let label = '';
    const id = node.id;
    const explicit = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
    if (explicit && explicit.innerText) {
        label = explicit.innerText;
    } else {
        let p = node.parentElement;
        for (let i = 0; p && i < 4; i++, p = p.parentElement) {
            const txt = (p.innerText || '').trim();
            if (txt) { label = txt; break; }
        }
    }
    if (!label) label = node.getAttribute('aria-label') || node.getAttribute('data-automation-id') || '';
    return {
        accept: (node.accept || '').toLowerCase(),
        name: (node.name || '').toLowerCase(),
        label: (label || '').toLowerCase(),
    };
})
"""


class JobExpiredError(Exception):
    """Exception raised when a job posting is no longer active or listed."""
    pass


class AuthFailedError(Exception):
    """Raised when a scraper detects an expired or invalid session.

    Caught by the orchestrator to trigger ReauthManager instead of
    silently skipping the source.
    """
    def __init__(self, source: str, detail: str = ""):
        self.source = source
        self.detail = detail
        super().__init__(f"{source} auth failed: {detail}" if detail else f"{source} auth failed")


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
        self._mc = None  # lazy ModelClient — shared across all model calls in this scraper instance

    @property
    def _model_client(self):
        """Lazy ModelClient: Ollama → Claude → OpenAI cascade for all in-scraper model calls."""
        if self._mc is None:
            import os as _os
            from src.model_client import ModelClient
            self._mc = ModelClient(
                anthropic_api_key=_os.environ.get("ANTHROPIC_API_KEY", ""),
                anthropic_model="claude-haiku-4-5-20251001",
            )
        return self._mc

    def _set_apply_outcome(self, status: str, detail: str) -> bool:
        """Record why an apply attempt did or did not submit."""
        self.last_apply_status = status
        self.last_apply_detail = detail
        return False

    @property
    def _session_file(self) -> Path:
        return SESSIONS_DIR / f"{self.name}.json"

    @property
    def _session_export_path(self) -> Path:
        """JSON cookie export for Chromium fallback (used when Chrome is already running)."""
        return SESSIONS_DIR / f"{self.name}_chromium.json"

    @staticmethod
    def _chrome_is_running() -> bool:
        """True when the user's Chrome instance is running (ProcessSingleton risk)."""
        try:
            return subprocess.run(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True, timeout=3,
            ).returncode == 0
        except Exception:
            return False

    @property
    def _profile_dir(self) -> Path:
        # DO NOT change this to the main Chrome profile — it causes database
        # lock failures because Chrome holds an exclusive lock on that directory.
        # Each scraper gets its own isolated profile under state/sessions/.
        d = SESSIONS_DIR / f"{self.name}_profile"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _clear_profile_locks(self) -> None:
        """Remove stale Chrome lock files and kill orphaned Chrome processes
        holding the profile before re-launching."""
        import time
        # Kill any Chrome processes still holding this profile directory
        try:
            subprocess.run(
                ["pkill", "-f", str(self._profile_dir)],
                capture_output=True, timeout=5
            )
            # Give Chrome time to fully release its SQLite databases before
            # we open the profile again. Without this, rapid open→close→open
            # sequences cause 'database is locked' / renderer crashes.
            time.sleep(2)
        except Exception:
            pass

        # Remove Singleton lock files
        for lockfile in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self._profile_dir / lockfile).unlink(missing_ok=True)
            except Exception:
                pass

        # Remove database lock files that persist after a hard kill
        for pattern in ("*.lock", "lockfile"):
            for p in self._profile_dir.rglob(pattern):
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
            "--window-size=1280,800",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-profile-error-dialogs",
        ]

        if load_extensions:
            ext_path = EXT_DIR / "jobright-autofill"
            if ext_path.exists():
                # Load local extension as fallback; don't disable-extensions-except
                # so extensions installed via Chrome Web Store in the profile also load.
                args.append(f"--load-extension={str(ext_path)}")
            # Extensions already installed in the Chrome profile load automatically.

        # Decide whether to use bundled Chromium + JSON session (background/unattended)
        # or persistent Chrome context (interactive).
        #
        # Background detection: launchd sets stdin=/dev/null (not a TTY).
        # Use sys.stdin, not sys.stdout — stdout piped to `tee` would incorrectly
        # classify an interactive run as background.
        #
        # Interactive runs (prepare-sessions, apply) always use persistent Chrome so:
        #   1. freshly-refreshed cookies land in the profile, not just the JSON;
        #   2. Chrome Web Store extensions remain available for apply.
        in_background = not sys.stdin.isatty()

        # When a valid JSON export exists, background runs always use bundled Chromium.
        # Background discover never needs channel="chrome" — it only needs cookies,
        # which the JSON export provides. This completely avoids the ProcessSingleton
        # conflict (two Chrome binaries sharing the same global IPC socket) regardless
        # of whether Chrome happens to be running at the time.
        use_chromium_fallback = False
        if in_background and self._session_export_path.exists():
            # Validate the file before trusting it — a partial write from a prior crash
            # would produce opaque Playwright errors inside new_context().
            try:
                json.loads(self._session_export_path.read_text())
                use_chromium_fallback = True
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning(
                    "%s: session export is corrupt (%s) — deleting and falling back to Chrome profile.",
                    self.name, exc,
                )
                self._session_export_path.unlink(missing_ok=True)

        if use_chromium_fallback:
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=args,
                slow_mo=80,
            )
            self._context = await self._browser.new_context(
                storage_state=str(self._session_export_path),
                viewport={"width": 1400, "height": 900},
                accept_downloads=True,
            )
        else:
            # Interactive run, or no valid session export yet — use persistent Chrome.
            if in_background:
                # No export file means bootstrap hasn't run yet.  Check Chrome as an
                # extra diagnostic — if it's also running the ProcessSingleton conflict
                # will happen, which is why a hint about 'prepare-sessions' matters.
                extra = " (Chrome is running — ProcessSingleton conflict likely)" if self._chrome_is_running() else ""
                _log.warning(
                    "%s: no session export found for background run%s. "
                    "Run 'prepare-sessions' once with Chrome closed to create it.",
                    self.name, extra,
                )
            # channel="chrome" uses the real installed Chrome binary so Chrome Web
            # Store extensions, saved logins, and Chrome's cookie encryption all
            # work correctly. The dedicated profile dir keeps sessions separate from
            # the main Chrome profile — no locking conflicts, no risk to personal data.
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                channel="chrome",
                headless=False,
                accept_downloads=True,
                args=args,
                slow_mo=80,
                viewport={"width": 1400, "height": 900},
            )

        # Get or create a page
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        try:
            from playwright_stealth import stealth_async
            await stealth_async(self._page)
        except ImportError:
            _log.warning(
                "playwright-stealth not installed — bot-detection mitigation is OFF. "
                "Run: pip install playwright-stealth"
            )
        except Exception as exc:
            _log.warning("%s: failed to apply playwright-stealth: %s", self.name, exc)
        await self._page.bring_to_front()
        return self._page

    async def _export_session_json(self) -> None:
        """Export current cookies to a JSON file that bundled Chromium can load.

        Called after successful login and on close so that background discover runs
        can use Chromium + JSON instead of fighting Chrome's ProcessSingleton.
        Uses an atomic rename so a crash mid-write never leaves a corrupt file.
        """
        if not self._context:
            return
        tmp = self._session_export_path.with_suffix(".tmp")
        try:
            state = await self._context.storage_state()
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(state))
            tmp.replace(self._session_export_path)  # atomic on POSIX
        except Exception as exc:
            _log.warning("%s: failed to export session to %s: %s", self.name, self._session_export_path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    async def _save_session(self) -> None:
        """Export session cookies to JSON for Chromium fallback (persistent context saves automatically)."""
        await self._export_session_json()

    async def _close_browser(self, save_session: bool = True) -> None:
        # Export session before closing — must happen while context is still live.
        # Separated from the close() call so that close() is always reached even if
        # the export fails (export has its own internal error handling).
        if save_session:
            await self._export_session_json()

        had_context = self._context is not None or self._browser is not None

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
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
        # Wait for Chrome to finish flushing its SQLite databases to disk.
        # Without this, rapid close→open on the same profile causes database
        # lock errors and renderer crashes in the next session.
        # Skip the wait if nothing was ever opened (avoids 2s delay on startup errors).
        if had_context:
            await asyncio.sleep(2)

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

    async def _safe_evaluate(self, page, script, *args, default=None):
        """Wrap page.evaluate() so browser-death errors propagate and others are logged.

        If the browser/page/context has closed, re-raises so the orchestrator
        can catch it. Any other evaluate error logs a warning and returns default.
        """
        try:
            if args:
                return await page.evaluate(script, *args)
            return await page.evaluate(script)
        except Exception as exc:
            err = str(exc).lower()
            if any(k in err for k in ["closed", "target page", "detached", "crashed", "browser has been"]):
                raise
            import logging
            logging.getLogger(__name__).warning("%s: evaluate failed (non-fatal): %s", self.name, exc)
            return default

    async def _safe_goto(self, page, url: str, timeout: int = 30000, wait_until: str = "domcontentloaded") -> bool:
        """Navigate to url, return False on failure instead of raising.
        Re-raises if the browser/context itself has closed.
        """
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as exc:
            err = str(exc).lower()
            if any(k in err for k in ["closed", "target page", "detached", "crashed", "browser has been"]):
                raise
            import logging
            logging.getLogger(__name__).warning("%s: goto %s failed: %s", self.name, url, exc)
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

    async def _upload_documents_if_prompted(
        self, page, resume_path: str, cover_letter_path: str = ""
    ) -> bool:
        """Upload a resume (and optionally a cover letter) to visible file inputs.

        Each file input is classified as a cover-letter or resume target using its
        ``accept`` type, ``name``, and resolved label. Metadata for every input is
        gathered in a single in-page ``evaluate`` (one Playwright round-trip for the
        whole form instead of several per input), then matched back to the element
        handles by document order.

        Returns True if a resume was uploaded.
        """
        res_path = Path(resume_path).expanduser() if resume_path else None
        cl_path = Path(cover_letter_path).expanduser() if cover_letter_path else None
        if not (res_path and res_path.exists()) and not (cl_path and cl_path.exists()):
            return False

        uploaded_resume = False
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
            if not file_inputs:
                return False
            # One round-trip: {accept, name, label} for every file input, in
            # document order (matches the query_selector_all ordering above).
            metas = await page.evaluate(_FILE_INPUT_METADATA_JS)
            for idx, file_input in enumerate(file_inputs):
                meta = metas[idx] if idx < len(metas) else {}
                accept = meta.get("accept", "")
                hints = " ".join([accept, meta.get("name", ""), meta.get("label", "")])

                # Skip inputs that explicitly reject document formats.
                if accept and not any(
                    ext in accept for ext in [".pdf", ".doc", ".docx", "pdf", "word"]
                ):
                    continue

                is_cover_letter = any(
                    w in hints for w in ["cover", "letter", "cl", "motivation", "motivational"]
                )
                is_resume = (
                    any(w in hints for w in ["resume", "cv", "vitae", "work history", "upload", "file"])
                    or not hints.strip()
                )

                if is_cover_letter and cl_path and cl_path.exists():
                    await file_input.set_input_files(str(cl_path))
                    console.print(
                        f"[green]{self.name.capitalize()}:[/green] Uploaded cover letter: {cl_path.name}"
                    )
                    await self._delay(1, 2)
                elif is_resume and res_path and res_path.exists():
                    await file_input.set_input_files(str(res_path))
                    console.print(
                        f"[green]{self.name.capitalize()}:[/green] Uploaded resume: {res_path.name}"
                    )
                    uploaded_resume = True
                    await self._delay(1, 2)
                elif res_path and res_path.exists() and not uploaded_resume:
                    # Fallback: no clear match — send the resume to the first input.
                    await file_input.set_input_files(str(res_path))
                    console.print(
                        f"[green]{self.name.capitalize()}:[/green] Uploaded resume (fallback): {res_path.name}"
                    )
                    uploaded_resume = True
                    await self._delay(1, 2)
        except Exception as exc:
            console.print(
                f"[yellow]{self.name.capitalize()}:[/yellow] Document upload check failed: {exc}"
            )
        return uploaded_resume

    async def _upload_resume_if_prompted(self, page, resume_path: str) -> None:
        """Back-compat wrapper — upload only a resume.

        Retained for callers that don't handle cover letters; delegates to
        :meth:`_upload_documents_if_prompted`.
        """
        await self._upload_documents_if_prompted(page, resume_path)

    async def _get_field_label(self, page, element) -> str:
        """Try to find the label text for an input element."""
        try:
            field_id = await element.get_attribute("id")
            if field_id:
                label = await page.query_selector(f'label[for="{field_id}"]')
                if label:
                    return (await label.inner_text()).strip()
            # Try parent/sibling label
            try:
                parent = await element.evaluate_handle("el => el.closest('.form-group, .jobs-easy-apply-form-element, fieldset, div')")
            except Exception as exc:
                err = str(exc).lower()
                if any(k in err for k in ["closed", "target page", "detached", "crashed"]):
                    raise
                parent = None
            if parent:
                label = await parent.query_selector("label, legend, span[class*='label']")
                if label:
                    return (await label.inner_text()).strip()
        except Exception:
            pass
        return ""
