"""
USAJobs.gov scraper + application executor.
Searches for GS-15, SES, SL, and target role titles.
User is assumed to be logged in with a saved resume.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from rich.console import Console

from .base import BaseScraper

console = Console()

USAJOBS_BASE = "https://www.usajobs.gov"
USAJOBS_SEARCH = "https://www.usajobs.gov/Search/Results"

# Search queries for USAJobs
USAJOBS_SEARCHES = [
    {"keyword": "IT Director", "grade": "15"},
    {"keyword": "Director Software Engineering", "grade": "15"},
    {"keyword": "Chief Technology Officer", "grade": ""},
    {"keyword": "Chief Information Officer", "grade": ""},
    {"keyword": "Engineering Manager", "grade": "15"},
    {"keyword": "Program Manager", "grade": "15"},
    {"keyword": "Senior Executive", "grade": ""},  # SES positions
    {"keyword": "IT Specialist Director", "grade": "15"},
    {"keyword": "Director Information Technology", "grade": "15"},
]

# User eligibility answers
USER_ELIGIBILITY = {
    "us_citizen": "Yes",
    "clearance": "Top Secret",
    "gs14_experience": "Yes",
    "federal_employee": "No",
    "veterans_preference": "No",
    "schedule_a": "No",
}


class USAJobsScraper(BaseScraper):
    name = "usajobs"

    async def scrape(self) -> list[dict]:
        console.print("[cyan]USAJobs:[/cyan] Opening browser…")
        page = await self._start_browser()
        all_jobs: list[dict] = []
        seen_ids: set[str] = set()

        try:
            await page.goto(USAJOBS_BASE, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check login
            if not await self._is_logged_in(page):
                console.print(
                    "[red]USAJobs:[/red] Not logged in. Please log in at https://www.usajobs.gov "
                    "in the opened browser window."
                )
                input("  Press Enter once logged in > ")
                await page.goto(USAJOBS_BASE, wait_until="domcontentloaded", timeout=30000)
                await self._delay(2, 3)

            for search in USAJOBS_SEARCHES:
                if len(all_jobs) >= self.max_jobs:
                    break
                keyword = search["keyword"]
                grade = search.get("grade", "")
                console.print(f"[cyan]USAJobs:[/cyan] Searching '{keyword}' grade={grade or 'any'}…")
                jobs = await self._search(page, keyword, grade, seen_ids)
                for j in jobs:
                    seen_ids.add(j["job_id"])
                all_jobs.extend(jobs)
                console.print(f"[cyan]USAJobs:[/cyan]   → {len(jobs)} new jobs")
                await self._delay(2, 4)

            console.print(f"[cyan]USAJobs:[/cyan] Total: {len(all_jobs)} jobs found.")
        except Exception as exc:
            console.print(f"[red]USAJobs scrape error:[/red] {exc}")
        finally:
            await self._close_browser()

        return all_jobs

    async def _is_logged_in(self, page) -> bool:
        """Check if user is logged into USAJobs."""
        try:
            # Logged-in users have profile nav or dashboard links
            logged_in_selectors = [
                'a[href*="/Applicant/"]',
                '.usajobs-nav__account-user',
                '[data-testid="logged-in"]',
                'a:text-matches("My Account", "i")',
                'a[href*="/myusajobs"]',
            ]
            for sel in logged_in_selectors:
                elem = await page.query_selector(sel)
                if elem:
                    return True
            return False
        except Exception:
            return False

    async def _search(self, page, keyword: str, grade: str, seen_ids: set) -> list[dict]:
        """Execute a USAJobs search and return job dicts."""
        jobs = []

        # Build search URL
        params = f"?p=1&Keywords={keyword.replace(' ', '+')}"
        if grade:
            params += f"&hp=GS-{grade}"
        params += "&s=Opening+Date&sd=DESC&DatePosted=7"
        url = USAJOBS_SEARCH + params

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._delay(2, 3)

        # Scroll to load results
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self._delay(1, 2)

        # Find job cards
        card_selectors = [
            "li.usajobs-search-result--core",
            ".usajobs-search-result--core",
            "li[class*='search-result']",
            "article[class*='job']",
            ".usajobs-joa-summary",
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
            job = await self._parse_card(card, page)
            if job and job["job_id"] not in seen_ids:
                jobs.append(job)

        return jobs

    async def _parse_card(self, card, page) -> Optional[dict]:
        """Parse a USAJobs job card."""
        try:
            # Title and URL
            link = None
            for sel in [
                "a.usajobs-search-result--core__title",
                "h2 a",
                "h3 a",
                "a[href*='/job/']",
                ".usajobs-search-result--core__title a",
            ]:
                link = await card.query_selector(sel)
                if link:
                    break

            url = ""
            title = ""
            if link:
                href = await link.get_attribute("href") or ""
                if href:
                    url = href if href.startswith("http") else USAJOBS_BASE + href
                title = (await link.inner_text()).strip()

            if not url:
                return None

            # Extract announcement number from URL or card
            announcement_num = _extract_announcement_number(url)
            job_id = announcement_num or self._make_job_id(url)

            # Agency
            agency = ""
            for sel in [
                ".usajobs-search-result--core__department",
                ".usajobs-search-result--core__agency",
                "[class*='department']",
                "[class*='agency']",
                "h3[class*='agency']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    agency = (await elem.inner_text()).strip()
                    break

            # Location
            location = ""
            for sel in [
                ".usajobs-search-result--core__location",
                "[class*='location']",
                "li[class*='location']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    location = (await elem.inner_text()).strip()
                    break

            # Salary range
            salary_raw = ""
            for sel in [
                ".usajobs-search-result--core__salary",
                "[class*='salary']",
                "[class*='pay']",
                "li[class*='salary']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    salary_raw = (await elem.inner_text()).strip()
                    break

            # Grade/series
            grade = ""
            card_text = (await card.inner_text()).strip()
            grade_match = re.search(r"GS-(\d{1,2})", card_text)
            if grade_match:
                grade = f"GS-{grade_match.group(1)}"

            is_ses = bool(re.search(r"\bSES\b|\bSenior Executive\b", card_text, re.IGNORECASE))
            is_sl = bool(re.search(r"\bSL\b|\bSenior Level\b", card_text, re.IGNORECASE))

            # Closing date
            closing_date = ""
            for sel in [
                ".usajobs-search-result--core__close-date",
                "[class*='close-date']",
                "[class*='closing']",
            ]:
                elem = await card.query_selector(sel)
                if elem:
                    closing_date = (await elem.inner_text()).strip()
                    break

            # Remote type
            remote_type = _infer_remote_type(location, card_text)

            # Determine flags
            flags_list = []
            if grade == "GS-15":
                flags_list.append("FEDERAL_ROLE")
                flags_list.append("ALWAYS_APPLY")
            if is_ses:
                flags_list.append("FEDERAL_ROLE")
                flags_list.append("ALWAYS_APPLY")
            if is_sl:
                flags_list.append("FEDERAL_ROLE")
                flags_list.append("ALWAYS_APPLY")
            if "clearance" in card_text.lower() or "secret" in card_text.lower():
                flags_list.append("CLEARED_ROLE")

            return {
                "job_id": job_id,
                "source": "usajobs",
                "title": title,
                "company": agency,
                "location": location,
                "salary_raw": salary_raw,
                "remote_type": remote_type,
                "url": url,
                "description": card_text[:1000],
                "grade": grade,
                "announcement_number": announcement_num,
                "closing_date": closing_date,
                "is_ses": is_ses,
                "is_sl": is_sl,
                "flags": ",".join(flags_list),
                "discovered_at": datetime.utcnow().isoformat(),
            }
        except Exception as exc:
            console.print(f"[dim]USAJobs card parse error: {exc}[/dim]")
            return None

    async def apply(self, job: dict) -> bool:
        """
        Execute USAJobs application flow.
        Selects saved resume, answers questionnaire, pauses before final submit.
        """
        console.print(f"\n[cyan]USAJobs Apply:[/cyan] {job.get('title')} @ {job.get('company')}")
        console.print(f"  Announcement: {job.get('announcement_number', 'N/A')}")
        page = await self._start_browser()
        submitted = False

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Find Apply button
            apply_btn = None
            for sel in [
                'a:text-matches("Apply", "i")',
                'button:text-matches("Apply", "i")',
                '[data-testid*="apply"]',
                '.usajobs-joa-apply__button',
                'a[href*="/apply"]',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        apply_btn = btn
                        break
                except Exception:
                    continue

            if not apply_btn:
                console.print("[yellow]USAJobs: Apply button not found.[/yellow]")
                return False

            await apply_btn.click()
            await self._delay(3, 4)

            # USAJobs typically opens a multi-step process in a new page or modal
            # Handle potential new tab
            pages = page.context.pages
            if len(pages) > 1:
                apply_page = pages[-1]
                await apply_page.wait_for_load_state("domcontentloaded")
            else:
                apply_page = page

            await self._delay(2, 3)

            # Step through the USAJobs application wizard
            max_steps = 15
            step = 0
            while step < max_steps:
                step += 1
                await self._delay(1, 2)
                current_url = apply_page.url
                console.print(f"[dim]USAJobs step {step}: {current_url[:80]}[/dim]")

                # Handle resume selection page
                if "resume" in current_url.lower() or await self._is_resume_page(apply_page):
                    await self._select_resume(apply_page)
                    await self._delay(1, 2)

                # Handle questionnaire page
                elif "questionnaire" in current_url.lower() or "question" in current_url.lower():
                    await self._answer_questionnaire(apply_page)
                    await self._delay(1, 2)

                # Handle documents page
                elif "document" in current_url.lower():
                    console.print("[dim]USAJobs: Documents page — skipping optional docs.[/dim]")

                # Handle review/confirm page
                elif any(kw in current_url.lower() for kw in ["review", "confirm", "submit"]):
                    # FINAL PAUSE
                    console.print("\n[bold yellow]══════════════════════════════════════[/bold yellow]")
                    console.print("[bold yellow]FINAL REVIEW — USAJobs Application:[/bold yellow]")
                    console.print(f"  Title:        {job.get('title')}")
                    console.print(f"  Agency:       {job.get('company')}")
                    console.print(f"  Announcement: {job.get('announcement_number', 'N/A')}")
                    console.print(f"  Grade:        {job.get('grade', 'N/A')}")
                    console.print(f"  URL:          {job.get('url')}")
                    console.print("[bold yellow]══════════════════════════════════════[/bold yellow]")

                    # Show questionnaire answers summary
                    await self._show_review_summary(apply_page)

                    confirm = input("\n  Submit this USAJobs application? [y/N] > ").strip().lower()
                    if confirm == "y":
                        # Click the final submit
                        for sel in [
                            'button:text-matches("Submit", "i")',
                            'button:text-matches("Submit Application", "i")',
                            'input[type="submit"]',
                            'button[type="submit"]',
                        ]:
                            try:
                                btn = await apply_page.wait_for_selector(sel, timeout=5000)
                                if btn:
                                    await btn.click()
                                    await self._delay(3, 5)
                                    submitted = True
                                    console.print("[green]USAJobs: Application submitted![/green]")
                                    break
                            except Exception:
                                continue
                        if not submitted:
                            console.print("[yellow]USAJobs: Could not find submit button. Please submit manually.[/yellow]")
                            input("  Press Enter when done > ")
                    else:
                        console.print("[yellow]USAJobs: Application cancelled by user.[/yellow]")
                    break

                # Navigate to next step
                navigated = await self._click_next(apply_page)
                if not navigated:
                    console.print("[yellow]USAJobs: Could not navigate to next step.[/yellow]")
                    input("  Please navigate manually and press Enter when ready > ")

        except Exception as exc:
            console.print(f"[red]USAJobs apply error:[/red] {exc}")
        finally:
            if submitted:
                await self._delay(3, 4)
            await self._close_browser()

        return submitted

    async def _is_resume_page(self, page) -> bool:
        """Check if current page is the resume selection step."""
        try:
            for sel in [
                "h1:text-matches('Resume', 'i')",
                "h2:text-matches('Resume', 'i')",
                ".usajobs-resume-section",
                'label:text-matches("resume", "i")',
            ]:
                elem = await page.query_selector(sel)
                if elem:
                    return True
        except Exception:
            pass
        return False

    async def _select_resume(self, page) -> None:
        """Select the first available saved resume."""
        console.print("[dim]USAJobs: Selecting saved resume…[/dim]")
        try:
            # Radio buttons for saved resumes
            resume_radios = await page.query_selector_all('input[type="radio"][name*="resume"], input[type="radio"][id*="resume"]')
            if resume_radios:
                first_radio = resume_radios[0]
                is_checked = await first_radio.is_checked()
                if not is_checked:
                    await first_radio.click()
                    await self._delay(0.5, 1)
                console.print("[dim]USAJobs: Resume selected.[/dim]")
            else:
                console.print("[dim]USAJobs: No resume radio found — may already be selected.[/dim]")
        except Exception as exc:
            console.print(f"[dim]USAJobs resume select error: {exc}[/dim]")

    async def _answer_questionnaire(self, page) -> None:
        """Answer USAJobs application questionnaire based on user profile."""
        console.print("[dim]USAJobs: Answering questionnaire…[/dim]")
        try:
            questions = await page.query_selector_all(".usajobs-assessment-question, .question, fieldset")
            for question in questions:
                question_text = (await question.inner_text()).lower()

                # US Citizen
                if any(kw in question_text for kw in ["citizen", "citizenship"]):
                    await self._select_answer(question, "yes")

                # TS Clearance
                elif any(kw in question_text for kw in ["top secret", "ts clearance", "clearance", "secret"]):
                    await self._select_answer(question, "yes")

                # GS-14 specialized experience
                elif "gs-14" in question_text or "specialized experience" in question_text or "one year" in question_text:
                    await self._select_answer(question, "yes")

                # Federal employee
                elif "federal employee" in question_text or "current federal" in question_text:
                    await self._select_answer(question, "no")

                # Veterans preference
                elif "veteran" in question_text:
                    await self._select_answer(question, "no")

                # Schedule A
                elif "schedule a" in question_text or "disability" in question_text:
                    await self._select_answer(question, "no")

        except Exception as exc:
            console.print(f"[dim]USAJobs questionnaire error: {exc}[/dim]")

    async def _select_answer(self, question_elem, answer: str) -> None:
        """Select a Yes/No radio in a question fieldset."""
        try:
            radios = await question_elem.query_selector_all('input[type="radio"]')
            for radio in radios:
                label_for = await radio.get_attribute("id")
                label = None
                if label_for:
                    label = await question_elem.query_selector(f'label[for="{label_for}"]')
                if label:
                    label_text = (await label.inner_text()).lower().strip()
                    if answer in label_text:
                        is_checked = await radio.is_checked()
                        if not is_checked:
                            await radio.click()
                            await self._delay(0.3, 0.5)
                        return
                # Fallback: try value attribute
                val = (await radio.get_attribute("value") or "").lower()
                if answer in val:
                    is_checked = await radio.is_checked()
                    if not is_checked:
                        await radio.click()
                        await self._delay(0.3, 0.5)
                    return
        except Exception:
            pass

    async def _show_review_summary(self, page) -> None:
        """Print the review page content for user inspection."""
        try:
            text = await page.inner_text("body")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            console.print("\n[dim]--- Application Review Summary (first 30 lines) ---[/dim]")
            for line in lines[:30]:
                console.print(f"  [dim]{line}[/dim]")
            console.print("[dim]---[/dim]\n")
        except Exception:
            pass

    async def _click_next(self, page) -> bool:
        """Click the Next/Continue button on the current step. Returns False if not found."""
        next_selectors = [
            'button:text-matches("Continue", "i")',
            'button:text-matches("Next", "i")',
            'input[type="submit"][value*="Next"]',
            'input[type="submit"][value*="Continue"]',
            'a:text-matches("Continue", "i")',
            'button[type="submit"]',
        ]
        for sel in next_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn:
                    is_disabled = await btn.get_attribute("disabled")
                    if not is_disabled:
                        await btn.click()
                        await self._delay(2, 3)
                        return True
            except Exception:
                continue
        return False


def _extract_announcement_number(url: str) -> str:
    """Extract USAJobs announcement number from URL."""
    match = re.search(r"/job/(\d+)", url)
    if match:
        return f"usajobs-{match.group(1)}"
    match = re.search(r"[?&]jvid=([^&]+)", url)
    if match:
        return f"usajobs-{match.group(1)}"
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
    elif "nationwide" in text or "anywhere in the u.s." in text:
        return "remote"
    return "onsite"  # default for federal jobs
