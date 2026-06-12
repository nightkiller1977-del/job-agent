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
from pathlib import Path

from rich.console import Console

from .base import BaseScraper, JobExpiredError
from src.notifier import notify_error, notify_warning, notify_success, notify_info
from src.resume_helper import ResumeFieldFixer, resolve_resume_path

console = Console()

JOBRIGHT_BASE = "https://jobright.ai"
JOBRIGHT_JOBS_URL = "https://jobright.ai/jobs"
JOBRIGHT_MATCHED_URL = "https://jobright.ai/jobs/recommend"
TAILORED_RESUMES_DIR = Path(__file__).parent.parent.parent / "state" / "tailored_resumes"
TAILORED_RESUMES_DIR.mkdir(parents=True, exist_ok=True)


class JobrightScraper(BaseScraper):
    name = "jobright"

    # Class-level flag: once Orion tailoring fails in a session, skip it for
    # all subsequent jobs instead of waiting 2-4 minutes per job on timeouts.
    _orion_tailoring_available: bool = True

    def _set_apply_outcome(self, status: str, detail: str) -> bool:
        self.last_apply_status = status
        self.last_apply_detail = detail
        return False

    async def tailor_resume_for_external_job(self, job: dict) -> str:
        """Use Jobright/Orion to tailor a resume for a non-Jobright job.

        The efficient path is to search Jobright by title/company, open the best
        matching Jobright card, run the existing Orion resume tool, and download
        the generated PDF. If no credible Jobright match is found, return an
        empty string so the source-specific apply flow can continue safely.
        """
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        query = " ".join(part for part in [title, company] if part).strip()
        if not query:
            console.print("[yellow]Jobright Tailor:[/yellow] Missing job title/company; cannot search Jobright.")
            return ""

        console.print(f"[magenta]Jobright Tailor:[/magenta] Searching Jobright for '{query}'")
        page = await self._start_browser(load_extensions=True)
        try:
            match = await self._find_external_jobright_match(page, job)
            if not match and job.get("url"):
                match = await self._add_external_job_to_jobright(page, job)
            if not match:
                await page.goto(JOBRIGHT_MATCHED_URL, wait_until="domcontentloaded", timeout=30000)
                await self._delay(2, 3)
                await self._dismiss_jobright_popups(page)

                if "/login" in page.url or "/signin" in page.url or "auth" in page.url:
                    email = os.environ.get("JOBRIGHT_EMAIL", "")
                    password = os.environ.get("JOBRIGHT_PASSWORD", "")
                    if email and password and not await self._auto_login(page, email, password):
                        console.print("[yellow]Jobright Tailor:[/yellow] Could not log in to Jobright.")
                        return ""

                search = await page.query_selector('input[type="search"], input[placeholder*="Search" i]')
                if search:
                    await search.click()
                    await search.fill(query)
                    await search.press("Enter")
                    await self._delay(4, 6)

                jobs = await self._js_extract(page)
                match = self._best_jobright_match(job, jobs)
            if not match:
                self._jobright_available = False
                self._jobright_url = ""
                console.print("[yellow]Jobright Tailor:[/yellow] No credible Jobright match found; using existing resume.")
                return ""

            self._jobright_available = True
            self._jobright_url = match.get("url", "")
            console.print(
                f"[magenta]Jobright Tailor:[/magenta] Matched '{match.get('title')}' @ {match.get('company')}"
            )
            if "external_" in (match.get("url") or ""):
                resume_path = await self._download_custom_resume_from_external_list(page, match, job)
                if resume_path:
                    return resume_path

            await page.goto(match["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(3, 4)
            await self._dismiss_jobright_popups(page)
            return await self._generate_tailored_resume(page, job)
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] Tailoring failed: {exc}")
            return ""
        finally:
            await self._close_browser()

    async def _find_external_jobright_match(self, page, job: dict) -> dict | None:
        await page.goto("https://jobright.ai/jobs/external", wait_until="domcontentloaded", timeout=30000)
        await self._delay(2, 3)
        await self._dismiss_jobright_popups(page)
        jobs = await self._js_extract(page)
        match = self._best_jobright_match(job, jobs)
        if match:
            console.print("[magenta]Jobright Tailor:[/magenta] Found matching external Jobright job.")
        return match

    async def _add_external_job_to_jobright(self, page, job: dict) -> dict | None:
        url = (job.get("url") or "").strip()
        if not url:
            return None
        console.print("[magenta]Jobright Tailor:[/magenta] Adding external job URL to Jobright.")
        try:
            await page.goto("https://jobright.ai/jobs/external", wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            await self._dismiss_jobright_popups(page)

            if "/login" in page.url or "/signin" in page.url or "auth" in page.url:
                email = os.environ.get("JOBRIGHT_EMAIL", "")
                password = os.environ.get("JOBRIGHT_PASSWORD", "")
                if email and password and not await self._auto_login(page, email, password):
                    console.print("[yellow]Jobright Tailor:[/yellow] Could not log in to Jobright.")
                    return None

            input_box = await page.query_selector(
                'input[placeholder*="job URL" i], input[placeholder*="Paste" i], input[type="text"]'
            )
            if not input_box:
                await self._click_first_button_text(page, [r"^Add Job$"])
                await self._delay(1, 2)
                input_box = await page.query_selector(
                    'input[placeholder*="job URL" i], input[placeholder*="Paste" i], input[type="text"]'
                )
            if not input_box:
                console.print("[yellow]Jobright Tailor:[/yellow] Add Job URL input not found.")
                return None

            await input_box.click()
            await input_box.fill(url)
            clicked = await self._click_first_button_text(page, [r"^Add Job$", r"^Import$", r"^Submit$"])
            if not clicked:
                await input_box.press("Enter")
            await self._delay(12, 18)

            for attempt in range(18):
                jobs = await self._js_extract(page)
                match = self._best_jobright_match(job, jobs)
                if match:
                    console.print("[green]Jobright Tailor:[/green] External job added/found in Jobright.")
                    return match
                # After ~44 seconds total, refresh the page — Jobright sometimes
                # needs a reload before the newly-added external card appears.
                if attempt == 8:
                    console.print("[dim]Jobright Tailor: reloading page to surface new card…[/dim]")
                    await page.reload(wait_until="domcontentloaded")
                    await self._delay(3, 4)
                await asyncio.sleep(4)

            console.print("[yellow]Jobright Tailor:[/yellow] External job was submitted to Jobright, but no matching card appeared yet.")
            return None
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] Add external job failed: {exc}")
            return None

    async def _download_custom_resume_from_external_list(self, page, match: dict, job: dict) -> str:
        """Open the matching external card's CUSTOM RESUME drawer and download the PDF."""
        url = match.get("url") or ""
        if not url:
            return ""
        try:
            if "/jobs/external" not in page.url:
                await page.goto("https://jobright.ai/jobs/external", wait_until="domcontentloaded", timeout=30000)
                await self._delay(2, 3)
            clicked = await page.evaluate(
                """
                (url) => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    const anchor = anchors.find(a => a.href === url || a.href.endsWith(new URL(url).pathname));
                    const card = anchor?.closest('[class*="job-card"], [class*="card"], li, article, section, div');
                    if (!card) return false;
                    const button = Array.from(card.querySelectorAll('button,a,[role="button"]'))
                        .find(el => /custom\\s+resume/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
                    if (!button) return false;
                    button.scrollIntoView({block: 'center', inline: 'center'});
                    button.click();
                    return true;
                }
                """,
                url,
            )
            if not clicked:
                return ""
            console.print("[magenta]Jobright Tailor:[/magenta] Opened external custom resume drawer.")
            await self._delay(4, 6)
            return await self._download_or_regenerate_custom_resume(page, job, self._latest_tailored_resume())
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] External custom resume drawer failed: {exc}")
            return ""

    async def apply_external_ats_job(
        self,
        job: dict,
        external_url: str,
        *,
        resume_path: str = "",
        auto_submit: bool = False,
    ) -> bool:
        """Apply to a company ATS URL discovered outside Jobright.

        This is used by LinkedIn jobs that do not expose Easy Apply. It reuses
        the persistent Jobright profile so the autofill extension is available.
        """
        self.auto_submit = auto_submit
        self.last_apply_status = "started"
        self.last_apply_detail = ""
        self._workday_session_expired = False
        self._field_fixer = ResumeFieldFixer()
        submitted = False
        resolved_resume = resolve_resume_path(self.config, preferred=resume_path)

        # Clean and rewrite Teamtailor URLs to load the application form directly
        if "teamtailor.com" in external_url.lower() and "/applications/new" not in external_url.lower():
            base_url = external_url.split('?')[0].rstrip('/')
            external_url = f"{base_url}/applications/new"

        console.print(f"[magenta]Jobright ATS:[/magenta] External apply for {job.get('title')} @ {job.get('company')}")
        page = await self._start_browser(load_extensions=True)
        try:
            await page.goto(external_url, wait_until="domcontentloaded", timeout=45000)
            await self._delay(3, 5)
            console.print(f"[magenta]Jobright ATS:[/magenta] Company portal loaded: {page.url}")

            entered_form = await self._click_ats_apply_button(page)
            await self._delay(3, 5)
            if getattr(self, "_workday_session_expired", False):
                return self._set_apply_outcome(
                    "workday_session_expired",
                    "Workday redirected to sign-in. Re-authenticate this company Workday portal, then rerun apply.",
                )

            if await self._portal_needs_login(page):
                console.print("[magenta]Jobright ATS:[/magenta] Login required — attempting configured portal login.")
                await self._company_portal_login(page)
                await self._delay(4, 6)

            # ── Claude ATS scoring + resume tailoring fallback ────────────────
            # Run regardless of whether Orion produced a resume. Gives us a score
            # and a tailored PDF when Orion failed (the ~60% failure rate case).
            self._last_ats_score = 0
            self._last_ats_missing_keywords: list = []
            self._last_application_method = "Direct ATS"
            try:
                _jd_text = await self._extract_job_description(page)
                if _jd_text:
                    console.print("[cyan]Claude:[/cyan] Calculating ATS score…")
                    _tailored = await self._claude_ats_and_tailor(job, _jd_text)
                    if _tailored:
                        self._last_ats_score = _tailored.get("ats_score", 0)
                        self._last_ats_missing_keywords = _tailored.get("missing_keywords", [])
                        _score_color = (
                            "green" if self._last_ats_score >= 85
                            else "yellow" if self._last_ats_score >= 60
                            else "red"
                        )
                        console.print(f"[{_score_color}]Claude ATS Score: {self._last_ats_score}/100[/{_score_color}]")
                        if self._last_ats_missing_keywords:
                            console.print(f"[dim]Missing keywords: {', '.join(self._last_ats_missing_keywords[:8])}[/dim]")
                        # If Orion didn't produce a tailored resume, generate one via Claude
                        _base_resume = resolve_resume_path(self.config)
                        if not resolved_resume or resolved_resume == _base_resume:
                            _claude_pdf = await self._generate_tailored_resume_pdf(job, _tailored)
                            if _claude_pdf:
                                resolved_resume = _claude_pdf
                                self._last_tailored_resume_path = _claude_pdf
                                self._last_application_method = "Claude Tailored"
                        # ATS gate: warn only — never block auto-submit
                        if self._last_ats_score > 0 and self._last_ats_score < 85 and auto_submit:
                            console.print(
                                f"[yellow]⚠  ATS score {self._last_ats_score}/100 is below 85 — "
                                "proceeding anyway (warn-only gate).[/yellow]"
                            )
            except Exception as _ce:
                console.print(f"[dim]Claude ATS block error (non-fatal): {_ce}[/dim]")

            if resolved_resume:
                await self._upload_resume_if_prompted(page, resolved_resume)
            else:
                console.print("[yellow]Jobright ATS:[/yellow] No local resume file found for upload fallback.")

            current_portal = page.url
            if "myworkdayjobs.com" not in current_portal:
                if not entered_form and not await self._looks_like_application_form(page):
                    family = await self._detect_portal_family(page)
                    controls = await self._visible_controls_snapshot(page)
                    return self._set_apply_outcome(
                        f"{family}_form_not_reached" if family != "generic" else "form_not_reached",
                        (
                            f"Company portal did not expose an application form after opening "
                            f"{current_portal}. Visible controls: {self._format_controls_snapshot(controls)}"
                        ),
                    )
                console.print("[magenta]Jobright ATS:[/magenta] Triggering Jobright autofill extension…")
                if await self._trigger_autofill(page):
                    await self._delay(10, 15)
                else:
                    console.print("[yellow]Jobright ATS:[/yellow] Autofill trigger not found; trying generic fill/upload fallback.")
                    await self._delay(3, 5)

            if resolved_resume:
                await self._upload_resume_if_prompted(page, resolved_resume)
            await self._field_fixer.fix_fields(page)

            # Teamtailor-specific form filling (Crunchbase, etc.)
            if "teamtailor.com" in page.url.lower():
                await self._fill_teamtailor_form(page)

            if not await self._looks_like_application_form(page):
                family = await self._detect_portal_family(page)
                controls = await self._visible_controls_snapshot(page)
                return self._set_apply_outcome(
                    f"{family}_form_not_detected" if family != "generic" else "form_not_detected",
                    (
                        f"ATS page loaded but no application/review form was detected at "
                        f"{page.url}. Visible controls: {self._format_controls_snapshot(controls)}"
                    ),
                )

            submitted = await self._confirm_and_submit(page, job, auto_submit=auto_submit)
            if submitted:
                self.last_apply_status = "submitted"
                self.last_apply_detail = "External ATS application submitted successfully."
        except Exception as exc:
            console.print(f"[red]External ATS apply error:[/red] {exc}")
            self.last_apply_status = "external_ats_error"
            self.last_apply_detail = str(exc)
        finally:
            await self._close_browser()

        return submitted

    def _best_jobright_match(self, source_job: dict, candidates: list[dict]) -> dict | None:
        wanted_title = self._tokenize_match_text(source_job.get("title", ""))
        wanted_company = self._tokenize_match_text(source_job.get("company", ""))
        best = None
        best_score = 0.0
        for candidate in candidates:
            title_score = self._overlap_score(wanted_title, self._tokenize_match_text(candidate.get("title", "")))
            company_score = self._overlap_score(wanted_company, self._tokenize_match_text(candidate.get("company", "")))
            score = (title_score * 0.75) + (company_score * 0.25)
            if score > best_score:
                best = candidate
                best_score = score
        return best if best and best_score >= 0.28 else None

    def _tokenize_match_text(self, value: str) -> set[str]:
        stop = {"and", "the", "of", "for", "to", "in", "a", "an", "remote", "senior"}
        return {
            token
            for token in re.findall(r"[a-z0-9]+", (value or "").lower())
            if len(token) > 2 and token not in stop
        }

    def _overlap_score(self, wanted: set[str], found: set[str]) -> float:
        if not wanted or not found:
            return 0.0
        return len(wanted & found) / max(len(wanted), 1)

    async def _generate_tailored_resume(self, page, job: dict) -> str:
        """Run Jobright's Orion resume tool on the current Jobright job page."""
        # Skip immediately if Orion failed earlier in this session — avoids
        # burning 2-4 minutes per job on timeouts that will never succeed.
        if not JobrightScraper._orion_tailoring_available:
            return ""
        console.print("[magenta]Jobright Tailor:[/magenta] Opening Orion AI resume modal…")
        before = self._latest_tailored_resume()
        current_ui_result = await self._generate_tailored_resume_current_ui(page, job, before)
        if current_ui_result:
            return current_ui_result
        # If the current-UI path marked Orion as unavailable, bail now — don't
        # run the 10s wait_for_selector that follows.
        if not JobrightScraper._orion_tailoring_available:
            return ""

        try:
            tool_card = await page.wait_for_selector(
                '[class*="tool-card-active"], [class*="tool-card"]',
                timeout=10000,
            )
            if tool_card:
                await tool_card.click()
                await self._delay(2, 3)
        except Exception:
            console.print("[yellow]Jobright Tailor:[/yellow] Resume tool card not found.")
            return ""

        improve_btn = None
        for pat in [
            'button:text-matches("Improve My Resume for This Job", "i")',
            'button:text-matches("Improve My Resume", "i")',
            'button:text-matches("Customize.*Resume", "i")',
            'button:text-matches("Generate.*Resume", "i")',
        ]:
            try:
                improve_btn = await page.wait_for_selector(pat, timeout=8000)
                if improve_btn:
                    break
            except Exception:
                continue
        if not improve_btn:
            console.print("[yellow]Jobright Tailor:[/yellow] Improve Resume button not found.")
            JobrightScraper._orion_tailoring_available = False
            return ""

        await improve_btn.click()
        await self._delay(2, 3)
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

        gen_btn = None
        for pat in [
            'button:text-matches("Generate My New Resume", "i")',
            'button:text-matches("Generate.*Resume", "i")',
            'button:text-matches("^Generate$", "i")',
            'button:text-matches("Create.*Resume", "i")',
        ]:
            try:
                gen_btn = await page.query_selector(pat)
                if gen_btn:
                    break
            except Exception:
                continue
        if not gen_btn:
            console.print("[yellow]Jobright Tailor:[/yellow] Generate button not found.")
            return ""

        await gen_btn.click()
        console.print("[magenta]Jobright Tailor:[/magenta] Generating tailored resume…")
        for _ in range(15):
            await asyncio.sleep(2)
            buttons = await page.evaluate(
                "Array.from(document.querySelectorAll('button')).map(b=>b.textContent.trim())"
            )
            if any("Download" in text for text in buttons):
                break

        download_btn = await page.query_selector('button:text-matches("Download", "i")')
        if not download_btn:
            console.print("[yellow]Jobright Tailor:[/yellow] Download button not found after generation.")
            return ""

        filename = self._tailored_resume_filename(job)
        save_path = TAILORED_RESUMES_DIR / filename
        try:
            async with page.expect_download(timeout=15000) as download_info:
                await download_btn.click()
            download = await download_info.value
            await download.save_as(str(save_path))
            console.print(f"[green]Jobright Tailor:[/green] Tailored resume saved: {save_path}")
            return str(save_path)
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] Download failed: {exc}")
            return ""

    async def _generate_tailored_resume_current_ui(self, page, job: dict, before: Path | None = None) -> str:
        """Handle Jobright's Orion UI: wizard → existing-resume view → chat fallback."""
        try:
            # ── 1. Existing tailored resume: just download it (fastest path) ────
            if await self._open_custom_resume_view(page):
                result = await self._download_or_regenerate_custom_resume(page, job, before)
                if result:
                    return result

            # ── 2. Orion resume wizard (primary generation path, current UI) ────
            wizard_result = await self._run_orion_resume_wizard(page, job, before)
            if wizard_result:
                return wizard_result

            # ── 3. Legacy: ASK ORION chat fallback ───────────────────────────────
            ask_clicked = await self._click_first_button_text(page, [r"ASK\s+ORION"])
            if ask_clicked:
                await self._delay(3, 5)

                prompt = (
                    "Tailor my resume for this job. Focus the summary, leadership bullets, "
                    "cloud/AI/security experience, ATS keywords, and measurable outcomes for "
                    f"{job.get('title', 'this role')} at {job.get('company', 'this company')}. "
                    "If a resume download or generated resume option is available, prepare it now."
                )
                textarea = None
                for sel in [
                    'textarea[placeholder*="Ask" i]',
                    'textarea[class*="copilot" i]',
                    'textarea',
                    '[contenteditable="true"]',
                ]:
                    try:
                        textarea = await page.wait_for_selector(sel, timeout=7000)
                        if textarea:
                            break
                    except Exception:
                        continue
                if textarea:
                    await textarea.click()
                    try:
                        await textarea.fill(prompt)
                    except Exception:
                        await page.keyboard.type(prompt)
                    await page.keyboard.press("Enter")
                    console.print("[magenta]Jobright Tailor:[/magenta] Asked Orion to tailor the resume for this job.")
                    await self._delay(8, 12)

            if await self._open_custom_resume_view(page):
                result = await self._download_or_regenerate_custom_resume(page, job, before)
                if result:
                    return result

            for _ in range(3):
                result = await self._download_visible_resume(page, job, before)
                if result:
                    return result
                await asyncio.sleep(3)

            console.print("[yellow]Jobright Tailor:[/yellow] Orion opened, but no downloadable tailored resume was exposed.")
            JobrightScraper._orion_tailoring_available = False
            return ""
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] Current Orion UI path failed: {exc}")
            return ""

    async def _open_custom_resume_view(self, page) -> bool:
        """Open Jobright's current tailored resume editor/view if present."""
        opened = await self._click_first_button_text(page, [
            r"View\s+Custom\s+Resume",
            r"View\s+Your\s+Tailored\s+Resume",
            r"Custom\s+Resume",
            r"Tailored\s+Resume",
        ])
        if opened:
            console.print("[magenta]Jobright Tailor:[/magenta] Opened custom resume view.")
            await self._delay(3, 5)
            return True
        return False

    async def _download_or_regenerate_custom_resume(self, page, job: dict, before: Path | None = None) -> str:
        """Download or regenerate the custom resume from an already-open view.

        Tries a direct download first; if the resume needs (re)generation, delegates
        to _run_orion_resume_wizard which robustly handles Full Edit + Select All.
        """
        await self._delay(1, 2)
        result = await self._download_visible_resume(page, job, before)
        if result:
            return result

        # Wizard handles Improve → Full Edit → Select All → Generate → Download
        wizard_result = await self._run_orion_resume_wizard(page, job, before)
        if wizard_result:
            return wizard_result

        # The drawer may already be on Step 2 from a prior partial run.
        generated = await self._click_jobright_resume_control(page, [
            r"Generate\s+My\s+New\s+Resume",
            r"Generate.*Resume",
            r"^Generate$",
        ], timeout_ms=5000)
        if generated:
            console.print("[magenta]Jobright Tailor:[/magenta] Resuming custom resume generation.")
            for _ in range(60):
                await asyncio.sleep(2)
                result = await self._download_visible_resume(page, job, before)
                if result:
                    return result

        # Final fallback: bare Regenerate button (no keyword/edit-type step)
        regenerated = await self._click_jobright_resume_control(page, [r"^Regenerate$"], timeout_ms=5000)
        if regenerated:
            console.print("[magenta]Jobright Tailor:[/magenta] Regenerating custom resume.")
            for _ in range(40):
                await asyncio.sleep(3)
                result = await self._download_visible_resume(page, job, before)
                if result:
                    return result
        return ""

    async def _click_jobright_resume_control(self, page, patterns: list[str], timeout_ms: int = 10000) -> bool:
        """Click a visible Jobright resume drawer control by text."""
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
        while asyncio.get_event_loop().time() < deadline:
            try:
                clicked = await page.evaluate(
                    """
                    (patterns) => {
                        const regexes = patterns.map(p => new RegExp(p, 'i'));
                        const root = document.querySelector('.ant-drawer-open') || document;
                        const controls = Array.from(root.querySelectorAll([
                            'button',
                            'a',
                            '[role="button"]',
                            'input[type="button"]',
                            'input[type="submit"]'
                        ].join(',')));
                        const visible = el => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.visibility !== 'hidden' &&
                                style.display !== 'none' &&
                                !el.disabled &&
                                el.getAttribute('aria-disabled') !== 'true' &&
                                rect.width > 0 &&
                                rect.height > 0;
                        };
                        const match = controls.find(el => {
                            if (!visible(el)) return false;
                            const text = [
                                el.innerText,
                                el.textContent,
                                el.value,
                                el.getAttribute('aria-label'),
                                el.getAttribute('title')
                            ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                            return text && regexes.some(re => re.test(text));
                        });
                        if (!match) return false;
                        match.scrollIntoView({block: 'center', inline: 'center'});
                        match.click();
                        return true;
                    }
                    """,
                    patterns,
                )
                if clicked:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
        return False

    async def _run_orion_resume_wizard(self, page, job: dict, before: Path | None = None) -> str:
        """Execute the Jobright Orion 3-step resume wizard reliably.

        Step 1 – See Your Difference  : click "Improve My Resume for This Job"
        Step 2 – Align Your Resume    : Full Edit radio + Select All keywords → Generate
        Step 3 – Review Your New Resume: download the tailored PDF

        Returns the local path to the downloaded resume, or "" on failure.
        """
        # ── Step 1 → 2: open the wizard ──────────────────────────────────────
        improve_btn = None
        for pat in [
            'button:text-matches("Improve My Resume for This Job", "i")',
            'button:text-matches("Improve My Resume", "i")',
            '[role="button"]:text-matches("Improve My Resume", "i")',
        ]:
            try:
                btn = await page.query_selector(pat)
                if btn and await btn.is_visible():
                    improve_btn = btn
                    break
            except Exception:
                continue

        if not improve_btn:
            return ""  # Wizard entry point not present on this page

        await improve_btn.click()
        console.print("[magenta]Jobright Tailor:[/magenta] Orion wizard — Step 1 clicked.")
        await self._delay(2, 3)

        # ── Wait for Step 2 (Align Your Resume) to render ────────────────────
        step2_loaded = False
        for sel in [
            'button:text-matches("Generate My New Resume", "i")',
            'text="Choose sections to enhance"',
            'text="Align Your Resume"',
            'text="Add missing skill keywords"',
        ]:
            try:
                await page.wait_for_selector(sel, timeout=12000)
                step2_loaded = True
                break
            except Exception:
                continue
        if not step2_loaded:
            console.print("[yellow]Jobright Tailor:[/yellow] Orion Step 2 did not load in time.")
            return ""

        await self._delay(0.5, 1.0)

        # ── Step 2a: select Full Edit radio ──────────────────────────────────
        full_edit_result = await page.evaluate("""
        () => {
            // Method 1: find the <input type="radio"> whose associated label starts with "Full Edit"
            for (const radio of document.querySelectorAll('input[type="radio"]')) {
                const label = (radio.labels && radio.labels[0]) ||
                              radio.closest('label') ||
                              (radio.id ? document.querySelector('label[for="' + radio.id + '"]') : null);
                const text = ((label && label.textContent) || radio.parentElement?.textContent || '').trim();
                if (/^full\\s+edit/i.test(text)) {
                    if (!radio.checked) radio.click();
                    return radio.checked ? 'radio-selected' : 'radio-click-attempted';
                }
            }
            // Method 2: click the innermost element whose own first-text-node starts with "Full Edit"
            for (const el of document.querySelectorAll('label, span, div, p')) {
                const firstText = (el.childNodes[0] && el.childNodes[0].nodeType === 3
                    ? el.childNodes[0].textContent : el.textContent || '').trim();
                if (/^full\\s+edit/i.test(firstText)) {
                    el.click();
                    return 'text-node-click';
                }
            }
            return 'full-edit-not-found';
        }
        """)
        console.print(f"[magenta]Jobright Tailor:[/magenta] Full Edit: {full_edit_result}")
        await self._delay(0.3, 0.6)

        # ── Step 2b: select ALL skill keywords ───────────────────────────────
        kw_result = await page.evaluate("""
        () => {
            // Prefer the "Select all" button (becomes "Unselect all" when already all-selected)
            const controls = Array.from(
                document.querySelectorAll('button, a, [role="button"], span[class*="select"]')
            );
            const selectAllBtn = controls.find(el =>
                /^select\\s+all$/i.test((el.textContent || el.innerText || '').trim())
            );
            if (selectAllBtn) {
                selectAllBtn.click();
                return 'select-all-btn-clicked';
            }
            // Fallback: individually check every unchecked keyword checkbox
            const unchecked = Array.from(
                document.querySelectorAll('input[type="checkbox"]:not(:checked)')
            );
            if (unchecked.length > 0) {
                unchecked.forEach(cb => cb.click());
                return 'checked-' + unchecked.length + '-boxes';
            }
            return 'all-keywords-already-checked';
        }
        """)
        console.print(f"[magenta]Jobright Tailor:[/magenta] Keywords: {kw_result}")
        await self._delay(0.5, 1.0)

        # ── Step 2c: click Generate My New Resume ────────────────────────────
        gen_btn = None
        for pat in [
            'button:text-matches("Generate My New Resume", "i")',
            'button:text-matches("Generate.*Resume", "i")',
            'button:text-matches("^Generate$", "i")',
        ]:
            try:
                btn = await page.query_selector(pat)
                if btn and await btn.is_visible():
                    gen_btn = btn
                    break
            except Exception:
                continue
        if not gen_btn:
            console.print("[yellow]Jobright Tailor:[/yellow] Generate button not found after wizard step 2.")
            return ""

        await gen_btn.click()
        console.print("[magenta]Jobright Tailor:[/magenta] Generating tailored resume (10–20 s)…")

        # ── Step 3: wait for Review step (Download Resume or APPLY NOW) ───────
        for _ in range(15):
            await asyncio.sleep(2)
            btns = await page.evaluate(
                "Array.from(document.querySelectorAll('button, a'))"
                ".map(b => (b.textContent || b.innerText || '').trim())"
            )
            if any(re.search(r'download\s*resume|apply\s*now', t, re.IGNORECASE) for t in btns):
                console.print("[magenta]Jobright Tailor:[/magenta] Orion Step 3 — review loaded.")
                break
        else:
            console.print("[yellow]Jobright Tailor:[/yellow] Timed out waiting for Orion review step.")
            JobrightScraper._orion_tailoring_available = False
            return ""

        await self._delay(1.0, 2.0)

        # ── Download the tailored resume ──────────────────────────────────────
        result = await self._download_visible_resume(page, job, before)
        if result:
            console.print(f"[green]Jobright Tailor:[/green] Orion wizard complete → {result}")
        else:
            console.print("[yellow]Jobright Tailor:[/yellow] Review step loaded but download failed.")
        return result

    async def _download_visible_resume(self, page, job: dict, before: Path | None = None) -> str:
        download_btn = None
        for pat in [
            'button:text-matches("Download.*Resume", "i")',
            'button:text-matches("Download", "i")',
            'a:text-matches("Download.*Resume", "i")',
            'a:text-matches("Download", "i")',
        ]:
            try:
                download_btn = await page.query_selector(pat)
                if download_btn and await download_btn.is_visible():
                    break
            except Exception:
                continue
        if download_btn:
            filename = self._tailored_resume_filename(job)
            save_path = TAILORED_RESUMES_DIR / filename
            try:
                async with page.expect_download(timeout=5000) as download_info:
                    await download_btn.click()
                download = await download_info.value
                await download.save_as(str(save_path))
                if save_path.exists() and save_path.stat().st_size > 0:
                    console.print(f"[green]Jobright Tailor:[/green] Tailored resume saved: {save_path}")
                    return str(save_path)
                console.print(f"[yellow]Jobright Tailor:[/yellow] Download completed but saved file is empty: {save_path}")
                return ""
            except Exception:
                try:
                    await download_btn.click()
                    await page.wait_for_timeout(750)
                except Exception:
                    pass
                dropdown_result = await self._download_from_open_dropdown(page, job)
                if dropdown_result:
                    return dropdown_result
                try:
                    async with page.expect_download(timeout=10000) as download_info:
                        await download_btn.click()
                    download = await download_info.value
                    await download.save_as(str(save_path))
                    if save_path.exists() and save_path.stat().st_size > 0:
                        console.print(f"[green]Jobright Tailor:[/green] Tailored resume saved: {save_path}")
                        return str(save_path)
                except Exception as exc:
                    console.print(f"[yellow]Jobright Tailor:[/yellow] Visible resume download failed: {exc}")
        latest = self._latest_tailored_resume()
        if latest and (not before or latest != before or latest.stat().st_mtime > before.stat().st_mtime):
            return str(latest)
        return ""

    async def _save_resume_download_from_click(self, page, click_target, save_path: Path) -> str:
        try:
            async with page.expect_download(timeout=15000) as download_info:
                await click_target.click()
            download = await download_info.value
            await download.save_as(str(save_path))
            if save_path.exists() and save_path.stat().st_size > 0:
                console.print(f"[green]Jobright Tailor:[/green] Tailored resume saved: {save_path}")
                return str(save_path)
            console.print(f"[yellow]Jobright Tailor:[/yellow] Dropdown download saved an empty file: {save_path}")
        except Exception as exc:
            console.print(f"[yellow]Jobright Tailor:[/yellow] Dropdown resume download failed: {exc}")
        return ""

    async def _download_from_open_dropdown(self, page, job: dict) -> str:
        """Handle Jobright's Ant dropdown under Download Resume."""
        filename = self._tailored_resume_filename(job)
        save_path = TAILORED_RESUMES_DIR / filename

        for _ in range(10):
            try:
                visible_dropdowns = page.locator(".ant-dropdown:not(.ant-dropdown-hidden), [role='menu']")
                if await visible_dropdowns.count():
                    break
            except Exception:
                pass
            await page.wait_for_timeout(250)

        dropdown_items = page.locator(
            ".ant-dropdown:not(.ant-dropdown-hidden) [role='menuitem'], "
            ".ant-dropdown:not(.ant-dropdown-hidden) li, "
            ".ant-dropdown:not(.ant-dropdown-hidden) button, "
            ".ant-dropdown:not(.ant-dropdown-hidden) a, "
            "[role='menu'] [role='menuitem'], "
            "[role='menu'] li, "
            "[role='menu'] button, "
            "[role='menu'] a"
        )

        candidates = []
        try:
            count = await dropdown_items.count()
        except Exception:
            count = 0
        for idx in range(count):
            item = dropdown_items.nth(idx)
            try:
                if not await item.is_visible(timeout=500):
                    continue
                text = ""
                try:
                    text = (await item.inner_text(timeout=500)).strip()
                except Exception:
                    pass
                candidates.append((item, text))
            except Exception:
                continue

        preferred = [
            item for item, text in candidates
            if re.search(r"\b(pdf|docx?|download|resume)\b", text or "", re.IGNORECASE)
        ]
        for item in preferred or [item for item, _text in candidates]:
            result = await self._save_resume_download_from_click(page, item, save_path)
            if result:
                return result
        return ""

    def _latest_tailored_resume(self) -> Path | None:
        candidates = [
            p for p in TAILORED_RESUMES_DIR.glob("*")
            if p.is_file() and p.suffix.lower() in {".pdf", ".doc", ".docx"}
        ]
        return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    async def _click_first_button_text(self, page, patterns: list[str]) -> bool:
        return await page.evaluate(
            """
            (patterns) => {
                const regexes = patterns.map(p => new RegExp(p, 'i'));
                const controls = Array.from(document.querySelectorAll([
                    'button',
                    'a',
                    '[role="button"]',
                    'input[type="button"]',
                    'input[type="submit"]',
                    '[class*="button"]',
                    '[class*="card"]',
                    '[class*="tool"]',
                    '[class*="resume"]'
                ].join(',')));
                const visible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        !el.disabled &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                const match = controls.find(el => {
                    const text = [el.innerText, el.textContent, el.getAttribute('aria-label')]
                        .filter(Boolean).join(' ').trim();
                    return visible(el) && text && regexes.some(re => re.test(text));
                });
                if (!match) return false;
                match.scrollIntoView({block: 'center', inline: 'center'});
                match.click();
                return true;
            }
            """,
            patterns,
        )

    def _tailored_resume_filename(self, job: dict) -> str:
        raw = f"{job.get('company','company')}-{job.get('title','resume')}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")[:120]
        return f"{safe or 'tailored-resume'}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"

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

            try:
                await page.goto("https://jobright.ai/jobs/external", wait_until="domcontentloaded", timeout=30000)
                await self._delay(2, 3)
                await self._dismiss_jobright_popups(page)
                external_jobs = await self._extract_jobs(page)
                seen = {job["job_id"] for job in jobs}
                new_external_jobs = [job for job in external_jobs if job["job_id"] not in seen]
                if new_external_jobs:
                    jobs.extend(new_external_jobs)
                    console.print(f"[magenta]Jobright:[/magenta] Added {len(new_external_jobs)} external job(s).")
            except Exception as exc:
                console.print(f"[yellow]Jobright:[/yellow] External jobs scrape skipped: {exc}")

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
                    const url = href ? new URL(href, JOBRIGHT_BASE).href : '';
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
        url = job.get("url", "")
        if "jobright.ai" not in url:
            console.print(f"[magenta]Jobright ATS:[/magenta] Delegating external URL apply: {url}")
            tailored_path = await self.tailor_resume_for_external_job(job)
            # Persist so _confirm_and_submit / outcome messages can reference it
            self._last_tailored_resume_path = tailored_path
            return await self.apply_external_ats_job(
                job,
                url,
                resume_path=tailored_path,
                auto_submit=auto_submit
            )

        self.auto_submit = auto_submit
        self.last_apply_status = "started"
        self.last_apply_detail = ""
        self._workday_session_expired = False
        self._field_fixer = ResumeFieldFixer()
        console.print(f"\n[magenta]Jobright Apply:[/magenta] {job.get('title')} @ {job.get('company')}")
        page = await self._start_browser(load_extensions=True)
        submitted = False

        try:
            # ── Step 1: Load job page ─────────────────────────────────────────
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(3, 4)

            # ── Step 1a: Capture ATS URL from Agent popup, then dismiss it ──────
            # _dismiss_jobright_popups() reads any ATS href from the popup DOM
            # BEFORE closing the modal, saving a separate extraction round-trip.
            popup_ext_url = await self._dismiss_jobright_popups(page)

            # Check page is still live
            page_text = await page.evaluate("document.body.innerText")
            if "no longer available" in page_text.lower():
                console.print(f"[yellow]Jobright:[/yellow] Job no longer listed — skipping.")
                raise JobExpiredError("Job no longer available")

            # ── Step 2: Extract external ATS URL (popup URL takes priority) ───
            # Fall back to __NEXT_DATA__ / anchor scan only when popup had nothing.
            ext_url = popup_ext_url or await self._extract_external_url(page)
            console.print(f"[magenta]Jobright:[/magenta] ATS URL: {ext_url or '(will try Apply Now)'}")

            # ── Step 3: Generate/download a tailored resume if Jobright exposes it
            tailored_resume_path = await self._generate_tailored_resume(page, job)
            resume_path = resolve_resume_path(self.config, preferred=tailored_resume_path)
            if tailored_resume_path:
                console.print(f"[green]Jobright:[/green] Using tailored resume: {tailored_resume_path}")
            elif resume_path:
                console.print(f"[yellow]Jobright:[/yellow] Tailored resume unavailable; using configured resume: {resume_path}")
            else:
                console.print("[yellow]Jobright:[/yellow] No tailored or configured resume path available.")

            # ── Step 6: Open ATS via Jobright Apply button (triggers extension autofill)
            # Close the Orion resume modal first
            await page.evaluate(
                "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}))"
            )
            await self._delay(1, 2)

            # Last resort URL extraction if not yet captured
            if not ext_url:
                ext_url = await self._extract_external_url(page)

            if not ext_url:
                console.print("[red]Jobright:[/red] Could not find company ATS URL — skipping.")
                return self._set_apply_outcome(
                    "missing_ats_url",
                    "Could not extract the company ATS URL from the Jobright posting.",
                )

            # Validate the URL has a real hostname
            try:
                from urllib.parse import urlparse as _urlparse
                _p = _urlparse(ext_url)
                _host = _p.hostname or ""
                if not _host or _host in ("www.", "www") or "." not in _host:
                    console.print(f"[red]Jobright:[/red] ATS URL has no valid hostname ({ext_url!r}) — skipping.")
                    return self._set_apply_outcome(
                        "bad_ats_url",
                        f"Extracted ATS URL has no valid hostname: {ext_url!r}. Check the Jobright posting manually.",
                    )
            except Exception:
                pass

            # ── Preferred: click Apply on the Jobright card to trigger extension ──
            # The Jobright autofill extension fires its full form-fill routine when
            # the ATS page is opened via the Jobright "Apply Now" button, rather than
            # navigated directly.  Capture the new tab that opens.
            company_page = None
            console.print("[magenta]Jobright:[/magenta] Clicking Apply on Jobright card to trigger extension autofill…")
            try:
                async with self._context.expect_page(timeout=12000) as _new_page:
                    # Try clicking "Apply Now" / "Apply" on the Jobright job card
                    clicked = await self._click_visible_control_by_text(page, [
                        r'^apply now$', r'^apply$', r'^quick apply$', r'^easy apply$', r'^start application$',
                    ])
                    if not clicked:
                        await page.evaluate("""
                            () => {
                                const el = [...document.querySelectorAll('button,a')]
                                    .find(b => /^apply( now)?$/i.test((b.textContent||'').trim()));
                                if (el) el.click();
                            }
                        """)
                company_page = await _new_page.value
                await company_page.wait_for_load_state("domcontentloaded", timeout=30000)
                await self._delay(8, 12)  # give extension time to inject and autofill
                if company_page and "teamtailor.com" in company_page.url.lower() and "/applications/new" not in company_page.url.lower():
                    base_url = company_page.url.split('?')[0].rstrip('/')
                    await company_page.goto(f"{base_url}/applications/new", wait_until="domcontentloaded", timeout=30000)
                    await self._delay(3, 5)
                console.print(f"[green]Jobright:[/green] Extension opened ATS: {company_page.url[:80]}")
            except Exception as _e:
                console.print(f"[yellow]Jobright:[/yellow] Apply button didn't open new tab ({_e}); using direct navigation")

            # ── Fallback: open ATS URL directly ──────────────────────────────────
            if company_page is None:
                if ext_url and "teamtailor.com" in ext_url.lower() and "/applications/new" not in ext_url.lower():
                    base_url = ext_url.split('?')[0].rstrip('/')
                    ext_url = f"{base_url}/applications/new"
                console.print(f"[magenta]Jobright:[/magenta] Opening company portal: {ext_url[:80]}")
                company_page = await self._context.new_page()
                await company_page.goto(ext_url, wait_until="domcontentloaded", timeout=45000)
                await self._delay(3, 5)

            if resume_path:
                await self._upload_resume_if_prompted(company_page, resume_path)

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
            entered_form = await self._click_ats_apply_button(company_page)
            await self._delay(3, 5)
            if resume_path:
                await self._upload_resume_if_prompted(company_page, resume_path)
            if getattr(self, "_workday_session_expired", False):
                return self._set_apply_outcome(
                    "workday_session_expired",
                    "Workday redirected to sign-in. Re-authenticate this company Workday portal in the Playwright profile, then rerun apply.",
                )
            if not entered_form and self.last_apply_status not in ("started", "", None):
                console.print(
                    f"[yellow]Jobright:[/yellow] Portal blocked before form: {self.last_apply_status}"
                )
                return False

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
                if not entered_form and not await self._looks_like_application_form(company_page):
                    console.print("[yellow]Jobright:[/yellow] Could not enter the ATS application form — skipping this job.")
                    if self.last_apply_status not in ("started", "", None):
                        return False
                    family = await self._detect_portal_family(company_page)
                    controls = await self._visible_controls_snapshot(company_page)
                    return self._set_apply_outcome(
                        f"{family}_form_not_reached" if family != "generic" else "form_not_reached",
                        (
                            f"Company portal did not expose an application form after opening "
                            f"{current_portal}. Visible controls: {self._format_controls_snapshot(controls)}"
                        ),
                    )
                # ── Claude ATS scoring + resume tailoring fallback (Jobright path) ──
                self._last_ats_score = 0
                self._last_ats_missing_keywords = []
                self._last_application_method = "Jobright Assisted"
                try:
                    _jd_text = await self._extract_job_description(company_page)
                    if _jd_text:
                        console.print("[cyan]Claude:[/cyan] Calculating ATS score…")
                        _tailored = await self._claude_ats_and_tailor(job, _jd_text)
                        if _tailored:
                            self._last_ats_score = _tailored.get("ats_score", 0)
                            self._last_ats_missing_keywords = _tailored.get("missing_keywords", [])
                            _score_color = (
                                "green" if self._last_ats_score >= 85
                                else "yellow" if self._last_ats_score >= 60
                                else "red"
                            )
                            console.print(f"[{_score_color}]Claude ATS Score: {self._last_ats_score}/100[/{_score_color}]")
                            if self._last_ats_missing_keywords:
                                console.print(f"[dim]Missing keywords: {', '.join(self._last_ats_missing_keywords[:8])}[/dim]")
                            # If Orion didn't produce a tailored resume, generate one via Claude
                            if not resume_path or resume_path == resolve_resume_path(self.config):
                                _claude_pdf = await self._generate_tailored_resume_pdf(job, _tailored)
                                if _claude_pdf:
                                    resume_path = _claude_pdf
                                    self._last_tailored_resume_path = _claude_pdf
                                    self._last_application_method = "Claude Tailored"
                                    if resume_path:
                                        await self._upload_resume_if_prompted(company_page, resume_path)
                            if self._last_ats_score > 0 and self._last_ats_score < 85 and auto_submit:
                                console.print(
                                    f"[yellow]⚠  ATS score {self._last_ats_score}/100 is below 85 — "
                                    "proceeding anyway (warn-only gate).[/yellow]"
                                )
                except Exception as _ce:
                    console.print(f"[dim]Claude ATS block error (non-fatal): {_ce}[/dim]")

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
            if not await self._looks_like_application_form(company_page):
                console.print("[yellow]Jobright:[/yellow] ATS page does not look like an application form or review page — skipping submit step.")
                if self.last_apply_status not in ("started", "", None):
                    return False
                family = await self._detect_portal_family(company_page)
                controls = await self._visible_controls_snapshot(company_page)
                controls_text = self._format_controls_snapshot(controls)

                # Special case: Workday job listing page with "Sign In" button visible
                # means the Playwright profile has no saved session for this company's
                # Workday tenant.  Classify as session-expired so it shows up in the
                # session-blocked queue rather than as a generic form_not_detected.
                try:
                    portal_url = company_page.url
                except Exception:
                    portal_url = ""
                if "myworkdayjobs.com" in portal_url and "BUTTON Sign In" in controls_text:
                    console.print("[yellow]Jobright:[/yellow] Workday portal requires sign-in — marking as session-needed.")
                    self._workday_session_expired = True
                    return self._set_apply_outcome(
                        "workday_session_expired",
                        f"Workday portal at {portal_url} requires sign-in. "
                        "Run: python src/main.py prepare-sessions to authenticate this tenant.",
                    )

                return self._set_apply_outcome(
                    f"{family}_form_not_detected" if family != "generic" else "form_not_detected",
                    (
                        f"ATS page loaded but no application/review form was detected at "
                        f"{portal_url}. Visible controls: {controls_text}"
                    ),
                )
            submitted = await self._confirm_and_submit(company_page, job, auto_submit=auto_submit)
            if submitted:
                self.last_apply_status = "submitted"
                self.last_apply_detail = "Application submitted successfully."
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
            self.last_apply_status = "error"
            self.last_apply_detail = str(exc)
        finally:
            await self._close_browser()

        return submitted

    async def prepare_session(self, job: dict) -> None:
        """Open the external ATS portal in the persistent profile for login/session refresh.

        For Workday specifically: navigates to the /apply/autofillWithResume URL and
        displays step-by-step sign-in instructions when the session is expired.
        The persistent profile saves the new cookies automatically so future apply
        runs skip this step entirely.
        """
        console.print(
            f"\n[magenta]Jobright Session Prep:[/magenta] {job.get('title')} @ {job.get('company')}"
        )
        # Reset recovery flag so the interactive prompt fires if needed
        self._workday_recovery_attempted = False
        page = await self._start_browser(load_extensions=True)
        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
            await self._delay(2, 3)
            popup_ext_url = await self._dismiss_jobright_popups(page)
            ext_url = popup_ext_url or await self._extract_external_url(page)
            if not ext_url:
                console.print("[red]Jobright:[/red] Could not find company ATS URL for session prep.")
                return

            company_page = await self._context.new_page()
            await company_page.goto(ext_url, wait_until="domcontentloaded", timeout=45000)
            await self._delay(4, 6)
            family = await self._detect_portal_family(company_page)
            console.print(f"[cyan]Portal:[/cyan] {company_page.url}")
            console.print(f"[cyan]Detected:[/cyan] {family}")

            if family == "workday":
                await self._click_ats_apply_button(company_page)
                # After clicking Apply, Workday may redirect to sign-in.
                # _workday_handle_post_chooser (called by _click_ats_apply_button) will
                # already offer the interactive prompt — but if the browser is still on
                # a login page when we arrive here, show the full setup guide.
                try:
                    portal_url = company_page.url.lower()
                    on_login = any(
                        w in portal_url
                        for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."]
                    )
                except Exception:
                    on_login = False

                if on_login or getattr(self, "_workday_session_expired", False):
                    console.print(
                        "\n[bold yellow]┌─ Workday Session Setup ─────────────────────────────────────┐[/bold yellow]\n"
                        "[bold yellow]│[/bold yellow]  Workday is showing a sign-in page.\n"
                        "[bold yellow]│[/bold yellow]\n"
                        "[bold yellow]│[/bold yellow]  Steps:\n"
                        "[bold yellow]│[/bold yellow]  1. Sign in with your work email + password in the browser.\n"
                        "[bold yellow]│[/bold yellow]  2. Complete any MFA or SSO prompts.\n"
                        "[bold yellow]│[/bold yellow]  3. You should reach the job page or 'Start Application'.\n"
                        "[bold yellow]│[/bold yellow]  4. Your session is saved automatically — this is a one-time step.\n"
                        "[bold yellow]│[/bold yellow]     Future 'apply' runs will skip this entirely.\n"
                        "[bold yellow]└─────────────────────────────────────────────────────────────┘[/bold yellow]\n"
                    )
                    self._workday_session_expired = False  # reset; user handles it here
            elif family == "microsoft":
                await self._handle_microsoft_apply(company_page)
            elif family == "brassring":
                await self._handle_brassring_apply(company_page)
            else:
                await self._click_ats_apply_button(company_page)

            controls = await self._visible_controls_snapshot(company_page)
            console.print(f"[dim]Visible controls: {self._format_controls_snapshot(controls)}[/dim]")

            if sys.stdin and sys.stdin.isatty():
                console.print(
                    "\n[bold yellow]Browser window is open.[/bold yellow] "
                    "Sign in, refresh your portal account, or click through to the application form."
                )
                input("  Press Enter when this portal session is ready > ")
            else:
                console.print(
                    "[yellow]Non-interactive run: diagnostic prep only. "
                    "Run from Terminal to pause while you sign in.[/yellow]"
                )
        finally:
            await self._close_browser()

    async def _click_visible_control_by_text(self, page, patterns: list[str]) -> bool:
        """Click the first visible button/link/control whose text matches."""
        return await page.evaluate(
            """
            (patterns) => {
                const regexes = patterns.map(p => new RegExp(p, 'i'));
                const candidates = Array.from(document.querySelectorAll([
                    'button',
                    'a',
                    '[role="button"]',
                    'input[type="button"]',
                    'input[type="submit"]'
                ].join(',')));
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        !el.disabled &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                for (const el of candidates) {
                    if (!isVisible(el)) continue;
                    const text = [
                        el.innerText,
                        el.textContent,
                        el.value,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title')
                    ].filter(Boolean).join(' ').trim();
                    if (regexes.some(re => re.test(text))) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            patterns,
        )

    async def _visible_controls_snapshot(self, page, limit: int = 40) -> list[dict]:
        """Return visible buttons/links/inputs to make ATS failures diagnosable."""
        try:
            return await page.evaluate(
                """
                (limit) => {
                    const candidates = Array.from(document.querySelectorAll([
                        'button',
                        'a',
                        '[role="button"]',
                        'input[type="button"]',
                        'input[type="submit"]'
                    ].join(',')));
                    const visible = el => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            !el.disabled &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };
                    return candidates.filter(visible).slice(0, limit).map(el => ({
                        tag: el.tagName,
                        role: el.getAttribute('role') || '',
                        text: [
                            el.innerText,
                            el.textContent,
                            el.value,
                            el.getAttribute('aria-label'),
                            el.getAttribute('title')
                        ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().slice(0, 120),
                        href: el.href || el.getAttribute('href') || '',
                    }));
                }
                """,
                limit,
            )
        except Exception:
            return []

    def _format_controls_snapshot(self, controls: list[dict]) -> str:
        if not controls:
            return "No visible controls detected."
        lines = []
        for c in controls[:15]:
            label = c.get("text") or c.get("href") or "(no text)"
            lines.append(f"{c.get('tag','?')} {label}")
        return "; ".join(lines)

    async def _has_visible_control_matching(self, page, patterns: list[str]) -> bool:
        try:
            return await page.evaluate(
                """
                (patterns) => {
                    const regexes = patterns.map(p => new RegExp(p, 'i'));
                    const candidates = Array.from(document.querySelectorAll([
                        'button',
                        'a',
                        '[role="button"]',
                        'input[type="button"]',
                        'input[type="submit"]'
                    ].join(',')));
                    const visible = el => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            !el.disabled &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };
                    return candidates.some(el => {
                        if (!visible(el)) return false;
                        const text = [
                            el.innerText,
                            el.textContent,
                            el.value,
                            el.getAttribute('aria-label'),
                            el.getAttribute('title')
                        ].filter(Boolean).join(' ').trim();
                        return regexes.some(re => re.test(text));
                    });
                }
                """,
                patterns,
            )
        except Exception:
            return False

    async def _detect_portal_family(self, page) -> str:
        try:
            url = page.url.lower()
        except Exception:
            url = ""
        if "myworkdayjobs.com" in url:
            return "workday"
        if "brassring.com" in url:
            return "brassring"
        if "careers.microsoft.com" in url or "microsoft.com/careers" in url:
            return "microsoft"
        if "greenhouse.io" in url:
            return "greenhouse"
        if "lever.co" in url:
            return "lever"
        return "generic"

    async def _looks_like_login_wall(self, page) -> bool:
        try:
            return await page.evaluate(
                """
                () => {
                    const body = (document.body?.innerText || '').toLowerCase();
                    const password = !!document.querySelector('input[type="password"]');
                    const email = !!document.querySelector('input[type="email"], input[name*="email" i], input[placeholder*="email" i]');
                    const loginText = /(sign in|log in|login|create account|forgot password|sso|single sign-on)/i.test(body);
                    return password || (email && loginText);
                }
                """
            )
        except Exception:
            return False

    async def _click_first_matching_link_or_button(self, page, patterns: list[str]) -> bool:
        """Like _click_visible_control_by_text but returns href navigation for anchors when possible."""
        return await page.evaluate(
            """
            (patterns) => {
                const regexes = patterns.map(p => new RegExp(p, 'i'));
                const candidates = Array.from(document.querySelectorAll([
                    'button',
                    'a',
                    '[role="button"]',
                    'input[type="button"]',
                    'input[type="submit"]'
                ].join(',')));
                const visible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        !el.disabled &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                for (const el of candidates) {
                    if (!visible(el)) continue;
                    const text = [
                        el.innerText,
                        el.textContent,
                        el.value,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title')
                    ].filter(Boolean).join(' ').trim();
                    if (!regexes.some(re => re.test(text))) continue;
                    const href = el.href || el.getAttribute('href');
                    if (href && !href.startsWith('#')) {
                        window.location.href = href;
                    } else {
                        el.click();
                    }
                    return true;
                }
                return false;
            }
            """,
            patterns,
        )

    async def _looks_like_application_form(self, page) -> bool:
        """Detect whether the ATS page is actually in an apply/review workflow."""
        try:
            if await self._looks_like_login_wall(page):
                return False
            return await page.evaluate(
                """
                () => {
                    const url = location.href.toLowerCase();
                    const body = (document.body?.innerText || '').toLowerCase();
                    const signInPage = /create account\\/?sign in|sign in with email|sign in with google|sign in with linkedin/.test(body);
                    if (signInPage) return false;
                    const formish = document.querySelectorAll('input, textarea, select, form').length;
                    const reviewText = /(review|submit|confirm|questionnaire|work experience|contact information|my information|resume|experience|profile|candidate profile|employment|job submission)/i.test(body);
                    const workflowUrl = /(\\/apply|review|submit|confirm|application)/i.test(url);
                    return reviewText || formish >= 3 || (workflowUrl && formish > 0);
                }
                """
            )
        except Exception:
            return False

    async def _dismiss_jobright_popups(self, page) -> str:
        """
        Dismiss any Jobright upsell/info popups that block the page:
        - "You can now access Jobright Agent (Beta)" — click the X
        - "Apply 5x Faster with Autofill" — click 'Yes, Enable Autofill Now'

        Returns any external ATS URL found inside the popup BEFORE closing it, so
        apply() can use it without an extra DOM extraction pass.
        """
        # ── Capture ATS URL from popup BEFORE closing ─────────────────────────
        # The Jobright Agent (Beta) popup often contains the company ATS apply link.
        # Reading it here prevents the window from closing before we can get the URL.
        _ATS = [
            "myworkdayjobs.com", "greenhouse.io", "lever.co", "taleo.net",
            "icims.com", "smartrecruiters.com", "bamboohr.com", "ashbyhq.com",
            "workable.com", "brassring.com", "successfactors.com",
            "myworkday.com", "jobs.lever.co", "apply.workable.com",
            "recruitingbypaycor.com", "paylocity.com",
        ]
        captured_url = ""
        try:
            captured_url = await page.evaluate(
                """
                (atsList) => {
                    const containers = document.querySelectorAll(
                        '.ant-modal-content, [class*="modal-content"], ' +
                        '[class*="popup-content"], [role="dialog"], ' +
                        '[class*="agent-modal"], [class*="AgentModal"]'
                    );
                    for (const modal of containers) {
                        for (const a of modal.querySelectorAll('a[href]')) {
                            const h = a.href || '';
                            if (h && !h.includes('jobright.ai') &&
                                atsList.some(p => h.includes(p))) return h;
                        }
                        for (const el of modal.querySelectorAll(
                            '[data-href],[data-url],[data-apply-url]'
                        )) {
                            const h = (
                                el.getAttribute('data-href') ||
                                el.getAttribute('data-url') ||
                                el.getAttribute('data-apply-url') || ''
                            );
                            if (h && !h.includes('jobright.ai') &&
                                atsList.some(p => h.includes(p))) return h;
                        }
                    }
                    return '';
                }
                """,
                _ATS,
            )
            if captured_url:
                console.print(
                    f"[dim]Jobright: captured ATS URL from Agent popup: {captured_url[:70]}[/dim]"
                )
        except Exception:
            pass

        # ── Now close the popup ───────────────────────────────────────────────
        popup_selectors = [
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            '.ant-modal-close',
            '[class*="modal-close"]',
            '[class*="close-btn"]',
        ]
        for sel in popup_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000)
                if btn and await btn.is_visible():
                    await btn.click()
                    console.print("[dim]Jobright: dismissed popup[/dim]")
                    await self._delay(0.5, 1)
            except Exception:
                continue

        # If "Apply 5x Faster with Autofill" modal appears, enable autofill
        try:
            yes_btn = await page.wait_for_selector(
                'button:text-matches("Yes, Enable Autofill", "i")',
                timeout=3000
            )
            if yes_btn and await yes_btn.is_visible():
                await yes_btn.click()
                console.print("[dim]Jobright: enabled autofill via popup[/dim]")
                await self._delay(1, 2)
        except Exception:
            pass

        return captured_url

    async def _extract_external_url(self, page) -> str:
        """
        Extract the company ATS application URL directly from the Jobright DOM.
        Tries Next.js page data first, then scans anchor tags for known ATS hostnames.
        """
        url = await page.evaluate("""
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
        if url:
            return url
        return await self._reveal_external_url_with_autofill(page)

    async def _reveal_external_url_with_autofill(self, page) -> str:
        """Click Jobright's current APPLY WITH AUTOFILL control and capture the ATS URL."""
        original_pages = set(page.context.pages)
        clicked = False
        try:
            clicked = await self._click_first_button_text(page, [r"APPLY\\s+WITH\\s+AUTOFILL", r"Apply"])
        except Exception:
            clicked = False
        if not clicked:
            return ""

        await self._delay(2, 3)
        for opened in page.context.pages:
            try:
                if opened not in original_pages and opened.url and "jobright.ai" not in opened.url:
                    return opened.url
            except Exception:
                continue
        try:
            if page.url and "jobright.ai" not in page.url:
                return page.url
        except Exception:
            pass
        return ""

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
        Falls back to COMPANY_EMAIL_ALT / COMPANY_PASSWORD_ALT if the primary fails.

        Two credential sets are configured:
          COMPANY_EMAIL       — alarkins.jsearch@yahoo.com (most portals: Motorola, Booz Allen,
                                Greenhouse, SAIC, PNC, Capital One, ManTech, etc.)
          COMPANY_EMAIL_ALT   — anthonyclarkins@icloud.com (government/contractor: GDIT, etc.)

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
            # ── Step 0: If "Create Account" page is showing, click Sign In first ──
            # Portals like Home Depot show "Create Account" by default.
            # We need to click the "Sign In" link to get to the login form.
            try:
                sign_in_link = await page.query_selector(
                    'a:text-matches("^Sign In$", "i"), button:text-matches("^Sign In$", "i")'
                )
                if sign_in_link and await sign_in_link.is_visible():
                    await sign_in_link.click()
                    console.print("[magenta]Jobright:[/magenta] Clicked Sign In link")
                    await self._delay(2, 3)
            except Exception:
                pass

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

            console.print(f"[green]Jobright:[/green] Login attempted with {email} → {page.url}")

            # If the page still looks like a login page, retry with the alt email
            login_indicators = ["login", "signin", "sign-in", "auth", "create-account"]
            still_on_login = any(w in page.url.lower() for w in login_indicators)
            if still_on_login:
                alt_email = os.environ.get("COMPANY_EMAIL_ALT", "")
                alt_password = os.environ.get("COMPANY_PASSWORD_ALT", password)
                if alt_email and alt_email != email:
                    console.print(f"[yellow]Jobright:[/yellow] Primary login may have failed — retrying with alt email {alt_email}…")
                    await self._company_portal_login_with(page, alt_email, alt_password)

        except Exception as e:
            console.print(f"[yellow]Jobright:[/yellow] Portal login attempt failed: {e}")

    async def _company_portal_login_with(self, page, email: str, password: str) -> None:
        """Re-run the portal login form with a specific email/password (alt-credential retry)."""
        try:
            for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[placeholder*="email" i]']:
                try:
                    elem = await page.wait_for_selector(sel, timeout=3000)
                    if elem:
                        await elem.triple_click()
                        await elem.type(email, delay=80)
                        await self._delay(0.5, 1)
                        break
                except Exception:
                    continue
            for sel in ['button:text-matches("^Next$","i")', 'button:text-matches("^Continue$","i")', 'button[type="submit"]']:
                try:
                    btn = await page.wait_for_selector(sel, timeout=2000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(2, 3)
                        break
                except Exception:
                    continue
            try:
                pwd = await page.wait_for_selector('input[type="password"]', timeout=6000)
                if pwd:
                    await pwd.triple_click()
                    await pwd.type(password, delay=80)
                    await self._delay(0.5, 1)
            except Exception:
                return
            for sel in ['button:text-matches("^Sign In$","i")', 'button:text-matches("^Log In$","i")', 'button[type="submit"]']:
                try:
                    btn = await page.wait_for_selector(sel, timeout=2000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await self._delay(5, 8)
                        break
                except Exception:
                    continue
            console.print(f"[green]Jobright:[/green] Alt login attempted with {email} → {page.url}")
        except Exception as e:
            console.print(f"[yellow]Jobright:[/yellow] Alt portal login failed: {e}")

    async def _fill_teamtailor_form(self, page) -> None:
        """Fill Teamtailor application form fields that Jobright autofill misses.

        Covers: LinkedIn Profile URL, work-auth radio (Yes), sponsorship radio (No),
        and any other visible text/select inputs left empty.
        """
        import os
        linkedin_url = "https://www.linkedin.com/in/anthonyclarkins"

        # Wait briefly for the form to settle after autofill
        await asyncio.sleep(1)

        # --- LinkedIn Profile URL ---
        linkedin_selectors = [
            "input[name*='linkedin' i]",
            "input[placeholder*='linkedin' i]",
            "input[id*='linkedin' i]",
            "input[aria-label*='linkedin' i]",
        ]
        for sel in linkedin_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    current_val = await el.input_value()
                    if not current_val:
                        await el.fill(linkedin_url)
                    break
            except Exception:
                pass

        # --- Work authorization radio (Yes) ---
        work_auth_yes_selectors = [
            # Teamtailor often renders label text next to a radio input
            "label:has-text('Yes')",
            "input[type='radio'][value='true']",
            "input[type='radio'][value='yes' i]",
            "input[type='radio'][value='1']",
        ]
        # We need the one near "authorized" / "legal" / "work in" context
        try:
            # Preferred: find all radio labels, pick the "Yes" near work-auth question
            await page.evaluate("""() => {
                const labels = Array.from(document.querySelectorAll('label'));
                const workSection = labels.find(l =>
                    /authorized|legally|work in the us/i.test(l.closest('fieldset,div,section')?.textContent || '')
                    && /^yes$/i.test(l.textContent.trim())
                );
                if (workSection) {
                    const radio = workSection.querySelector('input[type=radio]') ||
                        document.querySelector('#' + workSection.htmlFor);
                    if (radio) radio.click();
                }
            }""")
        except Exception:
            pass
        # Fallback: click any visible "Yes" radio inside a fieldset
        for sel in work_auth_yes_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    break
            except Exception:
                pass

        await asyncio.sleep(0.5)

        # --- Sponsorship required radio (No) ---
        try:
            await page.evaluate("""() => {
                const labels = Array.from(document.querySelectorAll('label'));
                const sponsorSection = labels.find(l =>
                    /sponsor/i.test(l.closest('fieldset,div,section')?.textContent || '')
                    && /^no$/i.test(l.textContent.trim())
                );
                if (sponsorSection) {
                    const radio = sponsorSection.querySelector('input[type=radio]') ||
                        document.querySelector('#' + sponsorSection.htmlFor);
                    if (radio) radio.click();
                }
            }""")
        except Exception:
            pass
        # Fallback: value="false" / value="no"
        for sel in ["input[type='radio'][value='false']", "input[type='radio'][value='no' i]", "input[type='radio'][value='0']"]:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    break
            except Exception:
                pass

        await asyncio.sleep(0.5)

        # --- Any remaining required text inputs left blank ---
        import json as _json
        profile_path = Path(__file__).parent.parent.parent / "state" / "profile.json"
        profile: dict = {}
        if profile_path.exists():
            try:
                profile = _json.loads(profile_path.read_text())
            except Exception:
                pass
        personal = profile.get("personal_info", {})
        fill_map = {
            "phone": personal.get("phone", ""),
            "city": personal.get("city", ""),
            "zip": personal.get("zip", ""),
        }
        for keyword, value in fill_map.items():
            if not value:
                continue
            try:
                el = page.locator(f"input[name*='{keyword}' i]:visible, input[placeholder*='{keyword}' i]:visible").first
                if await el.count() and await el.is_visible():
                    if not await el.input_value():
                        await el.fill(value)
            except Exception:
                pass

        console.print("[cyan]Teamtailor:[/cyan] form fields filled (LinkedIn, work auth, sponsorship)")

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
        #
        # Also handles company-branded Workday domains (CVS Health uses jobs.cvshealth.com,
        # Palo Alto uses wd5.myworkdayjobs.com, etc.) — detect via Workday-specific
        # DOM attribute data-automation-id which is exclusively used by Workday's framework.
        _is_workday_page = 'myworkdayjobs.com' in current_url
        if not _is_workday_page:
            try:
                _is_workday_page = await page.evaluate("""
                    () => !!(
                        document.querySelector('[data-automation-id="jobPostingApplyButton"]') ||
                        document.querySelector('[data-automation-id="candidateHomeLink"]') ||
                        document.querySelector('[data-automation-id="text-input"]') ||
                        document.querySelector('[class*="wd-Button-"]') ||
                        document.querySelector('[class*="wd-Text-"]') ||
                        (document.body?.innerHTML || '').includes('myworkdayjobs')
                    )
                """)
            except Exception:
                pass
        if _is_workday_page and '/apply' not in current_url:
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
        # After navigating to autofillWithResume (or on a company-branded Workday
        # portal), handle the "Start Your Application" chooser and sign-in gate.
        if _is_workday_page or 'myworkdayjobs.com' in current:
            await self._click_visible_control_by_text(page, [
                '^use my last application$',
                '^autofill with resume$',
                '^apply manually$',
                '^start application$',
            ])
            await self._delay(3, 5)
            await self._workday_handle_post_chooser(page)
            return await self._looks_like_application_form(page)

        # ── Microsoft careers ───────────────────────────────────────────────
        # Microsoft often renders an application shell where the job detail page
        # and form are separate client-side states. Click broad Apply/Sign-in
        # controls and classify login walls clearly instead of returning a vague
        # submit_not_found later.
        if 'careers.microsoft.com' in current.lower() or 'microsoft.com/careers' in current.lower():
            return await self._handle_microsoft_apply(page)

        # ── BrassRing ───────────────────────────────────────────────────────
        # BrassRing frequently hides the application behind "Apply to job" /
        # "Sign in" controls on a hash-based job details URL.
        if 'brassring.com' in current.lower():
            return await self._handle_brassring_apply(page)

        # ── Other ATS: click the Apply / Apply Now / entry CTA button ─────────
        apply_selectors = [
            # Workday fallback (data-automation-id only, not broad text matches)
            '[data-automation-id="jobPostingApplyButton"]',
            # Greenhouse / Lever
            '#apply_button',
            'a.btn-apply',
            '.apply-button',
            # Text-match on button elements
            'button:text-matches("^Apply Now$", "i")',
            'button:text-matches("^Apply$", "i")',
            'button:text-matches("Apply for This Job", "i")',
            'button:text-matches("Apply for Job", "i")',
            # SmartRecruiters / NBCUniversal — "I'm interested" is the entry CTA
            # that opens the one-click application form (not a nav link)
            'a:text-matches("I\'m interested", "i")',
            'button:text-matches("I\'m interested", "i")',
            # HRMDirect / SERVPRO — "START YOUR APPLICATION" anchor link
            'a:text-matches("START YOUR APPLICATION", "i")',
            'a:text-matches("Start Your Application", "i")',
            'a:text-matches("Start Application", "i")',
            # Teamtailor (Crunchbase, etc.)
            'button:text-matches("^Apply here$", "i")',
            'a:text-matches("^Apply here$", "i")',
            '.careersite-button',
            # Broad anchor fallback — many ATS portals use <a> not <button> for Apply
            'a:text-matches("^Apply Now$", "i")',
            'a:text-matches("^Apply$", "i")',
            'a:text-matches("Apply for This Job", "i")',
        ]

        for sel in apply_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=4000)
                if btn:
                    await btn.click()
                    console.print(f"[magenta]Jobright:[/magenta] Clicked Apply button → waiting for form…")
                    await self._delay(5, 8)
                    return True
            except Exception:
                continue

        # SmartRecruiters: JS click handles both straight and curly apostrophe variants
        # (selector text-matches won't find "I'm interested" if the HTML uses a curly apostrophe)
        try:
            _sr_url = page.url.lower()
            if 'smartrecruiters.com' in _sr_url:
                _sr_clicked = await page.evaluate("""
                    () => {
                        const els = [...document.querySelectorAll('a, button')];
                        const btn = els.find(el => /i[‘’']m interested/i.test(
                            (el.textContent || '').trim()
                        ));
                        if (btn) { btn.click(); return true; }
                        return false;
                    }
                """)
                if _sr_clicked:
                    console.print("[magenta]Jobright:[/magenta] SmartRecruiters — clicked 'I'm interested' (JS) → waiting for form…")
                    await self._delay(5, 8)
                    return True
        except Exception:
            pass

        clicked = await self._click_visible_control_by_text(page, [
            '^apply now$',
            '^apply$',
            'apply for this job',
            'apply for job',
            'start application',
            'begin application',
            "i'm interested",
            'start your application',
        ])
        if clicked:
            console.print("[magenta]Jobright:[/magenta] Clicked Apply control → waiting for form…")
            await self._delay(4, 6)
            return await self._looks_like_application_form(page)

        console.print("[dim]Jobright: No Apply button found — may already be on the application form[/dim]")
        return await self._looks_like_application_form(page)

    async def _handle_microsoft_apply(self, page) -> bool:
        console.print("[magenta]Jobright:[/magenta] Microsoft portal — locating apply flow…")
        if await self._looks_like_login_wall(page):
            self._set_apply_outcome(
                "microsoft_login_required",
                "Microsoft careers is showing a login/account wall. Sign in once in the Playwright profile, then rerun apply.",
            )
            return False

        clicked = False
        before_url = page.url
        for _ in range(8):
            try:
                explicit_apply = await page.query_selector(
                    'a:text-matches("Apply now", "i"), button:text-matches("Apply now", "i")'
                )
                if explicit_apply and await explicit_apply.is_visible():
                    await explicit_apply.click()
                    clicked = True
                    break
            except Exception:
                pass
            clicked = await self._click_first_matching_link_or_button(page, [
                '^apply now$',
                '^apply$',
                'apply for this job',
                'sign in to apply',
                'start application',
                'continue application',
            ])
            if clicked:
                break
            await self._delay(2, 3)

        if clicked:
            await self._delay(5, 8)
            if await self._looks_like_login_wall(page):
                self._set_apply_outcome(
                    "microsoft_login_required",
                    f"Microsoft careers redirected to login at {page.url}.",
                )
                return False
            apply_still_visible = await self._has_visible_control_matching(page, ['^apply now$', '^apply$'])
            url_changed = page.url != before_url
            if (url_changed or not apply_still_visible) and await self._looks_like_application_form(page):
                console.print("[green]Jobright:[/green] Microsoft application form detected.")
                return True

        controls = await self._visible_controls_snapshot(page)
        self._set_apply_outcome(
            "microsoft_apply_control_not_activated" if clicked else "microsoft_apply_not_reached",
            f"Could not enter Microsoft application flow at {page.url}. Visible controls: {self._format_controls_snapshot(controls)}",
        )
        return False

    async def _handle_brassring_apply(self, page) -> bool:
        console.print("[magenta]Jobright:[/magenta] BrassRing portal — locating apply flow…")
        if await self._looks_like_login_wall(page):
            self._set_apply_outcome(
                "brassring_login_required",
                "BrassRing is showing a login wall before the application form is reachable.",
            )
            return False

        clicked = await self._click_first_matching_link_or_button(page, [
            '^apply to job$',
            '^apply$',
            '^apply now$',
            'apply for this job',
            'start application',
            'create profile',
            'sign in',
            'login',
        ])
        if clicked:
            await self._delay(5, 8)
            if await self._looks_like_login_wall(page):
                self._set_apply_outcome(
                    "brassring_login_required",
                    f"BrassRing redirected to login/profile page at {page.url}.",
                )
                return False
            if await self._looks_like_application_form(page):
                console.print("[green]Jobright:[/green] BrassRing application form detected.")
                return True

        controls = await self._visible_controls_snapshot(page)
        self._set_apply_outcome(
            "brassring_apply_not_reached",
            f"Could not enter BrassRing application flow at {page.url}. Visible controls: {self._format_controls_snapshot(controls)}",
        )
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

        # ── A: Check if Workday redirected to a sign-in page ─────────────────
        # Filling Workday credentials programmatically crashes Chromium (SSO/bot
        # detection).  The persistent browser profile saves the session after the
        # first manual login, so this block is only reached when the session
        # has expired.  We detect the redirect and abort gracefully — the user
        # will need to re-run once manually to refresh the session.
        try:
            url = page.url.lower()
            on_login_page = any(w in url for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."])
        except Exception:
            on_login_page = False

        if not on_login_page:
            try:
                on_login_page = await page.evaluate(
                    """
                    () => {
                        const text = (document.body?.innerText || '').toLowerCase();
                        return /create account\\/?sign in|sign in with email|sign in with google|sign in with linkedin/.test(text);
                    }
                    """
                )
            except Exception:
                on_login_page = False

        if on_login_page:
            console.print(
                f"[bold red]Jobright: Workday session expired[/bold red] — portal URL: {page.url}"
            )
            self._workday_session_expired = True

            # ── Interactive recovery (TTY only, first attempt only) ───────────
            # When the user is sitting at a terminal, let them sign in RIGHT NOW
            # in the browser window and continue without re-running the command.
            # The persistent profile saves the new session automatically.
            if sys.stdin and sys.stdin.isatty() and not getattr(self, "_workday_recovery_attempted", False):
                self._workday_recovery_attempted = True
                console.print(
                    "\n[bold yellow]┌─ Workday Session Recovery ──────────────────────────────────┐[/bold yellow]\n"
                    "[bold yellow]│[/bold yellow]  The browser is showing the Workday sign-in page.\n"
                    "[bold yellow]│[/bold yellow]  1. Sign in with your work email + password in the browser.\n"
                    "[bold yellow]│[/bold yellow]  2. Complete any MFA / SSO prompts.\n"
                    "[bold yellow]│[/bold yellow]  3. Once you reach the job or 'Start Application' page,\n"
                    "[bold yellow]│[/bold yellow]     press Enter here to continue the wizard.\n"
                    "[bold yellow]└─────────────────────────────────────────────────────────────┘[/bold yellow]\n"
                )
                try:
                    input("  Press Enter after signing in > ")
                except (EOFError, KeyboardInterrupt):
                    notify_error(
                        "Workday session expired",
                        "Re-run: python src/main.py prepare-sessions to refresh your Workday session.",
                    )
                    return

                await self._delay(2, 3)
                try:
                    new_url = page.url.lower()
                    still_login = any(
                        w in new_url
                        for w in ["/login", "/signin", "/sign-in", "/auth", "login.", "sso."]
                    )
                except Exception:
                    still_login = True

                if not still_login:
                    # Session restored — clear the flag and re-run the wizard
                    self._workday_session_expired = False
                    console.print(
                        "[green]Jobright: Workday session restored — resuming application wizard…[/green]"
                    )
                    await self._workday_handle_post_chooser(page)
                    return
                else:
                    console.print(
                        "[yellow]Jobright: Still on Workday login page after confirmation.\n"
                        "  Run 'python src/main.py prepare-sessions' to set up the session.[/yellow]"
                    )
            else:
                notify_error(
                    "Workday session expired",
                    f"Run 'python src/main.py prepare-sessions' to refresh the Workday session.\n"
                    f"Portal: {page.url}",
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

        # ── B0: Re-check sign-in AFTER the chooser click ─────────────────────
        # Workday sometimes shows the chooser (Autofill / Apply Manually / Sign In)
        # on the landing page even when the session is expired.  After clicking
        # "Autofill with Resume" the page may still be at a sign-in gate rather
        # than the application form.  Detect this by looking for a prominent
        # "Sign In" button with no form fields present.
        try:
            session_gate = await page.evaluate(
                """
                () => {
                    const url = location.href.toLowerCase();
                    // Still on the login/SSO domain
                    if (['/login','/signin','/sign-in','/auth','login.','sso.'].some(w => url.includes(w)))
                        return true;
                    // Primary CTA is Sign In and there are no form inputs
                    const btns = [...document.querySelectorAll('button')];
                    const signInBtn = btns.find(b => /^sign in$/i.test(b.textContent.trim()));
                    const formInputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select').length;
                    return !!signInBtn && formInputs === 0;
                }
                """
            )
        except Exception:
            session_gate = False

        if session_gate:
            console.print(
                f"[bold red]Jobright: Workday session gate detected after chooser click[/bold red] — {page.url}"
            )
            self._workday_session_expired = True
            notify_error(
                "Workday session expired",
                f"Run 'python src/main.py prepare-sessions' to refresh the Workday session.\nPortal: {page.url}",
            )
            return

        # The extension fills fields on each step; we click 'Next' until we
        # reach the Review/Submit page (where the button text becomes 'Submit').
        console.print("[magenta]Jobright:[/magenta] Navigating Workday wizard steps…")
        for step_num in range(30):   # max 30 steps — GDIT/CVS/Citi have 15-20+ pages
            await self._delay(4, 6)  # wait for extension to fill the current step

            # Second-pass check: fix any empty or incorrect fields using resume profile
            if hasattr(self, "_field_fixer") and self._field_fixer:
                await self._field_fixer.fix_fields(page)


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

    async def _upload_resume_if_prompted(self, page, resume_path: str) -> bool:
        """Upload a configured resume to visible ATS file inputs when possible."""
        path = Path(resume_path).expanduser()
        if not path.exists():
            return False
        uploaded = False
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
            for file_input in file_inputs:
                accept = (await file_input.get_attribute("accept") or "").lower()
                name = (await file_input.get_attribute("name") or "").lower()
                label = ""
                try:
                    label = await file_input.evaluate(
                        """
                        node => {
                            const id = node.id;
                            const explicit = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                            if (explicit?.innerText) return explicit.innerText;
                            let p = node.parentElement;
                            for (let i = 0; p && i < 4; i++, p = p.parentElement) {
                                const txt = (p.innerText || '').trim();
                                if (txt) return txt;
                            }
                            return node.getAttribute('aria-label') || node.getAttribute('data-automation-id') || '';
                        }
                        """
                    )
                except Exception:
                    label = ""
                hints = " ".join([accept, name, label.lower()])
                if accept and not any(ext in accept for ext in [".pdf", ".doc", ".docx", "pdf", "word"]):
                    continue
                if any(word in hints for word in ["resume", "cv", "upload", "file", "attachment"]) or not hints.strip():
                    await file_input.set_input_files(str(path))
                    console.print(f"[green]Jobright ATS:[/green] Uploaded resume: {path.name}")
                    uploaded = True
                    await self._delay(1, 2)
        except Exception as exc:
            console.print(f"[yellow]Jobright ATS:[/yellow] Resume upload check failed: {exc}")
        return uploaded

    async def _trigger_autofill(self, page) -> bool:
        """
        Find and click the Jobright extension's autofill button injected into the ATS page.
        The extension (built with Plasmo) injects a content UI into the page DOM.

        If no explicit trigger button is found, wait up to 20 seconds for the extension
        to auto-inject and fill fields silently (it often does this without needing a click).
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

        # ── No explicit trigger found — wait for extension to auto-inject ────────
        # The Jobright extension often fills forms silently without a button click.
        # When opened via the Jobright Apply button, it fires automatically.
        # Wait up to 20 seconds for form fields to get populated.
        console.print("[dim]Jobright: no autofill button found — waiting 20s for extension auto-fill…[/dim]")
        try:
            await asyncio.sleep(20)
            # Check if any input fields got filled
            filled = await page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll('input:not([type="hidden"]):not([type="file"]), textarea')];
                    return inputs.some(i => (i.value || '').trim().length > 0);
                }
            """)
            if filled:
                console.print("[green]Jobright:[/green] Extension auto-filled form fields")
                return True
        except Exception:
            pass

        return False

    # ── Claude-powered helpers ─────────────────────────────────────────────────

    async def _extract_job_description(self, page) -> str:
        """Scrape the full job description text from the current ATS page."""
        jd_selectors = [
            '[data-automation-id="jobPostingDescription"]',  # Workday
            '.sr-job-description',                           # SmartRecruiters
            '[class*="jobDescription"]',
            '[class*="job-description"]',
            '#job-description',
            '#jobDescriptionText',
            '.jobsearch-jobDescriptionText',
            '.description__text',
            '.show-more-less-html__markup',
            'section.job-details',
        ]
        for sel in jd_selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if len(text) > 200:
                        return text[:6000]
            except Exception:
                pass
        try:
            return ((await page.evaluate("document.body.innerText")) or "")[:6000]
        except Exception:
            return ""

    def _extract_resume_text(self) -> str:
        """Extract plain text from the configured resume PDF, or fall back to profile.json."""
        resume_path = resolve_resume_path(self.config)
        if resume_path and resume_path.lower().endswith('.pdf') and os.path.isfile(resume_path):
            try:
                from pypdf import PdfReader
                reader = PdfReader(resume_path)
                text = "\n".join(p.extract_text() or "" for p in reader.pages).strip()
                if len(text) > 100:
                    return text[:6000]
            except Exception:
                pass
        profile_path = os.path.join("state", "profile.json")
        if os.path.isfile(profile_path):
            try:
                import json as _json
                with open(profile_path) as f:
                    return _json.dumps(_json.load(f), indent=2)[:4000]
            except Exception:
                pass
        return ""

    async def _claude_ats_and_tailor(self, job: dict, jd_text: str) -> dict:
        """Use Claude API (haiku) to score ATS match and generate tailored resume content.

        Returns dict with: ats_score, missing_keywords, matching_keywords,
        tailored_summary, tailored_bullets, cover_letter, recommendation.
        Returns empty dict on failure.
        """
        if not jd_text or len(jd_text) < 100:
            return {}
        try:
            import anthropic as _anthropic
            _api_key = os.environ.get("ANTHROPIC_API_KEY")
            _client = _anthropic.Anthropic(api_key=_api_key) if _api_key else _anthropic.Anthropic()
            resume_text = self._extract_resume_text()
            _prompt = (
                "You are an expert resume writer and ATS specialist. "
                "Analyze this job description against the candidate resume, then produce tailored content.\n\n"
                f"JOB: {job.get('title', '')} @ {job.get('company', '')}\n\n"
                f"JOB DESCRIPTION (truncated):\n{jd_text[:3000]}\n\n"
                f"CANDIDATE RESUME / PROFILE:\n{resume_text[:3000]}\n\n"
                "Respond ONLY with valid JSON in exactly this structure:\n"
                "{\n"
                '  "ats_score": <integer 0-100, 85+ means strong match>,\n'
                '  "missing_keywords": ["keyword1", "keyword2"],\n'
                '  "matching_keywords": ["keyword1", "keyword2"],\n'
                '  "recommendation": "one-line advice",\n'
                '  "tailored_summary": "2-3 sentence professional summary tailored to this role",\n'
                '  "tailored_bullets": [\n'
                '    {"role": "Most Recent Role Title", "bullets": ["Achievement 1", "Achievement 2"]},\n'
                '    {"role": "Second Role Title", "bullets": ["Achievement 1", "Achievement 2"]}\n'
                "  ],\n"
                '  "cover_letter": "3-paragraph cover letter for this specific role and company"\n'
                "}"
            )
            _msg = _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1800,
                messages=[{"role": "user", "content": _prompt}],
            )
            import json as _json
            _text = _msg.content[0].text.strip()
            if _text.startswith("```"):
                _text = re.sub(r"^```[a-z]*\n?", "", _text)
                _text = re.sub(r"\n?```$", "", _text)
            result = _json.loads(_text)
            return {
                "ats_score": int(result.get("ats_score", 0)),
                "missing_keywords": result.get("missing_keywords", []),
                "matching_keywords": result.get("matching_keywords", []),
                "recommendation": result.get("recommendation", ""),
                "tailored_summary": result.get("tailored_summary", ""),
                "tailored_bullets": result.get("tailored_bullets", []),
                "cover_letter": result.get("cover_letter", ""),
            }
        except Exception as _e:
            console.print(f"[dim]Claude ATS/tailor failed: {_e}[/dim]")
            return {}

    async def _generate_tailored_resume_pdf(self, job: dict, tailored: dict) -> str:
        """Render a tailored resume to PDF using Playwright's print engine (Jinja2-free).

        Returns the path to the generated PDF, or '' on failure.
        """
        try:
            import json as _json
            profile: dict = {}
            _ppath = os.path.join("state", "profile.json")
            if os.path.isfile(_ppath):
                with open(_ppath) as _f:
                    profile = _json.load(_f)
            info = profile.get("personal_info", {})
            name = f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or "Candidate"
            email = info.get("email", "")
            phone = info.get("phone", "")
            linkedin = profile.get("social_links", {}).get("linkedin", "")
            city = info.get("city", "")
            state_abbr = info.get("state", "")
            location = ", ".join(p for p in [city, state_abbr] if p)
            skills = list(profile.get("skills", []))
            for kw in tailored.get("missing_keywords", []):
                if kw and kw not in skills:
                    skills.append(kw)
            education = profile.get("education", [])
            work_history = profile.get("work_history", [])
            tailored_bullets = tailored.get("tailored_bullets", [])
            experience_html = ""
            for i, role in enumerate(tailored_bullets[:3]):
                wh = work_history[i] if i < len(work_history) else {}
                company = wh.get("company_name", "")
                title = wh.get("job_title", role.get("role", ""))
                start = wh.get("start_date", "")
                end = wh.get("end_date", "Present")
                bullets_html = "".join(f"<li>{b}</li>" for b in role.get("bullets", []))
                experience_html += (
                    f'<div class="exp-item">'
                    f'<div class="exp-header"><span class="exp-title">{title}</span>'
                    f'<span class="exp-dates">{start} – {end}</span></div>'
                    f'<div class="exp-company">{company}</div>'
                    f"<ul>{bullets_html}</ul></div>"
                )
            if not experience_html:
                for wh in work_history[:3]:
                    experience_html += (
                        f'<div class="exp-item">'
                        f'<div class="exp-header"><span class="exp-title">{wh.get("job_title","")}</span>'
                        f'<span class="exp-dates">{wh.get("start_date","")} – {wh.get("end_date","Present")}</span></div>'
                        f'<div class="exp-company">{wh.get("company_name","")}</div>'
                        f'<ul><li>{wh.get("description","")}</li></ul></div>'
                    )
            edu_html = ""
            for ed in education:
                edu_html += (
                    f'<div class="exp-item">'
                    f'<div class="exp-header"><span class="exp-title">{ed.get("degree","")} — {ed.get("major","")}</span>'
                    f'<span class="exp-dates">{ed.get("start_date","")} – {ed.get("end_date","")}</span></div>'
                    f'<div class="exp-company">{ed.get("school_name","")}</div></div>'
                )
            summary = (
                tailored.get("tailored_summary", "")
                or "Experienced technology leader with 18+ years delivering enterprise solutions "
                   "in government, defense, and commercial sectors."
            )
            skills_html = " &bull; ".join(skills[:20])
            contact_html = " | ".join(p for p in [email, phone, location, linkedin] if p)
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>"
                "body{font-family:Arial,sans-serif;font-size:11pt;color:#222;margin:0;padding:20px 30px}"
                "h1{font-size:20pt;margin:0 0 2px}"
                ".contact{font-size:9pt;color:#555;margin-bottom:10px}"
                "h2{font-size:12pt;border-bottom:1px solid #999;padding-bottom:2px;"
                "margin:12px 0 6px;text-transform:uppercase;letter-spacing:.05em}"
                ".summary,.skills{margin-bottom:8px;line-height:1.5}"
                ".exp-item{margin-bottom:10px}"
                ".exp-header{display:flex;justify-content:space-between;font-weight:bold}"
                ".exp-dates{font-weight:normal;font-size:10pt;color:#555}"
                ".exp-company{font-style:italic;font-size:10pt;margin-bottom:3px}"
                "ul{margin:4px 0 0 18px;padding:0}li{margin-bottom:2px;line-height:1.4}"
                "</style></head><body>"
                f"<h1>{name}</h1>"
                f"<div class='contact'>{contact_html}</div>"
                "<h2>Professional Summary</h2>"
                f"<div class='summary'>{summary}</div>"
                "<h2>Core Competencies</h2>"
                f"<div class='skills'>{skills_html}</div>"
                "<h2>Professional Experience</h2>"
                f"{experience_html}"
                "<h2>Education</h2>"
                f"{edu_html}"
                "</body></html>"
            )
            safe_title = re.sub(r'[^\w\-]', '_', (job.get('title') or 'role'))[:40]
            safe_co = re.sub(r'[^\w\-]', '_', (job.get('company') or 'co'))[:25]
            pdf_path = str(TAILORED_RESUMES_DIR / f"{safe_title}_{safe_co}_claude.pdf")
            _pdf_page = await self._context.new_page()
            try:
                await _pdf_page.set_content(html, wait_until="domcontentloaded")
                await _pdf_page.pdf(
                    path=pdf_path,
                    format="Letter",
                    margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                )
                console.print(f"[green]Claude Resume:[/green] Tailored PDF → {pdf_path}")
                return pdf_path
            finally:
                await _pdf_page.close()
        except Exception as _e:
            console.print(f"[dim]Claude resume PDF generation failed: {_e}[/dim]")
            return ""

    async def _run_pre_submission_validation(self, page) -> dict:
        """Check form state before submitting. Prints a checklist. Returns a dict of results."""
        results: dict = {}
        try:
            checks = await page.evaluate("""
                () => {
                    const inputs = [...document.querySelectorAll(
                        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), textarea, select'
                    )];
                    const fileInputs = [...document.querySelectorAll('input[type="file"]')];
                    const requiredEls = [...document.querySelectorAll('[required], [aria-required="true"]')];
                    const invalidEls = [...document.querySelectorAll(':invalid')].filter(e => e.tagName !== 'FORM');
                    const resumeUploaded = fileInputs.some(f => f.files && f.files.length > 0);
                    const hasName = inputs.some(i =>
                        /name|first|last/i.test(i.name || i.id || i.placeholder || '') &&
                        (i.value || '').trim()
                    );
                    const hasEmail = inputs.some(i =>
                        /email/i.test(i.name || i.id || i.type || i.placeholder || '') &&
                        (i.value || '').trim()
                    );
                    const hasPhone = inputs.some(i =>
                        /phone|tel/i.test(i.name || i.id || i.type || i.placeholder || '') &&
                        (i.value || '').trim()
                    );
                    const requiredFilled = requiredEls.every(el => {
                        if (el.type === 'checkbox' || el.type === 'radio') return el.checked;
                        return (el.value || '').trim().length > 0;
                    });
                    return {
                        resumeUploaded, hasName, hasEmail, hasPhone,
                        requiredFilled, invalidCount: invalidEls.length,
                        totalInputs: inputs.length
                    };
                }
            """)
            results = checks

            def _tick(ok: bool) -> str:
                return "✓" if ok else "?"

            def _col(ok: bool) -> str:
                return "green" if ok else "yellow"

            console.print("\n[bold cyan]── Pre-Submit Checklist ──[/bold cyan]")
            console.print(f"  [{_col(checks['resumeUploaded'])}]{_tick(checks['resumeUploaded'])}[/{_col(checks['resumeUploaded'])}] Resume uploaded")
            console.print(f"  [{_col(checks['hasName'])}]{_tick(checks['hasName'])}[/{_col(checks['hasName'])}] Name filled")
            console.print(f"  [{_col(checks['hasEmail'])}]{_tick(checks['hasEmail'])}[/{_col(checks['hasEmail'])}] Email filled")
            console.print(f"  [{_col(checks['hasPhone'])}]{_tick(checks['hasPhone'])}[/{_col(checks['hasPhone'])}] Phone filled")
            req_ok = checks['requiredFilled']
            console.print(f"  [{'green' if req_ok else 'red'}]{'✓' if req_ok else '✗'}[/{'green' if req_ok else 'red'}] Required questions answered")
            if checks['invalidCount'] > 0:
                console.print(f"  [yellow]⚠  {checks['invalidCount']} field(s) have validation errors[/yellow]")
        except Exception as _e:
            console.print(f"[dim]Validation checklist error (non-fatal): {_e}[/dim]")
        return results

    async def _confirm_and_submit(self, page, job: dict, auto_submit: bool = False) -> bool:
        """
        Find the final Submit / Apply button on the ATS page, show a preview,
        optionally ask for confirmation, then click.
        """
        submit_selectors = [
            # Workday final-step submit (data-automation-id) — highly specific, safe
            '[data-automation-id="bottom-navigation-next-button"]',
            '[data-automation-id*="submit" i]',
            'input[type="submit"][value*="Submit" i]',
            'input[type="button"][value*="Submit" i]',
            # Generic button text — Submit variants (unambiguous, final-step only)
            'button:text-matches("^Submit$", "i")',
            'button:text-matches("Submit Application", "i")',
            'button:text-matches("Send Application", "i")',
            'button:text-matches("Complete Application", "i")',
            'button:text-matches("Submit My Application", "i")',
            # Apply variants on BUTTONS (buttons inside forms are usually final-step)
            'button:text-matches("^Apply$", "i")',
            'button:text-matches("Apply Now", "i")',
            'button:text-matches("Apply for this job", "i")',
            'button:text-matches("Apply for Job", "i")',
            # aria-label submit (reliable signal)
            # NOTE: "I'm interested" is intentionally NOT here — it is a SmartRecruiters
            # entry CTA on the job listing page, handled by _click_ats_apply_button.
            '[aria-label*="submit" i]',
            # NOTE: Removed broad a:text-matches("Apply*") and partial-match fallbacks.
            # Those matched "Apply" entry buttons on job listing pages, causing false submits.
            # Portal-specific <a> Apply buttons are handled by _click_ats_apply_button first.
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
        family = await self._detect_portal_family(page)

        # ── Guard: refuse to submit if no form fields appear to be filled ────
        # This prevents false "Application submitted!" when the agent is on a
        # job listing page (not the actual application form) — the broad Apply
        # button selectors above can match entry CTAs on listing pages.
        if submit_btn:
            try:
                has_filled_fields = await page.evaluate("""
                    () => {
                        const inputs = [...document.querySelectorAll(
                            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]),' +
                            'textarea, select'
                        )];
                        // At least one visible input must have a non-empty value
                        return inputs.some(el => {
                            const val = (el.value || el.textContent || '').trim();
                            return val.length > 0 && !el.disabled;
                        });
                    }
                """)
            except Exception:
                has_filled_fields = True  # assume filled if we can't check

            if not has_filled_fields:
                console.print(
                    "[yellow]Jobright: Submit button found but form appears empty — "
                    "autofill did not run or this is a listing page, not the application form.[/yellow]"
                )
                console.print(f"[dim]Portal: {portal_url}[/dim]")
                return self._set_apply_outcome(
                    "form_empty_not_submitted",
                    f"Submit button was found at {portal_url} but all form fields were empty. "
                    "Jobright autofill did not populate the form — ensure the extension is active "
                    "and the Jobright session is logged in, then re-run.",
                )

        if not submit_btn and (auto_submit or not (sys.stdin and sys.stdin.isatty())):
            console.print(
                "[yellow]Jobright: Submit button not found and this run is non-interactive — "
                "skipping instead of prompting.[/yellow]"
            )
            console.print(f"[dim]Portal: {portal_url}[/dim]")
            controls = await self._visible_controls_snapshot(page)
            tailored_hint = getattr(self, "_last_tailored_resume_path", "") or ""
            if tailored_hint:
                console.print(f"[green]Jobright:[/green] Tailored resume ready for manual apply → {tailored_hint}")
            return self._set_apply_outcome(
                f"{family}_submit_not_found" if family != "generic" else "submit_not_found",
                (
                    f"Reached portal but could not find a final Submit/Apply button at "
                    f"{portal_url}. Visible controls: {self._format_controls_snapshot(controls)}"
                    + (f"\nTailored resume ready: {tailored_hint}" if tailored_hint else "")
                ),
            )

        # Run pre-submission validation checklist before showing the submit banner
        await self._run_pre_submission_validation(page)

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
            return self._set_apply_outcome(
                "submission_cancelled",
                "Final submission was not confirmed by the user.",
            )

        if submit_btn:
            # Use JS click to bypass Workday overlay divs that intercept pointer events
            try:
                await page.evaluate("btn => btn.click()", submit_btn)
            except Exception:
                # Fallback: dispatch a MouseEvent directly
                await page.evaluate("""btn => {
                    btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                }""", submit_btn)
            await self._delay(3, 5)
            # ── Record analytics for orchestrator to persist via extra_json ──
            self._apply_analytics = {
                "submitted": True,
                "submissionTime": datetime.utcnow().isoformat(),
                "applicationMethod": getattr(self, "_last_application_method", "Unknown"),
                "atsScore": getattr(self, "_last_ats_score", None),
                "resumeVersion": os.path.basename(getattr(self, "_last_tailored_resume_path", "") or ""),
                "missingKeywords": getattr(self, "_last_ats_missing_keywords", []),
                "jobrightAvailable": getattr(self, "_jobright_available", False),
                "jobrightUrl": getattr(self, "_jobright_url", ""),
                "interviewReceived": False,
                "offerReceived": False,
            }
            console.print("[green]✓ Application submitted![/green]")
            return True
        else:
            # No submit button found — user must click it manually in the browser
            if auto_submit:
                console.print("[red]Jobright: Submit button not found — cannot auto-submit. Skipping.[/red]")
                controls = await self._visible_controls_snapshot(page)
                tailored_hint = getattr(self, "_last_tailored_resume_path", "") or ""
                if tailored_hint:
                    console.print(f"[green]Jobright:[/green] Tailored resume ready for manual apply → {tailored_hint}")
                return self._set_apply_outcome(
                    f"{family}_submit_not_found" if family != "generic" else "submit_not_found",
                    (
                        f"Auto-submit requested, but no submit button was found at "
                        f"{portal_url}. Visible controls: {self._format_controls_snapshot(controls)}"
                        + (f"\nTailored resume ready: {tailored_hint}" if tailored_hint else "")
                    ),
                )
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
