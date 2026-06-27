"""
Indeed scraper.
Navigates Indeed's recommended / personalized job feed and extracts listings.
Uses a persistent Chromium profile — user logs in once; session survives restarts.

Apply strategy:
  Indeed jobs are almost always backed by a company ATS (Workday, Greenhouse, etc.)
  reached via "Apply on company site". We extract that ATS URL, then delegate to
  JobrightScraper.apply_external_ats_job() which contains all the Workday / Greenhouse /
  Lever / BrassRing handling already battle-tested for those portals.

  For the rare "Easy Apply" (form hosted on Indeed) we detect the flag and return a
  clear blocked status so the user can apply manually — Indeed's bot detection makes
  scripted Easy Apply unreliable.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

from .base import BaseScraper, AuthFailedError, JobExpiredError
from src.notifier import notify_error

console = Console()

INDEED_BASE          = "https://www.indeed.com"
INDEED_JOBS_URL      = "https://www.indeed.com/jobs"          # personalised feed when logged in
INDEED_RECOMMENDED   = "https://www.indeed.com/recommended"   # alternate recommended URL
INDEED_SEARCH_URL    = "https://www.indeed.com/jobs?q={q}&l={l}&sort=date&fromage=7"

# External ATS host fragments we recognise (same list as Jobright scraper)
_ATS_HOSTS = [
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "taleo.net",
    "icims.com", "smartrecruiters.com", "bamboohr.com", "ashbyhq.com",
    "workable.com", "brassring.com", "successfactors.com",
    "myworkday.com", "jobs.lever.co", "apply.workable.com",
    "recruitingbypaycor.com", "paylocity.com", "ultipro.com",
]


class IndeedScraper(BaseScraper):
    name = "indeed"

    # ------------------------------------------------------------------
    # scrape
    # ------------------------------------------------------------------

    async def scrape(self) -> list[dict]:
        """Scrape Indeed's recommended/personalised job feed."""
        console.print("[blue]Indeed:[/blue] Opening browser…")
        page = await self._start_browser()
        jobs: list[dict] = []

        try:
            await page.goto(INDEED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Handle login redirect
            if self._on_login_page(page):
                email    = os.environ.get("INDEED_EMAIL", "")
                password = os.environ.get("INDEED_PASSWORD", "")
                if email and password:
                    console.print("[blue]Indeed:[/blue] Not logged in — attempting auto-login…")
                    if not await self._auto_login(page, email, password):
                        if not (sys.stdin and sys.stdin.isatty()):
                            console.print("[red]Indeed: Auto-login failed (non-interactive). Raising AuthFailedError.[/red]")
                            raise AuthFailedError("indeed", "Auto-login returned False (non-interactive)")
                        console.print(
                            "\n[yellow]Indeed:[/yellow] Auto-login failed.\n"
                            "  → Log in manually in the browser window, then press Enter."
                        )
                        input("  Press Enter once logged in > ")
                        await page.goto(INDEED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
                        await self._delay(2, 3)
                else:
                    if not (sys.stdin and sys.stdin.isatty()):
                        console.print("[red]Indeed: Not logged in, no credentials, non-interactive. Raising AuthFailedError.[/red]")
                        raise AuthFailedError("indeed", "Not logged in and no credentials in .env")
                    console.print(
                        "\n[yellow]Indeed:[/yellow] Not logged in.\n"
                        "  → Log in to indeed.com in the browser, then press Enter."
                    )
                    input("  Press Enter once logged in > ")
                    await page.goto(INDEED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
                    await self._delay(2, 3)

            console.print(f"[blue]Indeed:[/blue] Page loaded: {page.url}")

            if "indeed.com" not in page.url:
                console.print("[red]Indeed: Could not navigate to jobs page — raising AuthFailedError.[/red]")
                raise AuthFailedError("indeed", f"Unexpected redirect to {page.url}")

            # Run configured keyword search if present
            search   = self.config.get("search_settings", {})
            keywords = search.get("keywords", "")
            location = search.get("location", "Remote")
            if keywords:
                await self._run_search(page, keywords, location)

            console.print("[blue]Indeed:[/blue] Extracting job cards…")
            jobs = await self._extract_all_pages(page)
            console.print(f"[blue]Indeed:[/blue] Found {len(jobs)} jobs.")

        except AuthFailedError:
            raise
        except Exception as exc:
            console.print(f"[red]Indeed scrape error:[/red] {exc}")
        finally:
            await self._close_browser()

        return jobs

    def _on_login_page(self, page) -> bool:
        """Return True if Indeed is showing a login/auth wall OR the empty-search
        redirect that indicates no active session."""
        try:
            url = page.url
            login_indicators = [
                "/account/login", "secure.indeed.com", "/auth", "/signin",
                # Redirected to the generic search homepage — happens when not logged in
                # and /jobs has no personalised feed to show.
                "?from=jobsearch-empty-whatwhere",
            ]
            return any(w in url for w in login_indicators)
        except Exception:
            return False

    async def _run_search(self, page, keywords: str, location: str) -> None:
        try:
            kw = await page.query_selector(
                '#text-input-what, input[name="q"], input[aria-label*="What" i]'
            )
            if kw:
                await kw.triple_click()
                await kw.fill(keywords)
                await self._delay(0.5, 1)

            loc = await page.query_selector(
                '#text-input-where, input[name="l"], input[aria-label*="Where" i]'
            )
            if loc:
                await loc.triple_click()
                await loc.fill(location)
                await self._delay(0.5, 1)

            btn = await page.query_selector('button[type="submit"], button[aria-label*="Find" i]')
            if btn:
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await self._delay(2, 3)
        except Exception as exc:
            console.print(f"[dim]Indeed: search form error: {exc}[/dim]")

    async def _extract_all_pages(self, page) -> list[dict]:
        jobs: list[dict] = []
        for _ in range(3):
            if len(jobs) >= self.max_jobs:
                break

            # Scroll to trigger lazy-loaded cards
            await self._safe_evaluate(page, "window.scrollTo(0, document.body.scrollHeight)", default=None)
            await self._delay(1, 2)
            await self._safe_evaluate(page, "window.scrollTo(0, 0)", default=None)
            await self._delay(0.5, 1)

            page_jobs = await self._js_extract(page)
            new = [j for j in page_jobs if j["job_id"] not in {x["job_id"] for x in jobs}]
            jobs.extend(new)
            if not new:
                break

            # Paginate
            try:
                nxt = await page.query_selector(
                    '[aria-label="Next Page"], [data-testid="pagination-page-next"]'
                )
                if nxt and not await nxt.get_attribute("disabled"):
                    await nxt.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await self._delay(2, 3)
                else:
                    break
            except Exception:
                break

        return jobs

    async def _js_extract(self, page) -> list[dict]:
        """Extract job cards from the Indeed search results DOM."""
        raw = await self._safe_evaluate(page, f"""
        () => {{
            const BASE = '{INDEED_BASE}';
            const results = [];
            // Cards are tagged with data-jk (the job key)
            const cards = document.querySelectorAll('[data-jk], .job_seen_beacon');
            for (const card of cards) {{
                try {{
                    const jk   = card.getAttribute('data-jk') || '';
                    const titleEl = card.querySelector(
                        'h2[class*="jobTitle"], [data-testid="job-title"], .jobTitle'
                    );
                    const title = titleEl ? titleEl.innerText.trim() : '';
                    if (!title) continue;

                    let url = jk ? `${{BASE}}/viewjob?jk=${{jk}}` : '';
                    if (!url) {{
                        const a = card.querySelector('a[href*="jk="]');
                        if (a) url = a.href;
                    }}
                    if (!url) continue;

                    const company = (card.querySelector(
                        '[data-testid="company-name"], .companyName'
                    )?.innerText || '').trim();
                    const location = (card.querySelector(
                        '[data-testid="job-location"], .companyLocation'
                    )?.innerText || '').trim();
                    const salary = (card.querySelector(
                        '[class*="salary-snippet"], [data-testid*="salary"]'
                    )?.innerText || '').trim();

                    results.push({{ jk, title, company, location, salary, url }});
                }} catch(e) {{}}
            }}
            return results;
        }}
        """, default=[])

        now = datetime.utcnow().isoformat()
        jobs = []
        for item in (raw or []):
            url = item.get("url", "")
            if not url:
                continue
            if url.startswith("/"):
                url = INDEED_BASE + url
            jobs.append({
                "job_id":       self._make_job_id(url),
                "source":       "indeed",
                "title":        item.get("title", ""),
                "company":      item.get("company", ""),
                "location":     item.get("location", ""),
                "salary_raw":   item.get("salary", ""),
                "remote_type":  _infer_remote_type(item.get("location", "")),
                "url":          url,
                "description":  (
                    f"{item.get('title', '')} at {item.get('company', '')} — "
                    f"{item.get('location', '')}"
                ),
                "discovered_at": now,
            })
        return jobs

    # ------------------------------------------------------------------
    # hydrate
    # ------------------------------------------------------------------

    async def _hydrate_job_detail(self, page, job: dict) -> None:
        """Extract full job details from an Indeed job detail page.

        Called by orchestrator.hydrate_external_jobs() when source == 'indeed'.
        The page is already navigated to job["url"] before this is called.
        """
        try:
            # Title
            title_el = await page.query_selector(
                '[data-testid="jobTitle"], h1.jobsearch-JobInfoHeader-title, '
                '.jobsearch-JobInfoHeader-title, h1[class*="title"]'
            )
            if title_el:
                job["title"] = (await title_el.inner_text()).strip()
            elif not job.get("title") or job["title"] in ("Importing...", ""):
                raw = await page.title()
                for suffix in [" - Indeed.com", " | Indeed", " - Jobs", " Indeed"]:
                    if raw.endswith(suffix):
                        raw = raw[: -len(suffix)].strip()
                job["title"] = raw

            # Company
            co_el = await page.query_selector(
                '[data-testid="inlineHeader-companyName"] a, '
                '[data-company-name], '
                '.jobsearch-InlineCompanyRating-companyHeader'
            )
            if co_el:
                job["company"] = (await co_el.inner_text()).strip()

            # Location
            loc_el = await page.query_selector(
                '[data-testid="job-location"], '
                '[data-testid="inlineHeader-companyLocation"]'
            )
            if loc_el:
                job["location"] = (await loc_el.inner_text()).strip()

            # Salary
            sal_el = await page.query_selector(
                '[class*="salary-snippet"], [data-testid*="salary"], '
                '#salaryInfoAndJobType'
            )
            if sal_el:
                job["salary_raw"] = (await sal_el.inner_text()).strip()

            # Description — prefer the dedicated description container
            desc_el = await page.query_selector(
                '#jobDescriptionText, '
                '[class*="jobsearch-jobDescriptionText"], '
                '[data-testid="jobsearch-JobComponent-description"]'
            )
            if desc_el:
                job["description"] = (await desc_el.inner_text()).strip()[:4000]
            else:
                body = await self._safe_evaluate(page, "document.body.innerText", default="")
                job["description"] = (body or "")[:3000]

        except Exception as exc:
            console.print(f"[dim]Indeed hydrate detail error: {exc}[/dim]")

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        """
        Apply to an Indeed job.

        Flow:
          1. Open the Indeed job page and extract the external company ATS URL.
          2. Delegate to JobrightScraper.apply_external_ats_job() — this reuses all
             Workday / Greenhouse / Lever / BrassRing logic already built and tested.
          3. If no external URL found (Easy Apply job): return a clear blocked status
             since Indeed's bot-detection makes scripted Easy Apply unreliable.
        """
        from .jobright import JobrightScraper

        self.last_apply_status = "started"
        self.last_apply_detail = ""
        url = job.get("url", "")

        console.print(f"\n[blue]Indeed Apply:[/blue] {job.get('title')} @ {job.get('company')}")

        # ── Step 1: Extract external ATS URL from the Indeed page ────────────
        page = await self._start_browser()
        ext_url = ""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            page_text = await page.evaluate("document.body.innerText")
            if "no longer available" in page_text.lower() or "job has expired" in page_text.lower():
                raise JobExpiredError("Indeed job no longer active")

            ext_url = await self._extract_apply_url(page)

            # No direct link yet — click "Apply now" and capture any new tab
            if not ext_url:
                console.print("[blue]Indeed:[/blue] No direct ATS link found — clicking Apply now…")
                before_pages = set(p.url for p in page.context.pages if p.url)
                clicked = await page.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('a, button'))
                        .find(el => /^apply now$/i.test((el.innerText || '').trim()));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """)
                if clicked:
                    await self._delay(2, 3)
                    for p in page.context.pages:
                        try:
                            if p.url and p.url not in before_pages and "indeed.com" not in p.url:
                                ext_url = p.url
                                break
                        except Exception:
                            continue

        except JobExpiredError:
            await self._close_browser()
            raise
        except Exception as exc:
            console.print(f"[red]Indeed: error reading job page: {exc}[/red]")
        finally:
            await self._close_browser()

        if not ext_url:
            return self._set_apply_outcome(
                "indeed_easy_apply_or_no_ats",
                f"No external company ATS URL found at {url}. "
                "This may be an Indeed Easy Apply job — open it manually to apply.",
            )

        # ── Step 2: Delegate to Jobright's battle-tested ATS machinery ───────
        console.print(f"[blue]Indeed:[/blue] Handing off to ATS: {ext_url[:80]}")
        jr = JobrightScraper(self.config)
        result = await jr.apply_external_ats_job(job, ext_url, auto_submit=auto_submit)
        self.last_apply_status = jr.last_apply_status
        self.last_apply_detail = jr.last_apply_detail
        return result

    async def _extract_apply_url(self, page) -> str:
        """
        Find the external company ATS URL on an Indeed job listing.
        Returns empty string for Easy Apply (no external URL) or if not found.
        """
        return await self._safe_evaluate(page, f"""
        () => {{
            const ATS = {_ATS_HOSTS!r};
            // Direct anchor tags pointing to company ATS
            for (const a of document.querySelectorAll('a[href]')) {{
                const h = a.href || '';
                if (!h.includes('indeed.com') && ATS.some(p => h.includes(p))) return h;
            }}
            // "Apply on company site" / "Apply on employer site" text links
            const ext = Array.from(document.querySelectorAll('a, button'))
                .find(el => /apply on (company|employer) site/i.test(el.textContent || ''));
            if (ext?.href) return ext.href;
            // data-apply-url or similar attributes
            const tagged = document.querySelector('[data-apply-url], [data-ats-url]');
            if (tagged) return tagged.getAttribute('data-apply-url') || tagged.getAttribute('data-ats-url') || '';
            return '';
        }}
        """, default="")

    async def prepare_session(self, job: dict | None) -> None:
        """Open Indeed and establish a persistent session.

        Tries auto-login with INDEED_EMAIL / INDEED_PASSWORD from env first
        (populated by load_credentials_from_dashboard() at run start).
        Falls back to a manual login prompt in interactive mode.
        """
        console.print("\n[blue]Indeed Session Prep:[/blue] Opening Indeed…")
        page = await self._start_browser()
        try:
            await page.goto(INDEED_JOBS_URL, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            console.print(f"[cyan]Current URL:[/cyan] {page.url}")

            # Detect whether a login wall is present (reuse the same check as scrape())
            needs_login = self._on_login_page(page)

            if needs_login:
                email    = os.environ.get("INDEED_EMAIL", "")
                password = os.environ.get("INDEED_PASSWORD", "")
                if email and password:
                    console.print("[blue]Indeed Session Prep:[/blue] Attempting auto-login with stored credentials…")
                    success = await self._auto_login(page, email, password)
                    if success:
                        console.print("[green]Indeed Session Prep:[/green] Auto-login succeeded — session saved.")
                        await self._delay(2, 3)
                        return
                    console.print(
                        "[yellow]Indeed Session Prep:[/yellow] Auto-login failed "
                        "(2FA challenge or CAPTCHA). Falling back to manual login."
                    )
                else:
                    console.print(
                        "[yellow]Indeed Session Prep:[/yellow] INDEED_EMAIL / INDEED_PASSWORD not set. "
                        "Add them in the cloud dashboard or local .env to enable auto-login."
                    )

                if sys.stdin and sys.stdin.isatty():
                    console.print(
                        "\n[bold yellow]Log in to Indeed in the browser window.[/bold yellow]\n"
                        "  Your session will be saved for future runs.\n"
                        "  Tip: save credentials in the dashboard to skip this step next time.\n"
                    )
                    input("  Press Enter once logged in > ")
                else:
                    console.print("[yellow]Non-interactive — skipping manual login prompt.[/yellow]")
            else:
                console.print("[green]Indeed Session Prep:[/green] Already logged in. Session is fresh.")
        finally:
            await self._close_browser()

    # ------------------------------------------------------------------
    # auth helper
    # ------------------------------------------------------------------

    async def _auto_login(self, page, email: str, password: str) -> bool:
        """Attempt programmatic login to Indeed."""
        try:
            # If we're on the homepage redirect (no session) rather than the actual
            # login form, navigate to the sign-in page directly.
            if "?from=jobsearch-empty-whatwhere" in page.url or (
                "/account/login" not in page.url and "secure.indeed.com" not in page.url
                and "signin" not in page.url.lower()
            ):
                await page.goto(
                    "https://secure.indeed.com/account/login",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await self._delay(1.5, 2.5)

            email_input = None
            for sel in [
                'input[type="email"]',
                'input[name="email"]',
                '#login-email-input',
                'input[autocomplete="username"]',
            ]:
                try:
                    elem = await page.wait_for_selector(sel, timeout=5000)
                    if elem:
                        email_input = elem
                        break
                except Exception:
                    continue
            if not email_input:
                return False

            await email_input.fill(email)
            await self._delay(0.5, 1)

            for sel in ['button[type="submit"]', 'button:text-matches("Continue", "i")']:
                try:
                    btn = await page.wait_for_selector(sel, timeout=8000)
                    if btn:
                        await btn.click()
                        await self._delay(2, 3)
                        break
                except Exception:
                    continue

            pwd = None
            for sel in ['input[type="password"]', 'input[name="password"]']:
                try:
                    elem = await page.wait_for_selector(sel, timeout=6000)
                    if elem:
                        pwd = elem
                        break
                except Exception:
                    continue
            if not pwd:
                return False

            await pwd.fill(password)
            await self._delay(0.5, 1)

            for sel in ['button[type="submit"]', 'button:text-matches("Sign In", "i")']:
                try:
                    btn = await page.wait_for_selector(sel, timeout=8000)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    continue

            # Wait for navigation triggered by the sign-in button click
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            for _ in range(8):
                cur = page.url
                if "indeed.com/jobs" in cur or "indeed.com/myjobs" in cur:
                    console.print("[green]Indeed: Auto-login successful![/green]")
                    return True
                if "/account/login" not in cur and "secure.indeed.com" not in cur:
                    console.print("[green]Indeed: Auto-login successful (redirect detected)![/green]")
                    return True
                await asyncio.sleep(2)

            return False

        except Exception as exc:
            console.print(f"[red]Indeed login error: {exc}[/red]")
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_remote_type(location: str) -> str:
    loc = (location or "").lower()
    if "remote" in loc and "hybrid" not in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    if any(w in loc for w in ["onsite", "on-site", "on site", "in office", "in-office"]):
        return "onsite"
    return "unknown"
