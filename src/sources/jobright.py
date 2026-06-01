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
import sys
from datetime import datetime

from rich.console import Console

from .base import BaseScraper, JobExpiredError
from src.notifier import notify_error, notify_warning, notify_success, notify_info

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
                email = os.environ.get("JOBRIGHT_EMAIL", "")
                password = os.environ.get("JOBRIGHT_PASSWORD", "")
                if email and password:
                    console.print("[magenta]Jobright:[/magenta] Not logged in — attempting auto-login…")
                    logged_in = await self._auto_login(page, email, password)
                    if not logged_in:
                        if not (sys.stdin and sys.stdin.isatty()):
                            console.print("[red]Jobright: Auto-login failed and running non-interactively. Skipping Jobright scrape.[/red]")
                            return []
                        console.print(
                            "\n[yellow]Jobright:[/yellow] Auto-login failed.\n"
                            "  → Please log in manually in the browser window.\n"
                            "  → Press Enter once you are logged in."
                        )
                        input("  Press Enter once logged in > ")
                        await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=30000)
                        await self._delay(2, 3)
                        await self._save_session()
                else:
                    if not (sys.stdin and sys.stdin.isatty()):
                        console.print("[red]Jobright: Not logged in, no credentials in .env, and running non-interactively. Skipping Jobright scrape.[/red]")
                        return []
                    console.print(
                        "\n[yellow]Jobright:[/yellow] Not logged in.\n"
                        "  → Please log in to jobright.ai in the browser window that just opened.\n"
                        "  → Once you're on the jobs page, come back here and press Enter."
                    )
                    input("  Press Enter once logged in > ")
                    await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=30000)
                    await self._delay(2, 3)
                    await self._save_session()
                    console.print("[green]Jobright: Session saved — future runs will log in automatically.[/green]")

            console.print(f"[magenta]Jobright:[/magenta] Page loaded: {page.url}")

            if "jobright.ai/jobs" not in page.url:
                console.print("[red]Jobright: Could not navigate to jobs page — skipping.[/red]")
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

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        """
        Full autofill apply flow powered by the Jobright Autofill Chrome extension:

        1. Open the job page on Jobright
        2. Use Orion AI to tailor resume (Full Edit + all missing keywords)
        3. Extract the company ATS URL directly from the Jobright page DOM
        4. Open ATS URL in a new Playwright page (avoids popup-blocking issues)
        5. Extension auto-fills form fields; auto-login to portal if needed
        6. Confirm with user, then submit
        """
        self.auto_submit = auto_submit
        console.print(f"\n[magenta]Jobright Apply:[/magenta] {job.get('title')} @ {job.get('company')}")
        page = await self._start_browser(load_extensions=True)
        submitted = False

        try:
            # ── Step 1: Load job page ─────────────────────────────────────────
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(3, 4)

            # Check page is still live
            page_text = await page.evaluate("document.body.innerText")
            if "no longer available" in page_text.lower():
                console.print(f"[yellow]Jobright:[/yellow] Job no longer listed — skipping.")
                raise JobExpiredError("Job no longer available")


            # ── Step 2: Extract external ATS URL before opening modal ─────────
            ext_url = await self._extract_external_url(page)
            console.print(f"[magenta]Jobright:[/magenta] ATS URL: {ext_url or '(will try Apply Now)'}")

            # ── Step 3: Open Orion AI "Customize Your Resume" tool ───────────
            # The tool card is active by default (class contains "tool-card-active").
            # Clicking it opens the resume customization modal.
            console.print("[magenta]Jobright:[/magenta] Opening Orion AI resume modal…")
            await page.evaluate("""
            () => {
                // Prefer the active card; fall back to the first tool card
                const card = document.querySelector('[class*="tool-card-active"]')
                           || document.querySelector('[class*="tool-card"]');
                if (card) card.click();
            }
            """)
            await self._delay(2, 3)

            # ── Step 4: Look for any resume-improvement trigger button ─────────
            # The button text changed from "Improve My Resume for This Job"
            # to "Customize Your Resume" (or similar) in the updated Jobright UI.
            improve_btn = None
            console.print("[magenta]Jobright:[/magenta] Waiting for resume tool to load (up to 20s)…")
            improve_patterns = [
                'button:text-matches("Improve My Resume", "i")',       # legacy
                'button:text-matches("Customize.*Resume", "i")',       # current
                'button:text-matches("Tailor.*Resume", "i")',
                'button:text-matches("Optimize.*Resume", "i")',
                'button:text-matches("Enhance.*Resume", "i")',
            ]
            for _ in range(20):
                await asyncio.sleep(1)
                for pat in improve_patterns:
                    try:
                        btn = await page.query_selector(pat)
                        if btn:
                            improve_btn = btn
                            break
                    except Exception:
                        pass
                if improve_btn:
                    break

            if improve_btn:
                btn_text = await improve_btn.inner_text()
                console.print(f"[green]Jobright:[/green] Found resume button: '{btn_text.strip()}'")
                await improve_btn.click()
                await self._delay(2, 3)

                # Select Full Edit mode and check all missing keywords
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
                gen_patterns = [
                    'button:text-matches("Generate My New Resume", "i")',
                    'button:text-matches("Generate.*Resume", "i")',
                    'button:text-matches("^Generate$", "i")',
                    'button:text-matches("Create.*Resume", "i")',
                ]
                gen_btn = None
                for pat in gen_patterns:
                    try:
                        gen_btn = await page.query_selector(pat)
                        if gen_btn:
                            break
                    except Exception:
                        pass

                if gen_btn:
                    await gen_btn.click()
                    console.print("[magenta]Jobright:[/magenta] Generating tailored resume… (~60s)")

                    # Poll until Download Resume appears (max 90s)
                    for _ in range(45):
                        await asyncio.sleep(2)
                        btns = await page.evaluate(
                            "Array.from(document.querySelectorAll('button')).map(b=>b.textContent.trim())"
                        )
                        if any("Download" in b for b in btns):
                            score_info = await page.evaluate("""
                            document.body.innerText
                                .match(/score jumped from [\\d.]+ to [\\d.]+/i)?.[0] || 'generated'
                            """)
                            console.print(f"[green]Jobright:[/green] Resume ready — {score_info}")
                            break

                    # Download tailored PDF
                    await page.evaluate("""
                    Array.from(document.querySelectorAll('button'))
                        .find(b => /download/i.test(b.textContent))?.click()
                    """)
                    await self._delay(1, 2)
                else:
                    console.print("[yellow]Jobright:[/yellow] Generate button not found — skipping generation")
            else:
                console.print("[yellow]Jobright:[/yellow] Resume tool button not found — using existing resume")

            # ── Step 6: Dismiss modal, open company ATS directly ──────────────
            # Close the resume modal
            await page.evaluate(
                "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}))"
            )
            await self._delay(1, 2)

            # Navigate to the ATS URL in a new page within our context
            if not ext_url:
                # Last resort: try to read it from the page after modal close
                ext_url = await self._extract_external_url(page)

            if not ext_url:
                console.print("[red]Jobright:[/red] Could not find company ATS URL — skipping.")
                return False

            console.print(f"[magenta]Jobright:[/magenta] Opening company portal: {ext_url[:80]}")
            company_page = await self._context.new_page()
            await company_page.goto(ext_url, wait_until="domcontentloaded", timeout=45000)
            await self._delay(3, 5)

            # Dismiss "Did you apply?" on Jobright (non-blocking)
            try:
                await page.evaluate("""
                Array.from(document.querySelectorAll('button'))
                    .find(b=>b.textContent.includes("didn't apply"))?.click()
                """)
            except Exception:
                pass

            console.print(f"[magenta]Jobright:[/magenta] Company portal loaded: {company_page.url}")

            # ── Step 7: Click Apply button FIRST ─────────────────────────────
            # Workday shows a job description page; clicking Apply opens the
            # sign-in dialog or application form.  Login comes AFTER this.
            await self._click_ats_apply_button(company_page)
            await self._delay(3, 5)

            # ── Step 7.5: Handle login if required (non-Workday ATS only) ──────
            # Workday login/wizard is handled interactively inside
            # _click_ats_apply_button, so skip the auto-login step for it.
            try:
                portal_url = company_page.url
            except Exception:
                portal_url = ""
            if 'myworkdayjobs.com' not in portal_url:
                if await self._portal_needs_login(company_page):
                    console.print("[magenta]Jobright:[/magenta] Login required — auto-logging in…")
                    await self._company_portal_login(company_page)
                    await self._delay(4, 6)

            # ── Step 8: Trigger Jobright extension autofill ───────────────────
            # For Workday: the user already clicked "Autofill with Resume" and
            # navigated through the wizard manually — the extension filled fields
            # during that process.  Skip the programmatic trigger for Workday to
            # avoid re-triggering and corrupting filled fields.
            try:
                current_portal = company_page.url
            except Exception:
                current_portal = ""

            if 'myworkdayjobs.com' not in current_portal:
                console.print("[magenta]Jobright:[/magenta] Triggering Jobright autofill extension…")
                autofill_triggered = await self._trigger_autofill(company_page)
                if autofill_triggered:
                    console.print("[magenta]Jobright:[/magenta] Autofill triggered — waiting for fields…")
                    await self._delay(10, 15)
                else:
                    console.print("[yellow]Jobright:[/yellow] Autofill button not found — extension may auto-fill on load")
                    await self._delay(5, 8)
            else:
                console.print("[dim]Jobright: Workday — autofill was handled manually, skipping trigger[/dim]")

            # ── Step 9: Confirm and submit ────────────────────────────────────
            submitted = await self._confirm_and_submit(company_page, job, auto_submit=auto_submit)
            if submitted:
                notify_success(
                    f"Applied: {job.get('title')} @ {job.get('company')}",
                    f"Application submitted successfully"
                )

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            console.print(f"[red]Jobright apply error:[/red] {exc}")
            console.print(f"[dim]{tb}[/dim]")
            notify_error(
                f"Apply failed: {job.get('title')} @ {job.get('company')}",
                str(exc)[:200]
            )
        finally:
            await self._close_browser()

        return submitted

    async def _extract_external_url(self, page) -> str:
        """
        Extract the company ATS application URL directly from the Jobright DOM.
        Tries Next.js page data first, then scans anchor tags for known ATS hostnames.
        """
        return await page.evaluate("""
        () => {
            // 1. Try Next.js __NEXT_DATA__ (most reliable)
            try {
                const nd = JSON.parse(document.getElementById('__NEXT_DATA__')?.textContent || '{}');
                const pageProps = nd?.props?.pageProps || {};
                const job = pageProps.job || pageProps.jobDetail || pageProps.jobInfo || {};
                const url = job.externalApplyLink || job.applyUrl || job.apply_url
                          || job.externalUrl || job.applicationUrl;
                if (url && !url.includes('jobright.ai')) return url;
            } catch(e) {}

            // 2. Scan all <a> tags for known ATS hostnames
            const ATS = [
                'myworkdayjobs.com', 'wd1.myworkday', 'wd3.myworkday', 'wd5.myworkday',
                'greenhouse.io', 'lever.co', 'taleo.net', 'icims.com',
                'smartrecruiters.com', 'bamboohr.com', 'ashbyhq.com',
                'workable.com', 'brassring.com', 'successfactors.com',
                'recruitingbypaycor.com', 'paylocity.com', 'ultipro.com',
                'myworkday.com', 'jobs.lever.co', 'apply.workable.com',
                'careers.', '/careers/', '/jobs/', 'recruit.',
            ];
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.href || '';
                if (h.includes('jobright.ai')) continue;
                if (ATS.some(p => h.includes(p))) return h;
            }

            // 3. "Original Job Post" button/link
            const orig = Array.from(document.querySelectorAll('a, button'))
                .find(el => /original job post/i.test(el.textContent));
            if (orig?.href) return orig.href;

            return '';
        }
        """)

    async def _portal_needs_login(self, page) -> bool:
        """Return True if the company ATS page is showing a login wall."""
        try:
            url = page.url.lower()
        except Exception:
            return False  # page closed or invalid
        if any(w in url for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."]):
            return True
        try:
            # Workday application forms have email fields (contact info) but NOT
            # password fields.  Only treat as login wall if a password input exists.
            # For all other ATS, presence of email OR password indicates a login form.
            if 'myworkdayjobs.com' in url:
                return await page.evaluate("""
                () => !!document.querySelector('input[type="password"]')
                """)
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
        """
        Log into a company ATS portal using COMPANY_EMAIL / COMPANY_PASSWORD from .env.
        Uses page.type() (real keystroke simulation) instead of fill() to avoid
        bot-detection triggers on Workday and similar ATS portals.
        """
        email = os.environ.get("COMPANY_EMAIL", "")
        password = os.environ.get("COMPANY_PASSWORD", "")
        if not email or not password:
            console.print("[yellow]Jobright:[/yellow] Set COMPANY_EMAIL + COMPANY_PASSWORD in .env for auto-login")
            notify_error(
                "Missing credentials",
                "Add COMPANY_EMAIL and COMPANY_PASSWORD to .env to enable ATS auto-login"
            )
            return

        try:
            # ── Step 1: Find and fill email with human-like typing ────────────
            email_filled = False
            for sel in [
                'input[type="email"]',
                'input[name*="email" i]',
                'input[placeholder*="email" i]',
                'input[autocomplete="email"]',
                'input[autocomplete="username"]',
            ]:
                try:
                    elem = await page.wait_for_selector(sel, timeout=4000)
                    if elem:
                        await elem.click()
                        await self._delay(0.3, 0.6)
                        await elem.type(email, delay=80)   # type() = real keystrokes
                        await self._delay(0.5, 1)
                        email_filled = True
                        break
                except Exception:
                    continue

            if not email_filled:
                console.print("[yellow]Jobright:[/yellow] Email field not found — sign-in skipped")
                return

            # ── Step 2: Submit email (Workday splits email → Next → password) ─
            for sel in [
                'button:text-matches("^Next$", "i")',
                'button:text-matches("^Continue$", "i")',
                'button[type="submit"]',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(2, 3)
                        break
                except Exception:
                    continue

            # ── Step 3: Fill password ─────────────────────────────────────────
            try:
                pwd = await page.wait_for_selector('input[type="password"]', timeout=8000)
                if pwd:
                    await pwd.click()
                    await self._delay(0.3, 0.6)
                    await pwd.type(password, delay=80)     # type() = real keystrokes
                    await self._delay(0.5, 1)
            except Exception:
                console.print("[yellow]Jobright:[/yellow] Password field not found — may be SSO-only")
                return

            # ── Step 4: Submit ────────────────────────────────────────────────
            for sel in [
                'button:text-matches("^Sign In$", "i")',
                'button:text-matches("^Log In$", "i")',
                'button:text-matches("^Login$", "i")',
                'button[type="submit"]',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(5, 8)  # wait for redirect
                        break
                except Exception:
                    continue

            console.print(f"[green]Jobright:[/green] Login attempted → {page.url}")

        except Exception as e:
            console.print(f"[yellow]Jobright:[/yellow] Portal login attempt failed: {e}")

    async def _click_ats_apply_button(self, page) -> bool:
        """
        Navigate to / click into the application form on an ATS job listing page.

        Strategy:
        • Workday (myworkdayjobs.com) — append /apply to the job URL to enter the
          wizard directly; this is more reliable than clicking a button that may
          mis-match navigation links.
        • Greenhouse / Lever / others — click the visible Apply/Apply Now button.

        Returns True if we successfully entered the form (or are already on it).
        """
        current_url = page.url

        # ── Workday: navigate directly to autofillWithResume endpoint ───────
        # This skips the "Start Your Application" chooser entirely.
        # If the Workday session is active → opens the application form.
        # If session expired → Workday shows its sign-in page; we handle that below.
        if 'myworkdayjobs.com' in current_url and '/apply' not in current_url:
            apply_url = current_url.rstrip('/') + '/apply/autofillWithResume'
            console.print(f"[magenta]Jobright:[/magenta] Workday — navigating to autofillWithResume…")
            try:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
                await self._delay(4, 6)
                after = page.url
                console.print(f"[magenta]Jobright:[/magenta] Landed on: {after[:90]}")
            except Exception as e:
                console.print(f"[yellow]Jobright:[/yellow] Workday nav failed ({e})")

        # ── Workday "Start Your Application" chooser ─────────────────────────
        # After landing on /apply, Workday shows:
        #   "Autofill with Resume" | "Apply Manually" | "Use My Last Application"
        # We use a JS-dispatched click (not Playwright .click()) to avoid the
        # Chromium crash that Playwright's synthetic mouse events triggered.
        # Priority: Use My Last Application > Autofill with Resume > Apply Manually
        try:
            current = page.url
        except Exception:
            current = ""
        # After navigating to autofillWithResume: handle sign-in if needed,
        # then auto-navigate the wizard steps.
        if 'myworkdayjobs.com' in current:
            await self._workday_handle_post_chooser(page)
            return True

        # ── Other ATS: click the Apply / Apply Now button ────────────────────
        apply_selectors = [
            # Workday fallback (data-automation-id only, not broad text matches)
            '[data-automation-id="jobPostingApplyButton"]',
            # Greenhouse / Lever
            '#apply_button',
            'a.btn-apply',
            '.apply-button',
            # Text-match on button elements only (not anchors, to avoid nav links)
            'button:text-matches("^Apply Now$", "i")',
            'button:text-matches("^Apply$", "i")',
            'button:text-matches("Apply for This Job", "i")',
            'button:text-matches("Apply for Job", "i")',
        ]

        for sel in apply_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=4000)
                if btn:
                    await btn.click()
                    from rich.markup import escape
                    console.print(f"[magenta]Jobright:[/magenta] Clicked Apply button → waiting for form…")
                    await self._delay(4, 6)
                    return True
            except Exception:
                continue

        console.print("[dim]Jobright: No Apply button found — may already be on the application form[/dim]")
        return False

    async def _workday_handle_post_chooser(self, page) -> None:
        """
        Called right after clicking the Workday 'Start Your Application' chooser.
        Handles two possible outcomes:
          A) Sign-in page appeared → pause for ONE-TIME manual sign-in (session is
             then saved to the persistent profile so future runs skip this step)
          B) Application form appeared → let the Jobright extension fill it, then
             auto-click 'Next' through each wizard step until the Review page.
        """
        await self._delay(3, 5)

        # ── A: Sign-in wall ───────────────────────────────────────────────────
        # Workday's sign-in page uses SSO/OAuth that crashes Chromium when
        # automated.  We pause and let the user sign in once; the persistent
        # profile saves the session so every future run is automatic.
        needs_login = False
        try:
            url = page.url.lower()
            if any(w in url for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."]):
                needs_login = True
        except Exception:
            pass

        if not needs_login:
            try:
                pwd = await page.query_selector('input[type="password"]')
                if pwd:
                    needs_login = True
            except Exception:
                pass

        if needs_login:
            # Use credentials from .env (COMPANY_EMAIL / COMPANY_PASSWORD).
            # page.type() simulates real keystrokes to avoid Workday bot detection.
            console.print("[magenta]Jobright:[/magenta] Workday sign-in — using credentials from .env…")
            await self._company_portal_login(page)
            await self._delay(4, 6)

            # Verify we left the sign-in page
            try:
                new_url = page.url.lower()
                still_login = any(w in new_url for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."])
                if still_login:
                    pwd_check = await page.query_selector('input[type="password"]')
                    still_login = bool(pwd_check)
            except Exception:
                still_login = False

            if still_login:
                console.print("[red]Jobright: Workday sign-in did not complete — skipping this job.[/red]")
                notify_error(
                    "Workday login failed",
                    "Check COMPANY_EMAIL / COMPANY_PASSWORD in .env — credentials may be wrong or portal uses SSO"
                )
                return

        # ── B: Auto-navigate Workday wizard ──────────────────────────────────
        # Wait for the application form to fully load after login/redirect.
        console.print("[magenta]Jobright:[/magenta] Waiting for Workday application form to load…")
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await self._delay(5, 7)  # extra buffer for JS rendering

        # The extension fills fields on each step; we click 'Next' until we
        # reach the Review/Submit page (where the button text becomes 'Submit').
        console.print("[magenta]Jobright:[/magenta] Navigating Workday wizard steps…")
        for step_num in range(30):   # max 30 steps — GDIT/CVS/Citi have 15-20+ pages
            await self._delay(4, 6)  # wait for extension to fill the current step

            # Early exit: URL contains 'review' or 'confirm' — we're at the final page
            try:
                cur_url = page.url.lower()
                if any(w in cur_url for w in ["/review", "/confirm", "/submit"]):
                    console.print(f"[green]Jobright:[/green] Reached review/submit URL — ready to submit")
                    break
            except Exception:
                pass

            # Get the Next/Submit button (Workday uses data-automation-id)
            next_btn = None
            for sel in [
                '[data-automation-id="bottom-navigation-next-button"]',
                'button:text-matches("^Next$", "i")',
                'button:text-matches("^Save and Continue$", "i")',
                'button:text-matches("^Continue$", "i")',
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn and await btn.is_visible():
                        next_btn = btn
                        break
                except Exception:
                    continue

            if not next_btn:
                console.print(f"[dim]Jobright: No Next button on step {step_num+1} — may be on final page[/dim]")
                break

            btn_text = ""
            try:
                btn_text = (await next_btn.inner_text()).strip()
            except Exception:
                pass

            # Stop auto-clicking when we reach Review/Submit
            if any(w in btn_text.lower() for w in ["submit", "review", "confirm"]):
                console.print(f"[green]Jobright:[/green] Reached final step: '{btn_text}' — ready to submit")
                break

            console.print(f"[dim]Jobright: Step {step_num+1} → clicking '{btn_text or 'Next'}'[/dim]")
            try:
                await next_btn.click()
            except Exception as e:
                console.print(f"[yellow]Jobright:[/yellow] Next click failed: {e}")
                break

        console.print("[magenta]Jobright:[/magenta] Workday wizard navigation complete")

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
                    console.print(f"[green]Jobright:[/green] Extension autofill triggered")
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

    async def _confirm_and_submit(self, page, job: dict, auto_submit: bool = False) -> bool:
        """
        Find the final Submit / Apply button on the ATS page, show a preview,
        optionally ask for confirmation, then click.
        """
        submit_selectors = [
            # Workday final-step submit (data-automation-id)
            '[data-automation-id="bottom-navigation-next-button"]',
            '[data-automation-id*="submit" i]',
            # Generic text-based
            'button:text-matches("^Submit$", "i")',
            'button:text-matches("Submit Application", "i")',
            'button:text-matches("^Apply$", "i")',
            'button:text-matches("Send Application", "i")',
            'button:text-matches("Complete Application", "i")',
            '[aria-label*="submit" i]',
        ]

        submit_btn = None
        for sel in submit_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=4000)
                if btn and await btn.is_visible():
                    submit_btn = btn
                    break
            except Exception:
                continue

        try:
            portal_url = page.url
        except Exception:
            portal_url = "(unknown)"

        console.print(f"\n[bold yellow]─── READY TO SUBMIT ───[/bold yellow]")
        console.print(f"  Job   : {job.get('title')} @ {job.get('company')}")
        console.print(f"  Portal: {portal_url}")
        if submit_btn:
            console.print(f"  [green]Submit button found ✓[/green]")
        else:
            console.print(f"  [yellow]Submit button not found — navigate to the final step in the browser[/yellow]")

        if auto_submit:
            console.print("[green]Jobright: Auto-submitting application (auto-submit active)![/green]")
            confirm = "y"
        else:
            try:
                confirm = input("\n  Submit this application? [y/N] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
        if confirm != "y":
            console.print("[yellow]Jobright: Submission cancelled.[/yellow]")
            return False

        if submit_btn:
            await submit_btn.click()
            await self._delay(3, 5)
            console.print("[green]✓ Application submitted![/green]")
            return True
        else:
            # No submit button found — user must click it manually in the browser
            if auto_submit:
                console.print("[red]Jobright: Submit button not found — cannot auto-submit. Skipping.[/red]")
                return False
            console.print("[yellow]Click Submit in the browser window, then confirm below.[/yellow]")
            try:
                input("  Press Enter after submitting (or to skip) > ")
                answer = input("  Did you successfully submit? [y/N] > ").strip().lower()
                return answer == "y"
            except (EOFError, KeyboardInterrupt):
                return False


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
