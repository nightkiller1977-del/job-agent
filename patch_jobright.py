import re

with open("src/sources/jobright.py", "r") as f:
    content = f.read()

# We want to replace everything from `async def _confirm_and_submit(self, page, job: dict, auto_submit: bool = False) -> bool:`
# to the line right before `def _infer_remote_type(remote_raw: str, location: str) -> str:`

start_match = re.search(r'    async def _confirm_and_submit\(self, page, job: dict, auto_submit: bool = False\) -> bool:', content)
end_match = re.search(r'def _infer_remote_type\(remote_raw: str, location: str\) -> str:', content)

if not start_match or not end_match:
    print("Could not find start or end matches")
    exit(1)

start_idx = start_match.start()
end_idx = end_match.start() - 4 # To keep the blank lines before _infer_remote_type

new_func = """    async def _confirm_and_submit(self, page, job: dict, auto_submit: bool = False) -> bool:
        \"\"\"
        Phase 1: Detect Submit / Apply button using evaluate (does NOT click).
        Returns a stable descriptor.
        Phase 2: Validate fields and block auto-submit if invalid.
        Phase 3: Click and explicitly verify success signal (differentially).
        \"\"\"
        try:
            portal_url = page.url
        except Exception:
            portal_url = "(unknown)"
        family = await self._detect_portal_family(page)

        # ── Phase 1: Detect ──
        submit_descriptor = await self._safe_evaluate(page, \"\"\"() => {
            const patterns = [
                '^Submit$', 'Submit Application', 'Send Application',
                'Complete Application', 'Submit My Application',
                '^Apply$', 'Apply Now', 'Apply for this job', 'Apply for Job'
            ];
            const regexes = patterns.map(p => new RegExp(p, 'i'));
            const candidates = Array.from(document.querySelectorAll([
                'button', 'a', '[role="button"]', 'input[type="button"]', 'input[type="submit"]',
                '[data-automation-id="bottom-navigation-next-button"]',
                '[data-automation-id*="submit" i]',
                '[aria-label*="submit" i]'
            ].join(',')));
            
            const visible = el => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && rect.width > 0 && rect.height > 0;
            };

            for (const el of candidates) {
                if (!visible(el)) continue;
                
                const is_vendor = el.matches('[data-automation-id="bottom-navigation-next-button"]') || el.matches('[data-automation-id*="submit" i]');
                const text = (el.innerText || el.value || el.textContent || '').trim();
                const aria = (el.getAttribute('aria-label') || '').trim();
                
                if (is_vendor || regexes.some(r => r.test(text)) || regexes.some(r => r.test(aria))) {
                    return {
                        tag: el.tagName.toLowerCase(),
                        text: text,
                        aria: aria,
                        data_automation: el.getAttribute('data-automation-id') || ''
                    };
                }
            }
            return null;
        }\"\"\", default=None)

        # ── Guard: refuse to submit if no form fields appear to be filled ────
        has_filled_fields = False
        if submit_descriptor:
            try:
                has_filled_fields = await page.evaluate(\"\"\"
                    () => {
                        const inputs = [...document.querySelectorAll(
                            'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"]):not([type="checkbox"]):not([type="radio"]),' +
                            'textarea, select'
                        )];
                        return inputs.some(el => {
                            const val = (el.value || el.textContent || '').trim();
                            return val.length > 0 && !el.disabled;
                        });
                    }
                \"\"\")
            except Exception:
                has_filled_fields = True  # assume filled if we can't check

            if not has_filled_fields:
                from ..console import console
                console.print(
                    "[yellow]Jobright: Submit button found but form appears empty — "
                    "autofill did not run or this is a listing page, not the application form.[/yellow]"
                )
                console.print(f"[dim]Portal: {portal_url}[/dim]")
                return self._set_apply_outcome(ApplyOutcomeCode.FORM_EMPTY_NOT_SUBMITTED, f"Submit button was found at {portal_url} but all form fields were empty. "
                    "Jobright autofill did not populate the form — ensure the extension is active "
                    "and the Jobright session is logged in, then re-run."
                )

        if not submit_descriptor and (auto_submit or not (import sys; sys.stdin and sys.stdin.isatty())):
            from ..console import console
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
                ApplyOutcomeCode.SUBMIT_NOT_FOUND, portal_family=family if family != "generic" else "",
                detail=(
                    f"Reached portal but could not find a final Submit/Apply button at "
                    f"{portal_url}. Visible controls: {self._format_controls_snapshot(controls)}"
                    + (f"\\nTailored resume ready: {tailored_hint}" if tailored_hint else "")
                ),
            )

        # Run pre-submission validation checklist before showing the submit banner
        await self._run_pre_submission_validation(page)

        from ..console import console
        console.print(f"\\n[bold yellow]─── READY TO SUBMIT ───[/bold yellow]")
        console.print(f"  Job   : {job.get('title')} @ {job.get('company')}")
        console.print(f"  Portal: {portal_url}")
        if submit_descriptor:
            console.print(f"  [green]Submit button found ✓[/green]")
        else:
            console.print(f"  [yellow]Submit button not found — navigate to the final step in the browser[/yellow]")

        if auto_submit:
            console.print("[green]Jobright: Auto-submitting application (auto-submit active)![/green]")
            confirm = "y"
        else:
            try:
                confirm = input("\\n  Submit this application? [y/N] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
        if confirm != "y":
            console.print("[yellow]Jobright: Submission cancelled.[/yellow]")
            return self._set_apply_outcome(ApplyOutcomeCode.SUBMISSION_CANCELLED, "Final submission was not confirmed by the user.")

        if submit_descriptor:
            pre_click_text = await page.evaluate("document.body.innerText.toLowerCase()")
            
            clicked = await page.evaluate(\"\"\"(desc) => {
                const visible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && rect.width > 0 && rect.height > 0;
                };
                const candidates = Array.from(document.querySelectorAll(desc.tag)).filter(el => {
                    if (!visible(el)) return false;
                    const text = (el.innerText || el.value || el.textContent || '').trim();
                    const aria = (el.getAttribute('aria-label') || '').trim();
                    if (desc.text && text !== desc.text) return false;
                    if (desc.aria && aria !== desc.aria) return false;
                    if (desc.data_automation && el.getAttribute('data-automation-id') !== desc.data_automation) return false;
                    return true;
                });
                if (candidates.length === 1) {
                    try { candidates[0].click(); } catch(e) { candidates[0].dispatchEvent(new MouseEvent('click', {bubbles: true})); }
                    return true;
                }
                return false;
            }\"\"\", submit_descriptor)
            
            if not clicked:
                console.print("[red]Jobright: Submit button became ambiguous or disappeared before click.[/red]")
                return self._set_apply_outcome(ApplyOutcomeCode.SUBMIT_NOT_FOUND, "Submit button descriptor became ambiguous or detached.")
            
            await self._delay(3, 5)

            success_signal = False
            try:
                await page.wait_for_function(f\"\"\"(oldText) => {{
                    if (window.location.href !== "{portal_url}") return true;
                    const body = document.body.innerText.toLowerCase();
                    const newText = body.replace(oldText, '');
                    return newText.includes("application submitted") || 
                           newText.includes("success") || 
                           newText.includes("thank you for applying");
                }}\"\"\", arg=pre_click_text, timeout=5000)
                success_signal = True
            except Exception:
                success_signal = False
                
            if not success_signal:
                console.print("[yellow]Jobright: Clicked submit but no clear success signal was detected.[/yellow]")
                return self._set_apply_outcome(ApplyOutcomeCode.SUBMISSION_UNVERIFIED, "Clicked submit button but no new confirmation page or success message was detected.")

            # ── Record analytics for orchestrator to persist via extra_json ──
            from datetime import datetime
            import os
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
                    ApplyOutcomeCode.SUBMIT_NOT_FOUND, portal_family=family if family != "generic" else "",
                    detail=(
                        f"Auto-submit requested, but no submit button was found at "
                        f"{portal_url}. Visible controls: {self._format_controls_snapshot(controls)}"
                        + (f"\\nTailored resume ready: {tailored_hint}" if tailored_hint else "")
                    ),
                )
            console.print("[yellow]Click Submit in the browser window, then confirm below.[/yellow]")
            try:
                confirm_ans = input("  Press Enter after submitting (or to skip) > ")
                answer = input("  Did you successfully submit? [y/N] > ").strip().lower()
                return answer == "y"
            except (EOFError, KeyboardInterrupt):
                return False
"""

new_content = content[:start_idx] + new_func + content[end_idx:]

with open("src/sources/jobright.py", "w") as f:
    f.write(new_content)
