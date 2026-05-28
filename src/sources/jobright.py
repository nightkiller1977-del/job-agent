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
            # Step 1: Click the Sign In link/button to open the modal
            console.print("[magenta]Jobright:[/magenta] Looking for Sign In button…")
            clicked = False
            for sign_in_sel in [
                'text="Sign In"',
                'text="SIGN IN"',
                '[href*="login"]',
                '[href*="signin"]',
            ]:
                try:
                    btn = await page.wait_for_selector(sign_in_sel, timeout=5000)
                    if btn:
                        await btn.click()
                        await self._delay(1.5, 2.5)
                        clicked = True
                        console.print(f"[magenta]Jobright:[/magenta] Clicked sign-in trigger.")
                        break
                except Exception:
                    continue

            if not clicked:
                console.print("[yellow]Jobright:[/yellow] No Sign In button found, trying direct login URL…")
                await page.goto("https://jobright.ai/login", wait_until="domcontentloaded", timeout=15000)
                await self._delay(2, 3)

            # Step 2: Wait for email input field (try each selector individually)
            console.print("[magenta]Jobright:[/magenta] Waiting for email input…")
            email_input = None
            for sel in [
                'input[type="email"]',
                'input[placeholder="Email"]',
                'input[placeholder*="email" i]',
                'input[name="email"]',
                'input[autocomplete="email"]',
            ]:
                try:
                    elem = await page.wait_for_selector(sel, timeout=5000)
                    if elem:
                        email_input = elem
                        console.print(f"[magenta]Jobright:[/magenta] Found email field.")
                        break
                except Exception:
                    continue

            if not email_input:
                console.print("[red]Jobright:[/red] Could not find email input field.")
                return False

            # Step 3: Fill email and password
            await email_input.click()
            await email_input.fill(email)
            await self._delay(0.5, 1)

            pwd_input = await page.wait_for_selector('input[type="password"]', timeout=5000)
            if not pwd_input:
                console.print("[red]Jobright:[/red] Could not find password input.")
                return False
            await pwd_input.click()
            await pwd_input.fill(password)
            await self._delay(0.5, 1)

            # Step 4: Submit
            console.print("[magenta]Jobright:[/magenta] Submitting credentials…")
            submitted = False
            for submit_sel in [
                'button[type="submit"]',
                'button:text("SIGN IN")',
                'button:text("Sign In")',
                'button:text("Log In")',
            ]:
                try:
                    btn = await page.wait_for_selector(submit_sel, timeout=3000)
                    if btn:
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                # Try pressing Enter on the password field
                await pwd_input.press("Enter")

            # Step 5: Wait for redirect to jobs page (up to 30s)
            console.print("[magenta]Jobright:[/magenta] Waiting for login to complete…")
            for _ in range(15):
                await asyncio.sleep(2)
                cur = page.url
                if "jobright.ai/jobs" in cur or "jobright.ai/dashboard" in cur or "jobright.ai/home" in cur:
                    console.print("[green]Jobright: ✓ Auto-login successful! Session saved.[/green]")
                    await self._save_session()
                    await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=20000)
                    await self._delay(2, 3)
                    return True
                console.print(f"[dim]  Waiting… {cur}[/dim]")

            console.print(f"[red]Jobright: Login redirect timed out. URL: {page.url}[/red]")
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
        Full autofill apply flow powered by the Jobright Autofill Chrome extension:

        1. Open the job page on Jobright
        2. Use Orion AI to tailor resume (Full Edit + all missing keywords)
        3. Click Apply Now → company ATS opens in new tab
        4. Extension auto-detects ATS fields and fills them with profile data
        5. Auto-login to company portal if credentials in .env
        6. Confirm with user, then submit
        """
        console.print(f"\n[magenta]Jobright Apply:[/magenta] {job.get('title')} @ {job.get('company')}")
        page = await self._start_browser(load_extensions=True)
        submitted = False

        try:
            # ── Step 1: Load job page ─────────────────────────────────────────
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)

            # Check page is still live (Jobright shows "no longer available")
            page_text = await page.evaluate("document.body.innerText")
            if "no longer available" in page_text.lower():
                console.print(f"[yellow]Jobright:[/yellow] Job {job.get('title')} is no longer listed — skipping.")
                return False

            # ── Step 2: Open Orion AI resume customization modal ──────────────
            console.print("[magenta]Jobright:[/magenta] Opening Orion AI resume modal…")
            await page.evaluate("""
            () => {
                const card = document.querySelector('[class*="tool-card"]');
                if (card) card.click();
            }
            """)
            await self._delay(2, 3)

            # ── Step 3: Improve My Resume for This Job ────────────────────────
            improve_clicked = False
            for sel in [
                'button:text-matches("Improve My Resume", "i")',
                'button:has-text("Improve My Resume")',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        await btn.click()
                        improve_clicked = True
                        break
                except Exception:
                    continue

            if improve_clicked:
                await self._delay(2, 3)

                # ── Step 4: Full Edit + all missing keywords ──────────────────
                await page.evaluate("""
                () => {
                    const labels = Array.from(document.querySelectorAll('label, span, p'));
                    const fullEdit = labels.find(l => l.textContent.trim().match(/^full edit/i));
                    if (fullEdit) fullEdit.click();
                    document.querySelectorAll('input[type="checkbox"]:not(:checked)')
                        .forEach(cb => cb.click());
                }
                """)
                await self._delay(1, 2)

                # ── Step 5: Generate tailored resume ─────────────────────────
                for sel in [
                    'button:text-matches("Generate My New Resume", "i")',
                    'button:has-text("Generate My New Resume")',
                ]:
                    try:
                        btn = await page.wait_for_selector(sel, timeout=5000)
                        if btn:
                            await btn.click()
                            console.print("[magenta]Jobright:[/magenta] Generating tailored resume… (up to 60s)")
                            break
                    except Exception:
                        continue

                # Wait for generation (Download Resume + Apply Now appear when done)
                for _ in range(30):
                    await asyncio.sleep(2)
                    btns = await page.evaluate(
                        "Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim())"
                    )
                    if "Download Resume" in btns and "Apply Now" in btns:
                        score_info = await page.evaluate("""
                        document.body.innerText.match(/score jumped from [\\d.]+ to [\\d.]+/i)?.[0] || 'generated'
                        """)
                        console.print(f"[green]Jobright:[/green] Resume ready — {score_info}")
                        break

                # Download the tailored PDF
                await page.evaluate("""
                Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.trim() === 'Download Resume')?.click()
                """)
                await self._delay(1, 2)

            # ── Step 6: Click Apply Now → company ATS opens in new tab ────────
            console.print("[magenta]Jobright:[/magenta] Clicking Apply Now — opening company portal…")
            try:
                async with self._context.expect_page(timeout=15000) as new_page_info:
                    await page.evaluate("""
                    Array.from(document.querySelectorAll('button'))
                        .find(b => b.textContent.trim() === 'Apply Now')?.click()
                    """)
                company_page = await new_page_info.value
                await company_page.wait_for_load_state("domcontentloaded", timeout=30000)
                await self._delay(4, 6)  # give extension time to inject
                console.print(f"[magenta]Jobright:[/magenta] Company portal: {company_page.url}")
            except Exception as e:
                console.print(f"[yellow]Jobright:[/yellow] No new tab opened ({e}). Trying APPLY WITH AUTOFILL…")
                # Fallback: dismiss modal and use the top-level Apply with Autofill button
                await page.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}))")
                await self._delay(1, 2)
                try:
                    async with self._context.expect_page(timeout=15000) as new_page_info:
                        btn = await page.wait_for_selector('button:text-matches("Apply with Autofill", "i")', timeout=5000)
                        if btn:
                            await btn.click()
                    company_page = await new_page_info.value
                    await company_page.wait_for_load_state("domcontentloaded", timeout=30000)
                    await self._delay(4, 6)
                except Exception:
                    console.print("[red]Jobright:[/red] Could not open company portal. Skipping.")
                    return False

            # Dismiss "Did you apply?" tracking dialog on Jobright (non-blocking)
            await page.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.includes("didn't apply"))?.click()
            """)

            # ── Step 7: Handle login on company portal if required ────────────
            if await self._portal_needs_login(company_page):
                console.print("[magenta]Jobright:[/magenta] Company portal requires login — attempting…")
                await self._company_portal_login(company_page)
                await self._delay(4, 6)

            # ── Step 8: Trigger Jobright extension autofill ───────────────────
            console.print("[magenta]Jobright:[/magenta] Looking for Jobright autofill button on ATS page…")
            autofill_triggered = await self._trigger_autofill(company_page)
            if autofill_triggered:
                console.print("[magenta]Jobright:[/magenta] Autofill triggered — waiting for fields to populate…")
                await self._delay(6, 10)
            else:
                console.print("[yellow]Jobright:[/yellow] Autofill button not detected — extension may auto-fill, or fields need manual entry")
                await self._delay(4, 6)

            # ── Step 9: Confirm and submit ────────────────────────────────────
            submitted = await self._confirm_and_submit(company_page, job)

        except Exception as exc:
            console.print(f"[red]Jobright apply error:[/red] {exc}")
        finally:
            await self._close_browser()

        return submitted

    async def _portal_needs_login(self, page) -> bool:
        """Return True if the company ATS page is showing a login wall."""
        url = page.url.lower()
        if any(w in url for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso.", "myworkday"]):
            return True
        try:
            return await page.evaluate("""
            () => {
                const emailInput = document.querySelector(
                    'input[type="email"], input[name*="email" i], input[placeholder*="email" i]'
                );
                const passInput = document.querySelector('input[type="password"]');
                return !!(emailInput || passInput);
            }
            """)
        except Exception:
            return False

    async def _company_portal_login(self, page) -> None:
        """Log into a company ATS portal using COMPANY_EMAIL / COMPANY_PASSWORD from .env."""
        email = os.environ.get("COMPANY_EMAIL", "")
        password = os.environ.get("COMPANY_PASSWORD", "")
        if not email or not password:
            console.print("[yellow]Jobright:[/yellow] Set COMPANY_EMAIL + COMPANY_PASSWORD in .env for auto-login")
            return

        try:
            # Fill email field
            for sel in [
                'input[type="email"]',
                'input[name*="email" i]',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]',
                'input[autocomplete="username"]',
            ]:
                try:
                    elem = await page.wait_for_selector(sel, timeout=3000)
                    if elem:
                        await elem.fill(email)
                        await self._delay(0.5, 1)
                        break
                except Exception:
                    continue

            # Some portals (Workday) split email → Next → password
            for sel in [
                'button[type="submit"]',
                'button:text("Next")',
                'button:text("Continue")',
                'button:text("Sign In")',
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await self._delay(2, 3)
                        break
                except Exception:
                    continue

            # Fill password (may now be on next page)
            try:
                pwd = await page.wait_for_selector('input[type="password"]', timeout=5000)
                if pwd:
                    await pwd.fill(password)
                    await self._delay(0.5, 1)
            except Exception:
                pass

            # Final submit
            for sel in [
                'button[type="submit"]',
                'button:text("Sign In")',
                'button:text("Log In")',
                'button:text("Login")',
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await self._delay(4, 6)
                        break
                except Exception:
                    continue

            console.print(f"[green]Jobright:[/green] Login attempted → {page.url}")

        except Exception as e:
            console.print(f"[yellow]Jobright:[/yellow] Portal login attempt failed: {e}")

    async def _trigger_autofill(self, page) -> bool:
        """
        Find and click the Jobright extension's autofill button injected into the ATS page.
        The extension (built with Plasmo) injects a content UI into the page DOM.
        """
        selectors = [
            # Plasmo extension shadow host element
            "plasmo-csui",
            # Elements with jobright in class/id/data attributes
            '[class*="jobright"]',
            '[id*="jobright"]',
            '[data-jobright]',
            # Button text the extension might inject
            'button:text-matches("autofill", "i")',
            'button:text-matches("fill with jobright", "i")',
            '[aria-label*="jobright" i]',
            # Extension popup trigger
            '[class*="autofill-btn"]',
            '[class*="jb-"]',
        ]

        for sel in selectors:
            try:
                elem = await page.wait_for_selector(sel, timeout=3000)
                if elem:
                    await elem.click()
                    console.print(f"[green]Jobright:[/green] Extension autofill triggered via '{sel}'")
                    return True
            except Exception:
                continue

        # Extension may use shadow DOM — try piercing it
        try:
            found = await page.evaluate("""
            () => {
                // Walk shadow roots looking for jobright autofill button
                const walk = (root) => {
                    for (const el of root.querySelectorAll('*')) {
                        const text = (el.textContent || '').toLowerCase();
                        if (text.includes('autofill') || text.includes('jobright')) {
                            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') {
                                el.click();
                                return true;
                            }
                        }
                        if (el.shadowRoot && walk(el.shadowRoot)) return true;
                    }
                    return false;
                };
                return walk(document);
            }
            """)
            if found:
                console.print("[green]Jobright:[/green] Extension autofill triggered via shadow DOM")
                return True
        except Exception:
            pass

        return False

    async def _confirm_and_submit(self, page, job: dict) -> bool:
        """
        Find the final Submit / Apply button on the ATS page, show a preview,
        ask for confirmation, then click.
        """
        submit_selectors = [
            'button:text-matches("^Submit$", "i")',
            'button:text-matches("Submit Application", "i")',
            'button:text-matches("^Apply$", "i")',
            'button:text-matches("Send Application", "i")',
            'button:text-matches("Complete Application", "i")',
            '[data-automation-id*="submit" i]',
            '[aria-label*="submit" i]',
            '[type="submit"]',
        ]

        submit_btn = None
        for sel in submit_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000)
                if btn:
                    submit_btn = btn
                    break
            except Exception:
                continue

        console.print(f"\n[bold yellow]─── READY TO SUBMIT ───[/bold yellow]")
        console.print(f"  Job   : {job.get('title')} @ {job.get('company')}")
        console.print(f"  Portal: {page.url}")
        if submit_btn:
            console.print(f"  Submit button found ✓")
        else:
            console.print(f"  [yellow]Submit button not found — may need multi-step navigation[/yellow]")

        confirm = input("\n  Submit this application? [y/N] > ").strip().lower()
        if confirm != "y":
            console.print("[yellow]Jobright: Submission cancelled.[/yellow]")
            return False

        if submit_btn:
            await submit_btn.click()
            await self._delay(3, 5)
            console.print("[green]✓ Application submitted![/green]")
            return True
        else:
            console.print("[yellow]Please complete the submission manually in the browser window.[/yellow]")
            input("  Press Enter when submitted (or to skip) > ")
            answer = input("  Did you successfully submit? [y/N] > ").strip().lower()
            return answer == "y"


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
