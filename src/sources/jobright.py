"""
Jobright.ai scraper.
Navigates to the matched/recommended jobs section and extracts listings.
User is assumed to already be logged in (uses existing browser session via profile
or simply navigates — if redirected to login, prints a clear error).
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime

from rich.console import Console

from .base import BaseScraper

console = Console()

JOBRIGHT_BASE = "https://jobright.ai"
JOBRIGHT_JOBS_URL = "https://jobright.ai/jobs"
JOBRIGHT_MATCHED_URL = "https://jobright.ai/jobs/recommend"


class JobrightScraper(BaseScraper):
    name = "jobright"

    async def scrape(self) -> list[dict]:
        console.print("[magenta]Jobright:[/magenta] Opening browser…")
        page = await self._start_browser()
        jobs = []

        try:
            # Navigate to jobright matched jobs
            await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check for login redirect
            if "/login" in page.url or "/signin" in page.url or "auth" in page.url or "jobright.ai" not in page.url:
                console.print(
                    "\n[yellow]Jobright:[/yellow] Not logged in.\n"
                    "  → Please log in to jobright.ai in the browser window that just opened.\n"
                    "  → Once you're on the jobs page, come back here and press Enter."
                )
                input("  Press Enter once logged in > ")
                await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=30000)
                await self._delay(2, 3)
                # Save session so next run is automatic
                await self._save_session()
                console.print("[green]Jobright: Session saved — future runs will log in automatically.[/green]")

            console.print(f"[magenta]Jobright:[/magenta] Page loaded: {page.url}")

            # If not on the jobs page, attempt auto-login from .env credentials
            if "jobright.ai/jobs" not in page.url:
                email = os.environ.get("JOBRIGHT_EMAIL", "")
                password = os.environ.get("JOBRIGHT_PASSWORD", "")

                if email and password:
                    console.print("[magenta]Jobright:[/magenta] Not logged in — attempting auto-login…")
                    logged_in = await self._auto_login(page, email, password)
                    if not logged_in:
                        console.print("[red]Jobright: Auto-login failed — skipping.[/red]")
                        return []
                else:
                    console.print(
                        "[red]Jobright:[/red] Not logged in and no credentials in .env.\n"
                        "  Add JOBRIGHT_EMAIL and JOBRIGHT_PASSWORD to your .env file."
                    )
                    return []

            console.print("[magenta]Jobright:[/magenta] Waiting for job cards to render…")
            try:
                await page.wait_for_selector('[class*="index_job-card__"]', timeout=20000)
                console.print("[magenta]Jobright:[/magenta] Job cards detected.")
            except Exception:
                console.print(f"[yellow]Jobright:[/yellow] Cards slow to load, trying extraction anyway…")

            # Scroll to load jobs
            jobs = await self._extract_jobs(page)

            console.print(f"[magenta]Jobright:[/magenta] Found {len(jobs)} jobs.")
        except Exception as exc:
            console.print(f"[red]Jobright scrape error:[/red] {exc}")
        finally:
            await self._close_browser()

        return jobs

    async def _auto_login(self, page, email: str, password: str) -> bool:
        """Attempt to log in to jobright.ai using email/password from .env."""
        try:
            # Click the Sign In button if visible on the homepage
            for sign_in_sel in [
                'button:text("Sign In")', 'a:text("Sign In")',
                'button:text-matches("sign in", "i")', 'a:text-matches("sign in", "i")',
            ]:
                try:
                    btn = await page.wait_for_selector(sign_in_sel, timeout=4000)
                    if btn:
                        await btn.click()
                        await self._delay(1, 2)
                        break
                except Exception:
                    continue

            # Wait for the email field in the login modal
            email_sel = 'input[type="email"], input[placeholder*="mail" i], input[name="email"]'
            await page.wait_for_selector(email_sel, timeout=8000)
            await page.fill(email_sel, email)
            await self._delay(0.5, 1)

            # Fill password
            pwd_sel = 'input[type="password"]'
            await page.fill(pwd_sel, password)
            await self._delay(0.5, 1)

            # Click Sign In / Submit
            for submit_sel in [
                'button[type="submit"]',
                'button:text("Sign In")', 'button:text("Log In")',
                'button:text-matches("sign in", "i")',
            ]:
                try:
                    btn = await page.wait_for_selector(submit_sel, timeout=3000)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    continue

            # Wait for redirect to jobs page
            console.print("[magenta]Jobright:[/magenta] Credentials submitted, waiting for redirect…")
            for _ in range(20):
                await asyncio.sleep(2)
                if "jobright.ai/jobs" in page.url or "jobright.ai/dashboard" in page.url:
                    console.print("[green]Jobright: Auto-login successful! Session saved.[/green]")
                    await self._save_session()
                    # Navigate to the recommended jobs page
                    await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=20000)
                    await self._delay(2, 3)
                    return True

            console.print(f"[red]Jobright: Login redirect timeout. Current URL: {page.url}[/red]")
            return False

        except Exception as exc:
            console.print(f"[red]Jobright auto-login error: {exc}[/red]")
            return False

    async def _extract_jobs(self, page) -> list[dict]:
        """Extract job listings using JS evaluation against real DOM structure."""
        jobs = []
        max_pages = 5

        for page_num in range(max_pages):
            if len(jobs) >= self.max_jobs:
                break

            # Scroll to load lazy content
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self._delay(1, 1.5)
            await page.evaluate("window.scrollTo(0, 0)")
            await self._delay(0.5, 1)

            page_jobs = await self._js_extract(page)
            new_jobs = [j for j in page_jobs if j["job_id"] not in {x["job_id"] for x in jobs}]
            jobs.extend(new_jobs)

            if not new_jobs:
                break

            # Try paginating
            clicked = False
            for sel in ['button:text("Next")', '[aria-label="Next page"]', 'button[class*="next"]']:
                try:
                    btn = await page.query_selector(sel)
                    if btn and not await btn.get_attribute("disabled"):
                        await btn.click()
                        await self._delay(2, 3)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break

            # Try to paginate
            next_btn = None
            for sel in ['button:text("Next")', 'a:text("Next")', '[aria-label="Next page"]', 'button[class*="next"]']:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        is_disabled = await btn.get_attribute("disabled")
                        if not is_disabled:
                            next_btn = btn
                            break
                except Exception:
                    continue

            if next_btn:
                await next_btn.click()
                await self._delay(2, 3)
            else:
                break

        return jobs

    async def _js_extract(self, page) -> list[dict]:
        """Use page.evaluate() to extract all job cards using the real Jobright DOM structure."""
        raw = await page.evaluate("""
        () => {
            const JOBRIGHT_BASE = 'https://jobright.ai';
            const cards = document.querySelectorAll('[class*="index_job-card__"]');
            const jobs = [];
            for (const card of cards) {
                try {
                    const titleEl = card.querySelector('h2, h3');
                    const title = titleEl ? titleEl.innerText.trim() : '';
                    if (!title) continue;
                    const linkEl = card.querySelector('a[href*="/jobs/info/"]');
                    const href = linkEl ? linkEl.getAttribute('href') : '';
                    const url = href ? JOBRIGHT_BASE + href : '';
                    if (!url) continue;
                    // All leaf ant-typography text nodes
                    const leaves = [...card.querySelectorAll('.ant-typography')]
                        .filter(e => e.children.length === 0)
                        .map(e => (e.innerText || '').trim())
                        .filter(Boolean);
                    const company = leaves.find(t =>
                        t !== '/' && t !== title && !t.includes('·')
                    ) || '';
                    const salary = leaves.find(t => t.includes('$')) || '';
                    const remoteRaw = leaves.find(t => /^(remote|hybrid|on.?site|onsite)$/i.test(t)) || '';
                    const location = leaves.find(t =>
                        t !== title && t !== company && t !== '/' &&
                        !t.includes('$') && !t.includes('·') &&
                        !/^(remote|hybrid|on.?site|full.?time|part.?time|contract|executive|senior|mid|level|\\d+\\+?\\s*yr)/i.test(t) &&
                        t !== remoteRaw && t.length > 2 && t.length < 60
                    ) || '';
                    jobs.push({ title, company, location, salary, remoteRaw, url });
                } catch(e) {}
            }
            return jobs;
        }
        """)

        results = []
        now = datetime.utcnow().isoformat()
        for item in (raw or []):
            url = item.get("url", "")
            if not url:
                continue
            results.append({
                "job_id": self._make_job_id(url),
                "source": "jobright",
                "title": item.get("title", ""),
                "company": item.get("company", ""),
                "location": item.get("location", ""),
                "salary_raw": item.get("salary", ""),
                "remote_type": _infer_remote_type(item.get("remoteRaw", ""), item.get("location", "")),
                "url": url,
                "description": f"{item.get('title','')} at {item.get('company','')} — {item.get('location','')}",
                "discovered_at": now,
            })
        return results

    async def _parse_card(self, card, page) -> dict | None:
        """Parse a single job card element."""
        try:
            # Extract link
            link_elem = await card.query_selector("a[href]")
            url = ""
            if link_elem:
                href = await link_elem.get_attribute("href")
                if href:
                    if href.startswith("http"):
                        url = href
                    else:
                        url = JOBRIGHT_BASE + href

            if not url:
                return None

            job_id = self._make_job_id(url)

            # Extract title
            title = ""
            for sel in ["h2", "h3", ".job-title", "[class*='title']", "a"]:
                elem = await card.query_selector(sel)
                if elem:
                    text = (await elem.inner_text()).strip()
                    if text and len(text) > 3:
                        title = text
                        break

            # Extract company
            company = ""
            for sel in [".company-name", "[class*='company']", "span[class*='employer']"]:
                elem = await card.query_selector(sel)
                if elem:
                    company = (await elem.inner_text()).strip()
                    break

            # Extract location
            location = ""
            for sel in [".location", "[class*='location']", "[data-testid*='location']"]:
                elem = await card.query_selector(sel)
                if elem:
                    location = (await elem.inner_text()).strip()
                    break

            # Extract salary
            salary_raw = ""
            for sel in [".salary", "[class*='salary']", "[class*='compensation']", "[class*='pay']"]:
                elem = await card.query_selector(sel)
                if elem:
                    salary_raw = (await elem.inner_text()).strip()
                    break

            # Infer remote type from location text
            remote_type = _infer_remote_type(location, "")

            # Get full card text for description snippet
            description = (await card.inner_text()).strip()[:500]

            return {
                "job_id": job_id,
                "source": "jobright",
                "title": title,
                "company": company,
                "location": location,
                "salary_raw": salary_raw,
                "remote_type": remote_type,
                "url": url,
                "description": description,
                "discovered_at": datetime.utcnow().isoformat(),
            }
        except Exception as exc:
            console.print(f"[dim]Jobright card parse error: {exc}[/dim]")
            return None

    async def _extract_from_links(self, page) -> list[dict]:
        """Fallback: extract job info from all links on the page."""
        jobs = []
        try:
            links = await page.query_selector_all("a[href*='/job']")
            seen = set()
            for link in links[:self.max_jobs]:
                href = await link.get_attribute("href")
                if not href or href in seen:
                    continue
                seen.add(href)
                url = href if href.startswith("http") else JOBRIGHT_BASE + href
                title = (await link.inner_text()).strip()
                if not title or len(title) < 5:
                    continue
                jobs.append({
                    "job_id": self._make_job_id(url),
                    "source": "jobright",
                    "title": title,
                    "company": "",
                    "location": "",
                    "salary_raw": "",
                    "remote_type": "unknown",
                    "url": url,
                    "description": "",
                    "discovered_at": datetime.utcnow().isoformat(),
                })
        except Exception as exc:
            console.print(f"[dim]Jobright link fallback error: {exc}[/dim]")
        return jobs

    async def apply(self, job: dict) -> bool:
        """
        Apply via jobright — either 1-click/easy apply or extract external URL.
        """
        console.print(f"\n[magenta]Jobright Apply:[/magenta] Opening {job.get('title')} @ {job.get('company')}")
        page = await self._start_browser()
        submitted = False

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check for Easy Apply / 1-click apply button
            easy_apply_selectors = [
                'button:text-matches("Easy Apply", "i")',
                'button:text-matches("1-Click Apply", "i")',
                'button:text-matches("Quick Apply", "i")',
                'button:text-matches("Apply Now", "i")',
                '[data-testid*="apply"]',
            ]

            apply_btn = None
            for sel in easy_apply_selectors:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn:
                        apply_btn = btn
                        break
                except Exception:
                    continue

            if apply_btn:
                console.print("[magenta]Jobright:[/magenta] Found apply button. Preparing to apply…")

                # Show user a confirmation before clicking
                console.print("\n[bold yellow]PAUSE — Review before applying:[/bold yellow]")
                console.print(f"  Title: {job.get('title')}")
                console.print(f"  Company: {job.get('company')}")
                console.print(f"  URL: {job.get('url')}")
                confirm = input("\n  Confirm apply? [y/N] > ").strip().lower()
                if confirm != "y":
                    console.print("[yellow]Jobright: Application cancelled by user.[/yellow]")
                    return False

                await apply_btn.click()
                await self._delay(2, 3)

                # Handle any modal/confirmation dialog
                for confirm_sel in [
                    'button:text("Submit")',
                    'button:text("Apply")',
                    'button:text("Confirm")',
                ]:
                    try:
                        btn = await page.wait_for_selector(confirm_sel, timeout=3000)
                        if btn:
                            final = input(f"\n  [FINAL] Submit application to {job.get('company')}? [y/N] > ").strip().lower()
                            if final == "y":
                                await btn.click()
                                await self._delay(2, 3)
                                submitted = True
                                console.print("[green]Jobright: Application submitted![/green]")
                            else:
                                console.print("[yellow]Jobright: Final submit cancelled.[/yellow]")
                            break
                    except Exception:
                        continue

                if not submitted:
                    console.print("[yellow]Jobright: Could not find final submit button. Please complete manually.[/yellow]")
                    input("  Press Enter when done (or to skip) > ")

            else:
                # No easy apply — check for external link
                external_selectors = [
                    'a:text-matches("Apply", "i")[href*="http"]',
                    '[data-testid*="external-apply"]',
                    'a[href*="lever.co"]',
                    'a[href*="greenhouse.io"]',
                    'a[href*="workday.com"]',
                    'a[href*="linkedin.com/jobs"]',
                ]
                for sel in external_selectors:
                    try:
                        link = await page.query_selector(sel)
                        if link:
                            href = await link.get_attribute("href")
                            console.print(f"[magenta]Jobright:[/magenta] External application URL: {href}")
                            console.print("[yellow]Please complete application manually at the URL above.[/yellow]")
                            input("  Press Enter when done (or to skip) > ")
                            break
                    except Exception:
                        continue

        except Exception as exc:
            console.print(f"[red]Jobright apply error:[/red] {exc}")
        finally:
            await self._close_browser()

        return submitted


def _infer_remote_type(remote_raw: str, location: str) -> str:
    """Infer remote type from Jobright's explicit remote tag first, then location text."""
    tag = remote_raw.lower().strip()
    if tag == "remote":
        return "remote"
    elif tag == "hybrid":
        return "hybrid"
    elif tag in ("on-site", "onsite", "on site"):
        return "onsite"
    # Fall back to location text
    loc = location.lower()
    if "remote" in loc and "hybrid" not in loc:
        return "remote"
    elif "hybrid" in loc:
        return "hybrid"
    elif any(w in loc for w in ["onsite", "on-site", "on site", "in office"]):
        return "onsite"
    return "unknown"
