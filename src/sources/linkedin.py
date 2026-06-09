"""
LinkedIn scraper + Easy Apply executor.
Searches for each target role, filters by Easy Apply + last 7 days + Director+.
Handles multi-step Easy Apply forms.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from rich.console import Console

from .base import BaseScraper, JobExpiredError
from src.resume_helper import resolve_resume_path

console = Console()

LINKEDIN_BASE = "https://www.linkedin.com"
LINKEDIN_JOBS_SEARCH = "https://www.linkedin.com/jobs/search/"

TARGET_SEARCHES = [
    "Director of Software Engineering",
    "Director of IT",
    "VP of Software Engineering",
    "VP of IT",
    "AVP Software Engineering",
    "Engineering Manager",
    "CTO",
    "CIO",
    "Program Manager DoD",
    "Director Engineering cleared",
]

LOGIN_URL_MARKERS = ("/login", "/authwall", "uas/login", "checkpoint", "challenge")

# User profile answers for Easy Apply forms
USER_ANSWERS = {
    "years_experience": "18",
    "current_title": "Director of Software Engineering",
    "clearance": "Top Secret",
    "authorized_us": "Yes",
    "require_sponsorship": "No",
    "linkedin_profile_up_to_date": "Yes",
    "willing_to_relocate": "Yes",
    "phone_default": "",  # filled from env if needed
}


class LinkedInScraper(BaseScraper):
    name = "linkedin"

    async def scrape(self) -> list[dict]:
        console.print("[blue]LinkedIn:[/blue] Opening browser…")
        page = await self._start_browser()
        all_jobs: list[dict] = []
        seen_ids: set[str] = set()

        try:
            # Go directly to Jobs. Some authenticated LinkedIn accounts no longer
            # redirect the bare host to /feed, so using /feed as the auth signal
            # causes valid sessions to be treated as logged out.
            await page.goto(f"{LINKEDIN_BASE}/jobs/", wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check login — attempt auto-login from .env if not authenticated
            if await self._needs_login(page):
                email = os.environ.get("LINKEDIN_EMAIL", "")
                password = os.environ.get("LINKEDIN_PASSWORD", "")
                if email and password:
                    console.print("[blue]LinkedIn:[/blue] Not logged in — attempting auto-login…")
                    logged_in = await self._auto_login(page, email, password)
                    if not logged_in:
                        if not (sys.stdin and sys.stdin.isatty()):
                            console.print("[red]LinkedIn: Auto-login failed and running non-interactively. Skipping LinkedIn scrape.[/red]")
                            return []
                        console.print(
                            "\n[yellow]LinkedIn:[/yellow] Auto-login failed.\n"
                            "  → Please log in manually in the browser window.\n"
                            "  → Press Enter once you are logged in and on the feed/jobs page."
                        )
                        input("  Press Enter once logged in > ")
                        await page.goto(f"{LINKEDIN_BASE}/jobs/", wait_until="domcontentloaded", timeout=30000)
                        await self._delay(2, 3)
                        await self._save_session()
                else:
                    if not (sys.stdin and sys.stdin.isatty()):
                        console.print("[red]LinkedIn: Not logged in, no credentials in .env, and running non-interactively. Skipping LinkedIn scrape.[/red]")
                        return []
                    console.print(
                        "\n[yellow]LinkedIn:[/yellow] Not logged in.\n"
                        "  → Please log in to LinkedIn in the browser window.\n"
                        "  → Press Enter once logged in."
                    )
                    input("  Press Enter once logged in > ")
                    await page.goto(f"{LINKEDIN_BASE}/jobs/", wait_until="domcontentloaded", timeout=30000)
                    await self._delay(2, 3)
                    await self._save_session()

            searches = self.config.get("target_roles") or TARGET_SEARCHES
            for query in searches:
                if len(all_jobs) >= self.max_jobs:
                    break
                console.print(f"[blue]LinkedIn:[/blue] Searching '{query}'…")
                jobs = await self._search_jobs(page, query, seen_ids)
                all_jobs.extend(jobs)
                for j in jobs:
                    seen_ids.add(j["job_id"])
                console.print(f"[blue]LinkedIn:[/blue]   → {len(jobs)} new jobs")
                await self._delay(2, 4)

            console.print(f"[blue]LinkedIn:[/blue] Total: {len(all_jobs)} jobs found.")
        except Exception as exc:
            console.print(f"[red]LinkedIn scrape error:[/red] {exc}")
        finally:
            await self._close_browser()

        return all_jobs

    async def scrape_saved(self) -> list[dict]:
        """Import jobs the user explicitly saved in LinkedIn.

        A saved job is treated as user intent to apply, so callers should mark
        these as approved without running the normal score gate.
        """
        console.print("[blue]LinkedIn Saved:[/blue] Opening browser…")
        page = await self._start_browser()
        jobs: list[dict] = []
        seen_ids: set[str] = set()

        try:
            await page.goto("https://www.linkedin.com/my-items/saved-jobs/", wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            if await self._needs_login(page):
                email = os.environ.get("LINKEDIN_EMAIL", "")
                password = os.environ.get("LINKEDIN_PASSWORD", "")
                if email and password:
                    console.print("[blue]LinkedIn Saved:[/blue] Not logged in — attempting auto-login…")
                    if not await self._auto_login(page, email, password):
                        console.print("[red]LinkedIn Saved: Auto-login failed. Skipping saved-job import.[/red]")
                        return []
                    await page.goto("https://www.linkedin.com/my-items/saved-jobs/", wait_until="domcontentloaded", timeout=30000)
                    await self._delay(2, 3)
                else:
                    console.print("[red]LinkedIn Saved: Missing LINKEDIN_EMAIL/LINKEDIN_PASSWORD. Skipping saved-job import.[/red]")
                    return []

            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self._delay(1, 2)

            card_selectors = [
                ".job-card-container",
                ".jobs-saved-job-card",
                ".reusable-search__result-container",
                "li:has(a[href*='/jobs/view/'])",
            ]
            cards = []
            for sel in card_selectors:
                try:
                    found = await page.query_selector_all(sel)
                    if found:
                        cards = found
                        break
                except Exception:
                    continue

            for card in cards[:self.max_jobs]:
                job = await self._parse_card(card, page, "saved")
                if job and job["job_id"] not in seen_ids:
                    await self._hydrate_job_detail(page, job)
                    jobs.append(self._mark_saved_job(job))
                    seen_ids.add(job["job_id"])

            if not jobs:
                fallback_jobs = await self._extract_jobs_from_page(page, "saved", seen_ids)
                for job in fallback_jobs:
                    await self._hydrate_job_detail(page, job)
                    jobs.append(self._mark_saved_job(job))

            console.print(f"[blue]LinkedIn Saved:[/blue] Total: {len(jobs)} saved jobs found.")
        except Exception as exc:
            console.print(f"[red]LinkedIn saved-job import error:[/red] {exc}")
        finally:
            await self._close_browser()

        return jobs

    def _mark_saved_job(self, job: dict) -> dict:
        job["status"] = "approved"
        job["score"] = max(int(job.get("score") or 0), 100)
        job["score_reason"] = "User saved this job in LinkedIn; approved regardless of score."
        job["flags"] = ",".join(filter(None, [job.get("flags", ""), "linkedin_saved"]))
        job["recommended_action"] = "apply"
        job["saved_on_linkedin"] = True
        return job

    async def _hydrate_job_detail(self, page, job: dict) -> None:
        """Fill missing saved-job fields from the LinkedIn job detail page."""
        if job.get("title") and job.get("company"):
            return
        url = job.get("url")
        if not url:
            return
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._delay(1, 2)
            title = await self._first_text(page, [
                ".job-details-jobs-unified-top-card__job-title",
                ".jobs-unified-top-card__job-title",
                "h1",
            ])
            company = await self._first_text(page, [
                ".job-details-jobs-unified-top-card__company-name",
                ".jobs-unified-top-card__company-name",
                "a[href*='/company/']",
            ])
            location = await self._first_text(page, [
                ".job-details-jobs-unified-top-card__tertiary-description-container",
                ".jobs-unified-top-card__bullet",
                ".jobs-unified-top-card__workplace-type",
            ])
            description = await self._first_text(page, [
                ".jobs-description-content__text",
                "#job-details",
                ".jobs-box__html-content",
            ])
            if not title:
                page_title = await page.title()
                title_parts = [part.strip() for part in page_title.split("|")]
                if title_parts and title_parts[-1].lower() == "linkedin":
                    title_parts = title_parts[:-1]
                if title_parts:
                    title = title_parts[0]
                if not company and len(title_parts) > 1:
                    company = title_parts[1]
            if title:
                job["title"] = title
            if company:
                job["company"] = company
            if location and not job.get("location"):
                job["location"] = location
                job["remote_type"] = _infer_remote_type(location, "")
            if description:
                job["description"] = description[:5000]
        except Exception as exc:
            console.print(f"[dim]LinkedIn saved detail hydration error: {exc}[/dim]")

    async def _first_text(self, page, selectors: list[str]) -> str:
        for sel in selectors:
            try:
                elem = await page.query_selector(sel)
                if elem:
                    text = (await elem.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    async def _auto_login(self, page, email: str, password: str) -> bool:
        """Log in to LinkedIn with stored credentials."""
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=20000)
            await self._delay(1.5, 2.5)
            if await self._needs_login(page) is False:
                console.print("[green]LinkedIn: Already authenticated after login redirect.[/green]")
                await self._save_session()
                return True

            # Fill email
            username_selectors = [
                '#username',
                'input[name="session_key"]',
                'input[autocomplete="username"]',
                'input[type="email"]',
            ]
            password_selectors = [
                '#password',
                'input[name="session_password"]',
                'input[autocomplete="current-password"]',
                'input[type="password"]',
            ]
            username_filled = await self._fill_first_available(page, username_selectors, email, timeout=10000)
            if not username_filled:
                console.print(f"[red]LinkedIn: Login form not found. Current URL: {page.url}[/red]")
                return False
            await self._delay(0.5, 1)

            # Fill password
            password_field = await self._fill_first_available(page, password_selectors, password, timeout=8000)
            password_filled = bool(password_field)
            if not password_filled:
                console.print(f"[red]LinkedIn: Password field not found. Current URL: {page.url}[/red]")
                return False
            await self._delay(0.5, 1)

            # Submit
            submitted = False
            for sel in ['button[type="submit"]', 'button:text-matches("Sign in", "i")']:
                try:
                    await page.click(sel, timeout=5000)
                    submitted = True
                    break
                except Exception:
                    continue
            if not submitted:
                try:
                    await password_field.press("Enter")
                    submitted = True
                except Exception:
                    console.print(f"[red]LinkedIn: Sign-in button not found. Current URL: {page.url}[/red]")
                    return False
            console.print("[blue]LinkedIn:[/blue] Credentials submitted, waiting for redirect…")

            # Wait for feed or jobs page
            for _ in range(20):
                await asyncio.sleep(2)
                cur = page.url
                if "linkedin.com/feed" in cur or "linkedin.com/jobs" in cur or "linkedin.com/mynetwork" in cur:
                    console.print("[green]LinkedIn: ✓ Auto-login successful! Session saved.[/green]")
                    await self._save_session()
                    return True
                if "checkpoint" in cur or "challenge" in cur:
                    console.print("[yellow]LinkedIn: Security checkpoint detected — manual action needed.[/yellow]")
                    return False

            console.print(f"[red]LinkedIn: Login timeout. URL: {page.url}[/red]")
            return False
        except Exception as exc:
            console.print(f"[red]LinkedIn auto-login error: {exc}[/red]")
            return False

    async def _fill_first_available(self, page, selectors: list[str], value: str, timeout: int = 5000):
        """Fill the first visible matching input from a selector list."""
        deadline = asyncio.get_event_loop().time() + (timeout / 1000)
        while asyncio.get_event_loop().time() < deadline:
            for sel in selectors:
                try:
                    fields = await page.query_selector_all(sel)
                    for field in fields:
                        if await field.is_visible():
                            await field.fill(value)
                            return field
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        return None

    async def _search_jobs(self, page, query: str, seen_ids: set) -> list[dict]:
        """Search LinkedIn jobs for a query and return job dicts."""
        jobs = []
        # Build search URL with filters:
        # f_TPR=r604800 = last 7 days
        # f_E=4,5 = Director and Executive level
        # f_LF=f_AL = Easy Apply only — sometimes
        search_url = (
            f"{LINKEDIN_JOBS_SEARCH}?keywords={quote_plus(query)}"
            f"&f_TPR=r604800"
            f"&f_E=4%2C5"
            f"&f_LF=f_AL"   # Easy Apply only
            f"&sortBy=DD"
        )
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await self._delay(2, 3)
        if await self._needs_login(page):
            console.print("[yellow]LinkedIn:[/yellow] Search redirected to login/authwall; no jobs collected for this query.")
            return []

        # Scroll to load results
        results_container_selectors = [
            ".jobs-search-results-list",
            ".scaffold-layout__list",
            "ul.jobs-search-results__list",
        ]
        for _ in range(4):
            scrolled = False
            for sel in results_container_selectors:
                try:
                    container = await page.query_selector(sel)
                    if container:
                        await container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
                        scrolled = True
                        break
                except Exception:
                    continue
            if not scrolled:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self._delay(1, 2)

        # Get all job cards
        card_selectors = [
            ".job-card-container",
            ".jobs-search-results__list-item",
            "li.scaffold-layout__list-item",
            ".job-card-list__entity-lockup",
        ]
        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    break
            except Exception:
                continue

        for card in cards[:self.max_jobs - len(seen_ids)]:
            job = await self._parse_card(card, page, query)
            if job and job["job_id"] not in seen_ids:
                jobs.append(job)

        if not jobs:
            jobs = await self._extract_jobs_from_page(page, query, seen_ids)

        return jobs

    async def _extract_jobs_from_page(self, page, query: str, seen_ids: set) -> list[dict]:
        """Fallback parser for LinkedIn DOM changes where card selectors move."""
        try:
            raw_jobs = await page.evaluate(
                """
                () => {
                    const anchors = [...document.querySelectorAll('a[href*="/jobs/view/"]')];
                    const byUrl = new Map();
                    for (const anchor of anchors) {
                        const href = anchor.href || anchor.getAttribute('href') || '';
                        if (!href) continue;
                        const url = href.split('?')[0];
                        if (byUrl.has(url)) continue;
                        const card =
                            anchor.closest('.job-card-container, .jobs-search-results__list-item, li.scaffold-layout__list-item') ||
                            anchor.closest('li') ||
                            anchor.parentElement;
                        const text = (card?.innerText || anchor.innerText || '').trim();
                        const lines = text.split('\\n').map(line => line.trim()).filter(Boolean);
                        byUrl.set(url, {
                            url,
                            title: lines[0] || anchor.innerText.trim(),
                            company: lines[1] || '',
                            location: lines.find(line => /remote|hybrid|united states|miami|washington|dc|fl/i.test(line)) || '',
                            description: text.slice(0, 500),
                            has_easy_apply: /easy apply/i.test(text)
                        });
                    }
                    return [...byUrl.values()];
                }
                """
            )
        except Exception as exc:
            console.print(f"[dim]LinkedIn fallback extraction error: {exc}[/dim]")
            return []

        jobs = []
        for raw in raw_jobs[: self.max_jobs - len(seen_ids)]:
            url = raw.get("url", "")
            if not url:
                continue
            if not url.startswith("http"):
                url = LINKEDIN_BASE + url
            job_id = self._make_job_id(url)
            if job_id in seen_ids:
                continue
            jobs.append({
                "job_id": job_id,
                "source": "linkedin",
                "title": raw.get("title", ""),
                "company": raw.get("company", ""),
                "location": raw.get("location", ""),
                "salary_raw": "",
                "remote_type": _infer_remote_type(raw.get("location", ""), ""),
                "url": url,
                "description": raw.get("description", ""),
                "has_easy_apply": bool(raw.get("has_easy_apply")),
                "search_query": query,
                "discovered_at": datetime.utcnow().isoformat(),
            })
        if jobs:
            console.print(f"[blue]LinkedIn:[/blue] Fallback parser recovered {len(jobs)} job cards.")
        return jobs

    async def _parse_card(self, card, page, query: str) -> Optional[dict]:
        """Parse a LinkedIn job card."""
        try:
            # Get link
            link = await card.query_selector("a[href*='/jobs/view/']")
            url = ""
            if link:
                href = await link.get_attribute("href")
                if href:
                    # Clean tracking params
                    url = href.split("?")[0]
                    if not url.startswith("http"):
                        url = LINKEDIN_BASE + url

            if not url:
                return None

            job_id = self._make_job_id(url)

            # Title
            title = ""
            for sel in [
                ".job-card-list__title",
                ".job-card-container__link",
                "a[href*='/jobs/view/'] span",
                ".artdeco-entity-lockup__title",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    t = (await elem.inner_text()).strip()
                    if t:
                        title = t
                        break

            # Company
            company = ""
            for sel in [
                ".job-card-container__primary-description",
                ".job-card-container__company-name",
                ".artdeco-entity-lockup__subtitle",
                "span[class*='company']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    c = (await elem.inner_text()).strip()
                    if c:
                        company = c
                        break

            # Location
            location = ""
            for sel in [
                ".job-card-container__metadata-item",
                ".job-card-list__footer-wrapper li",
                "li[class*='location']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    loc = (await elem.inner_text()).strip()
                    if loc:
                        location = loc
                        break

            # Easy Apply badge
            has_easy_apply = False
            for sel in [
                ".job-card-container__apply-method",
                'span:text-matches("Easy Apply", "i")',
                'li-icon[type="linkedin-bug"]',
            ]:
                try:
                    badge = await card.query_selector(sel)
                    if badge:
                        badge_text = (await badge.inner_text()).lower()
                        if "easy apply" in badge_text or badge_text == "":
                            has_easy_apply = True
                            break
                except Exception:
                    continue

            # Salary (LinkedIn often doesn't show it on the card)
            salary_raw = ""
            for sel in ["[class*='salary']", "[class*='compensation']"]:
                elem = await card.query_selector(sel)
                if elem:
                    salary_raw = (await elem.inner_text()).strip()
                    break

            remote_type = _infer_remote_type(location, "")
            card_text = (await card.inner_text()).strip()

            return {
                "job_id": job_id,
                "source": "linkedin",
                "title": title,
                "company": company,
                "location": location,
                "salary_raw": salary_raw,
                "remote_type": remote_type,
                "url": url,
                "description": card_text[:500],
                "has_easy_apply": has_easy_apply,
                "search_query": query,
                "discovered_at": datetime.utcnow().isoformat(),
            }
        except Exception as exc:
            console.print(f"[dim]LinkedIn card parse error: {exc}[/dim]")
            return None

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        """
        Execute LinkedIn Easy Apply for an approved job.
        Steps through the multi-page form and pauses before final submit.
        """
        self.last_apply_status = "started"
        self.last_apply_detail = ""
        console.print(f"\n[blue]LinkedIn Apply:[/blue] {job.get('title')} @ {job.get('company')}")
        tailored_resume_path = await self._tailor_resume_with_jobright(job)
        resume_path = tailored_resume_path or self._configured_resume_path()
        if tailored_resume_path:
            console.print(f"[blue]LinkedIn Apply:[/blue] Using tailored resume: {tailored_resume_path}")
        elif resume_path:
            console.print(f"[yellow]LinkedIn Apply:[/yellow] Tailored resume unavailable; using configured resume: {resume_path}")
        else:
            console.print("[yellow]LinkedIn Apply:[/yellow] No tailored or configured resume path available.")
        page = await self._start_browser()
        submitted = False

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            if await self._needs_login(page):
                return self._set_apply_outcome(
                    "linkedin_login_required",
                    "LinkedIn redirected to login/authwall. Run prepare-sessions --source linkedin and sign in once.",
                )

            # Check if job is expired/closed
            page_text = await page.evaluate("document.body.innerText")
            if any(w in page_text.lower() for w in ["no longer accepting applications", "job is closed", "no longer available"]):
                console.print(f"[yellow]LinkedIn:[/yellow] Job no longer accepting applications — skipping.")
                raise JobExpiredError("LinkedIn: Job is closed or no longer accepting applications.")


            # Find and click Easy Apply button
            easy_apply_btn = None
            for sel in [
                'button.jobs-apply-button',
                'a[href*="/jobs/view/"][href*="/apply/"]',
                'a:text-matches("^Continue$", "i")',
                'button:text-matches("^Continue$", "i")',
                'button:text-matches("Easy Apply", "i")',
                '[data-control-name="jobdetails_topcard_inapply"]',
                '.jobs-apply-button--top-card',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        try:
                            text = (await btn.inner_text()).strip().lower()
                            href = (await btn.get_attribute("href") or "").lower()
                            if text == "continue" and "/apply/" not in href:
                                continue
                        except Exception:
                            pass
                        easy_apply_btn = btn
                        break
                except Exception:
                    continue

            if not easy_apply_btn:
                console.print("[yellow]LinkedIn: Easy Apply button not found. May not be an Easy Apply job.[/yellow]")
                external_url = await self._extract_external_apply_url(page)
                if not external_url:
                    return self._set_apply_outcome(
                        "linkedin_easy_apply_not_found",
                        "LinkedIn did not expose an Easy Apply button or a usable external apply link for this job.",
                    )
                console.print(f"[blue]LinkedIn Apply:[/blue] External apply URL found: {external_url[:100]}")
                return await self._apply_external_ats(job, external_url, resume_path, auto_submit=auto_submit)

            await easy_apply_btn.click()
            await self._delay(2, 3)

            # Step through modal pages
            max_steps = 10
            step = 0
            while step < max_steps:
                step += 1
                await self._delay(1, 2)
                try:
                    heading = await page.evaluate(
                        """
                        () => {
                            const h = document.querySelector('h1,h2,h3');
                            const pageText = document.body?.innerText?.match(/\\d+\\/\\d+ pages/i)?.[0] || '';
                            return [pageText, h?.innerText || ''].filter(Boolean).join(' — ');
                        }
                        """
                    )
                    if heading:
                        console.print(f"[dim]LinkedIn apply step {step}: {heading[:140]}[/dim]")
                except Exception:
                    pass

                # Check if modal is open
                modal = None
                for sel in ['.jobs-easy-apply-modal', '[data-test-modal]', '.artdeco-modal']:
                    try:
                        modal = await page.query_selector(sel)
                        if modal:
                            break
                    except Exception:
                        continue

                if not modal:
                    if "/apply/" in page.url:
                        modal = page
                    else:
                        console.print("[dim]LinkedIn: Modal closed — application may be complete.[/dim]")
                        break

                # Fill form fields in the current step
                await self._fill_easy_apply_fields(page, resume_path=resume_path)
                await self._delay(1, 1.5)

                # Check for Next / Review / Submit buttons
                next_btn = None
                submit_btn = None
                review_btn = None

                for sel in ['button[aria-label*="Submit application"]', 'button:text-matches("Submit application", "i")']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            submit_btn = btn
                            break
                    except Exception:
                        continue

                for sel in ['button[aria-label*="Review"]', 'button:text-matches("Review", "i")']:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            review_btn = btn
                            break
                    except Exception:
                        continue

                for sel in [
                    'button[aria-label*="Continue to next step"]',
                    'button:text-matches("Next", "i")',
                    'button[data-easy-apply-next-button]',
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and not submit_btn:
                            next_btn = btn
                            break
                    except Exception:
                        continue

                if submit_btn:
                    # FINAL PAUSE — show user summary before submitting
                    console.print("\n[bold yellow]══════════════════════════════════════[/bold yellow]")
                    console.print("[bold yellow]FINAL REVIEW — About to submit application:[/bold yellow]")
                    console.print(f"  Title:   {job.get('title')}")
                    console.print(f"  Company: {job.get('company')}")
                    console.print(f"  URL:     {job.get('url')}")
                    console.print("[bold yellow]══════════════════════════════════════[/bold yellow]")
                    if auto_submit:
                        console.print("[green]LinkedIn: Auto-submitting application (auto-submit active)![/green]")
                        confirm = "y"
                    else:
                        try:
                            confirm = input("\n  Submit this LinkedIn application? [y/N] > ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            confirm = "n"
                    if confirm == "y":
                        await submit_btn.click()
                        await self._delay(3, 4)
                        submitted = True
                        console.print("[green]LinkedIn: Application submitted![/green]")
                    else:
                        console.print("[yellow]LinkedIn: Application cancelled by user.[/yellow]")
                        self._set_apply_outcome(
                            "submission_cancelled",
                            "Final LinkedIn submission was not confirmed by the user.",
                        )
                    break
                elif review_btn:
                    await review_btn.click()
                    await self._delay(2, 2)
                elif next_btn:
                    await next_btn.click()
                    await self._delay(1, 2)
                elif await self._click_linkedin_button_by_text(page, [r"^Next$", r"^Continue$"]):
                    await self._delay(1, 2)
                else:
                    console.print("[yellow]LinkedIn: No navigable button found. Stopping.[/yellow]")
                    return self._set_apply_outcome(
                        "linkedin_step_blocked",
                        "Easy Apply modal opened, but no Next/Review/Submit control was found on the current step.",
                    )
                    break

            if step >= max_steps and not submitted and self.last_apply_status in ("started", "", None):
                self._set_apply_outcome(
                    "linkedin_max_steps_reached",
                    f"LinkedIn apply flow reached {max_steps} steps without exposing a final Submit control.",
                )

        except Exception as exc:
            console.print(f"[red]LinkedIn apply error:[/red] {exc}")
            self._set_apply_outcome("linkedin_error", str(exc))
        finally:
            # Keep browser open briefly so user can see result
            if submitted:
                await self._delay(3, 4)
            await self._close_browser()

        if submitted:
            self.last_apply_status = "submitted"
            self.last_apply_detail = "LinkedIn application submitted successfully."
        elif self.last_apply_status in ("started", "", None):
            self._set_apply_outcome(
                "linkedin_not_submitted",
                "LinkedIn apply flow ended without reaching a submitted state.",
            )
        return submitted

    async def _tailor_resume_with_jobright(self, job: dict) -> str:
        try:
            from .jobright import JobrightScraper
            return await JobrightScraper(self.config).tailor_resume_for_external_job(job)
        except Exception as exc:
            console.print(f"[yellow]LinkedIn Apply:[/yellow] Jobright resume tailoring failed: {exc}")
            return ""

    def _configured_resume_path(self) -> str:
        return resolve_resume_path(self.config)

    async def _extract_external_apply_url(self, page) -> str:
        """Find or reveal the external company apply URL on a LinkedIn job page."""
        url = await page.evaluate("""
        () => {
            const direct = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href)
                .find(h => h && !h.includes('linkedin.com') && /apply|job|career|workday|greenhouse|lever|icims|brassring|taleo|smartrecruiters|successfactors|ashby/i.test(h));
            if (direct) return direct;

            const buttons = Array.from(document.querySelectorAll('button,a,[role="button"]'));
            const apply = buttons.find(el => /\\bapply\\b/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
            return apply?.href || '';
        }
        """)
        if url and "linkedin.com" not in url:
            return url

        apply_btn = None
        for sel in [
            'button:text-matches("^Apply$", "i")',
            'a:text-matches("^Apply$", "i")',
            'button:text-matches("Apply on company site", "i")',
            'a:text-matches("Apply on company site", "i")',
            '.jobs-apply-button',
        ]:
            try:
                candidate = await page.query_selector(sel)
                if candidate:
                    text = (await candidate.inner_text()).strip().lower()
                    if "easy apply" not in text:
                        apply_btn = candidate
                        break
            except Exception:
                continue
        if not apply_btn:
            return ""

        original_pages = set(page.context.pages)
        try:
            async with page.expect_popup(timeout=8000) as popup_info:
                await apply_btn.click()
            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded", timeout=20000)
            external = popup.url
            if external and "linkedin.com" not in external:
                return external
        except Exception:
            try:
                await apply_btn.click()
                await self._delay(2, 3)
                for opened in page.context.pages:
                    if opened not in original_pages and "linkedin.com" not in opened.url:
                        return opened.url
                if page.url and "linkedin.com" not in page.url:
                    return page.url
            except Exception:
                return ""
        return ""

    async def _apply_external_ats(self, job: dict, external_url: str, resume_path: str, auto_submit: bool = False) -> bool:
        try:
            from .jobright import JobrightScraper
            scraper = JobrightScraper(self.config)
            result = await scraper.apply_external_ats_job(
                job,
                external_url,
                resume_path=resume_path,
                auto_submit=auto_submit,
            )
            self.last_apply_status = scraper.last_apply_status
            self.last_apply_detail = scraper.last_apply_detail
            return result
        except Exception as exc:
            return self._set_apply_outcome("linkedin_external_apply_error", str(exc))

    async def _click_linkedin_button_by_text(self, page, patterns: list[str]) -> bool:
        try:
            return await page.evaluate(
                """
                (patterns) => {
                    const regexes = patterns.map(p => new RegExp(p, 'i'));
                    const controls = Array.from(document.querySelectorAll('button,a,[role="button"]'));
                    const match = controls.find(el => {
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                        const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
                        if (!text) return false;
                        return regexes.some(re => re.test(text));
                    });
                    if (!match) return false;
                    match.scrollIntoView({block: 'center', inline: 'center'});
                    match.click();
                    return true;
                }
                """,
                patterns,
            )
        except Exception:
            return False

    async def prepare_session(self, job: Optional[dict] = None) -> None:
        """Open LinkedIn in the persistent profile so the user can refresh login/challenge state."""
        console.print("\n[blue]LinkedIn Session Prep:[/blue] Opening LinkedIn session")
        page = await self._start_browser()
        try:
            target = (job or {}).get("url") or LINKEDIN_BASE
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            if await self._needs_login(page):
                # Session missing or expired — need manual login.
                console.print("[yellow]LinkedIn needs login or challenge completion in this browser window.[/yellow]")
                if sys.stdin and sys.stdin.isatty():
                    input("Press Enter after LinkedIn session is ready > ")
                else:
                    console.print("[yellow]Non-interactive run: rerun from Terminal to sign in once.[/yellow]")
            else:
                # Session is already good — no interaction needed.
                console.print("[green]LinkedIn session is authenticated — no action needed.[/green]")
        finally:
            await self._close_browser()

    async def _needs_login(self, page) -> bool:
        try:
            url = page.url.lower()
            if any(part in url for part in LOGIN_URL_MARKERS):
                return True
            return await page.evaluate(
                """
                () => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    return /sign in|join linkedin|security verification|checkpoint|authwall/.test(text) &&
                        !!document.querySelector('input[type="password"], input[name="session_password"]');
                }
                """
            )
        except Exception:
            return False

    async def _fill_easy_apply_fields(self, page, resume_path: str = "") -> None:
        """
        Attempt to auto-fill common LinkedIn Easy Apply form fields based on user profile.
        """
        await self._delay(0.5, 1)
        if resume_path:
            await self._upload_resume_if_prompted(page, resume_path)

        # Phone number — fill if empty
        phone_inputs = await page.query_selector_all(
            'input[id*="phone"], input[placeholder*="phone"], input[name*="phone"], '
            'input[aria-label*="phone" i], input[type="tel"]'
        )
        for inp in phone_inputs:
            current = await inp.input_value()
            if not current:
                phone = USER_ANSWERS.get("phone_default", "") or self._profile_value("personal_info", "phone")
                if phone:
                    await inp.fill(phone)
                    await self._delay(0.3, 0.5)

        # Radio/select fields — years of experience, authorization, etc.
        await self._fill_select_fields(page)
        await self._fill_radio_fields(page)
        await self._fill_text_questions(page)

    async def _upload_resume_if_prompted(self, page, resume_path: str) -> None:
        path = Path(resume_path).expanduser()
        if not path.exists():
            return
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
            for file_input in file_inputs:
                accept = (await file_input.get_attribute("accept") or "").lower()
                name = (await file_input.get_attribute("name") or "").lower()
                label = (await self._get_field_label(page, file_input) or "").lower()
                hints = " ".join([accept, name, label])
                if accept and not any(ext in accept for ext in [".pdf", "pdf", "application/pdf"]):
                    continue
                if any(word in hints for word in ["resume", "cv", "upload", "file"]) or not hints.strip():
                    await file_input.set_input_files(str(path))
                    console.print(f"[green]LinkedIn:[/green] Uploaded resume: {path.name}")
                    await self._delay(1, 2)
        except Exception as exc:
            console.print(f"[yellow]LinkedIn:[/yellow] Resume upload check failed: {exc}")

    async def _fill_select_fields(self, page) -> None:
        """Fill dropdown selects based on common LinkedIn question patterns."""
        selects = await page.query_selector_all("select")
        for sel_elem in selects:
            try:
                label_text = await self._get_field_label(page, sel_elem)
                label_lower = label_text.lower()
                options = await sel_elem.query_selector_all("option")
                option_values = []
                for opt in options:
                    val = await opt.get_attribute("value")
                    text = await opt.inner_text()
                    option_values.append((val, text.strip().lower()))

                chosen = None
                if "experience" in label_lower or "years" in label_lower:
                    # Pick highest matching option >= 18
                    for val, text in option_values:
                        try:
                            num = int(re.search(r"\d+", text).group())
                            if num >= 10:
                                chosen = val
                        except Exception:
                            pass
                    if not chosen:
                        # Try "10+" or "15+" style
                        for val, text in option_values:
                            if "10+" in text or "15+" in text or "16+" in text or "18+" in text or "20+" in text:
                                chosen = val
                                break
                elif "authorized" in label_lower or "eligible" in label_lower or "citizen" in label_lower:
                    for val, text in option_values:
                        if "yes" in text:
                            chosen = val
                            break
                elif "sponsor" in label_lower:
                    for val, text in option_values:
                        if "no" in text:
                            chosen = val
                            break
                elif "phone" in label_lower and "country" in label_lower:
                    for val, text in option_values:
                        if "united states" in text or "(+1)" in text:
                            chosen = val
                            break

                if chosen:
                    await sel_elem.select_option(value=chosen)
                    await self._delay(0.3, 0.5)
            except Exception:
                continue

    async def _fill_radio_fields(self, page) -> None:
        """Handle radio button questions."""
        # Find all fieldsets or divs containing radio groups
        radio_groups = await page.query_selector_all('fieldset, div[class*="question"]')
        for group in radio_groups:
            try:
                label_elem = await group.query_selector("legend, label")
                label_text = ""
                if label_elem:
                    label_text = (await label_elem.inner_text()).lower()

                radios = await group.query_selector_all('input[type="radio"]')
                if not radios:
                    continue

                chosen_radio = None
                if any(kw in label_text for kw in ["authorized", "citizen", "eligible", "cleared"]):
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        if val in ("yes", "true", "1"):
                            chosen_radio = radio
                            break
                elif "sponsor" in label_text:
                    for radio in radios:
                        val = (await radio.get_attribute("value") or "").lower()
                        if val in ("no", "false", "0"):
                            chosen_radio = radio
                            break

                if chosen_radio:
                    is_checked = await chosen_radio.is_checked()
                    if not is_checked:
                        await chosen_radio.click()
                        await self._delay(0.3, 0.5)
            except Exception:
                continue

    async def _fill_text_questions(self, page) -> None:
        """Fill short text answer boxes."""
        text_inputs = await page.query_selector_all('input[type="text"], textarea')
        for inp in text_inputs:
            try:
                current = await inp.input_value()
                if current:
                    continue  # already filled
                label_text = await self._get_field_label(page, inp)
                label_lower = label_text.lower()

                answer = None
                if "city" in label_lower:
                    answer = "Miami"
                elif "state" in label_lower:
                    answer = "Florida"
                elif "years" in label_lower and "experience" in label_lower:
                    answer = "18"

                if answer:
                    await inp.fill(answer)
                    await self._delay(0.3, 0.5)
            except Exception:
                continue

    async def _get_field_label(self, page, element) -> str:
        """Try to find the label text for an input element."""
        try:
            field_id = await element.get_attribute("id")
            if field_id:
                label = await page.query_selector(f'label[for="{field_id}"]')
                if label:
                    return (await label.inner_text()).strip()
            # Try parent/sibling label
            parent = await element.evaluate_handle("el => el.closest('.form-group, .jobs-easy-apply-form-element, fieldset, div')")
            if parent:
                label = await parent.query_selector("label, legend, span[class*='label']")
                if label:
                    return (await label.inner_text()).strip()
        except Exception:
            pass
        return ""

    def _profile_value(self, section: str, key: str) -> str:
        try:
            import json
            profile_path = Path("state/profile.json")
            if not profile_path.exists():
                profile_path = Path(__file__).parent.parent.parent / "state" / "profile.json"
            if not profile_path.exists():
                return ""
            data = json.loads(profile_path.read_text())
            return str(data.get(section, {}).get(key, "") or "")
        except Exception:
            return ""


def _infer_remote_type(location: str, description: str) -> str:
    text = f"{location} {description}".lower()
    if "remote" in text and "hybrid" not in text:
        return "remote"
    elif "hybrid" in text:
        return "hybrid"
    elif any(w in text for w in ["onsite", "on-site", "on site", "in office", "in-office"]):
        return "onsite"
    elif "remote" in text:
        return "remote"
    return "unknown"
