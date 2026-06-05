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
from typing import Optional

from rich.console import Console

from .base import BaseScraper, JobExpiredError

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
            # Go to LinkedIn jobs
            await page.goto(LINKEDIN_BASE, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check login — attempt auto-login from .env if not authenticated
            if "/login" in page.url or "/authwall" in page.url or "uas/login" in page.url or "linkedin.com/feed" not in page.url:
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
                        await page.goto(LINKEDIN_BASE, wait_until="domcontentloaded", timeout=30000)
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
                    await page.goto(LINKEDIN_BASE, wait_until="domcontentloaded", timeout=30000)
                    await self._delay(2, 3)
                    await self._save_session()

            for query in TARGET_SEARCHES:
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

    async def _auto_login(self, page, email: str, password: str) -> bool:
        """Log in to LinkedIn with stored credentials."""
        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=20000)
            await self._delay(1.5, 2.5)

            # Fill email
            await page.wait_for_selector('#username', timeout=8000)
            await page.fill('#username', email)
            await self._delay(0.5, 1)

            # Fill password
            await page.fill('#password', password)
            await self._delay(0.5, 1)

            # Submit
            await page.click('button[type="submit"]')
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

    async def _search_jobs(self, page, query: str, seen_ids: set) -> list[dict]:
        """Search LinkedIn jobs for a query and return job dicts."""
        jobs = []
        # Build search URL with filters:
        # f_TPR=r604800 = last 7 days
        # f_E=4,5 = Director and Executive level
        # f_LF=f_AL = Easy Apply only — sometimes
        search_url = (
            f"{LINKEDIN_JOBS_SEARCH}?keywords={query.replace(' ', '%20')}"
            f"&f_TPR=r604800"
            f"&f_E=4%2C5"
            f"&f_LF=f_AL"   # Easy Apply only
            f"&sortBy=DD"
        )
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await self._delay(2, 3)

        # Scroll to load results
        for _ in range(3):
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
                'button:text-matches("Easy Apply", "i")',
                '[data-control-name="jobdetails_topcard_inapply"]',
                '.jobs-apply-button--top-card',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        easy_apply_btn = btn
                        break
                except Exception:
                    continue

            if not easy_apply_btn:
                console.print("[yellow]LinkedIn: Easy Apply button not found. May not be an Easy Apply job.[/yellow]")
                return self._set_apply_outcome(
                    "linkedin_easy_apply_not_found",
                    "LinkedIn did not expose an Easy Apply button for this job.",
                )

            await easy_apply_btn.click()
            await self._delay(2, 3)

            # Step through modal pages
            max_steps = 10
            step = 0
            while step < max_steps:
                step += 1
                await self._delay(1, 2)

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
                    console.print("[dim]LinkedIn: Modal closed — application may be complete.[/dim]")
                    break

                # Fill form fields in the current step
                await self._fill_easy_apply_fields(page)
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
                else:
                    console.print("[yellow]LinkedIn: No navigable button found. Stopping.[/yellow]")
                    return self._set_apply_outcome(
                        "linkedin_step_blocked",
                        "Easy Apply modal opened, but no Next/Review/Submit control was found on the current step.",
                    )
                    break

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

    async def prepare_session(self, job: Optional[dict] = None) -> None:
        """Open LinkedIn in the persistent profile so the user can refresh login/challenge state."""
        console.print("\n[blue]LinkedIn Session Prep:[/blue] Opening LinkedIn session")
        page = await self._start_browser()
        try:
            target = (job or {}).get("url") or LINKEDIN_BASE
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            if await self._needs_login(page):
                console.print("[yellow]LinkedIn needs login or challenge completion in this browser window.[/yellow]")
            else:
                console.print("[green]LinkedIn session appears authenticated.[/green]")
            if sys.stdin and sys.stdin.isatty():
                input("Press Enter after LinkedIn session is ready > ")
            else:
                console.print("[yellow]Non-interactive run: rerun from Terminal to pause while signing in.[/yellow]")
        finally:
            await self._close_browser()

    async def _needs_login(self, page) -> bool:
        try:
            url = page.url.lower()
            if any(part in url for part in ["/login", "/authwall", "uas/login", "checkpoint", "challenge"]):
                return True
            return await page.evaluate(
                """
                () => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    return /sign in|join linkedin|security verification|checkpoint/.test(text) &&
                        !!document.querySelector('input[type="password"], input[name="session_password"]');
                }
                """
            )
        except Exception:
            return False

    async def _fill_easy_apply_fields(self, page) -> None:
        """
        Attempt to auto-fill common LinkedIn Easy Apply form fields based on user profile.
        """
        await self._delay(0.5, 1)

        # Phone number — fill if empty
        phone_inputs = await page.query_selector_all('input[id*="phone"], input[placeholder*="phone"], input[name*="phone"]')
        for inp in phone_inputs:
            current = await inp.input_value()
            if not current:
                phone = USER_ANSWERS.get("phone_default", "")
                if phone:
                    await inp.fill(phone)
                    await self._delay(0.3, 0.5)

        # Radio/select fields — years of experience, authorization, etc.
        await self._fill_select_fields(page)
        await self._fill_radio_fields(page)
        await self._fill_text_questions(page)

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
