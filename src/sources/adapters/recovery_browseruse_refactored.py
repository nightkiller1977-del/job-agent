"""
recovery_browseruse_refactored.py — LLM-guided browser agent with loop detection and progress tracking.

Key improvements over the original:
1. Uses configurable parameters (from agent_config) - NO HARD-CODED THRESHOLDS
2. Integrates progress_tracker to detect loops before step limit
3. Better LLM prompts with explicit progress guidance and loop warnings
4. Tracks progress metrics and detects when agent is genuinely making progress
5. Provides context to LLM about what constitutes progress vs repetition
6. Telemetry for loop events and step efficiency
"""
import asyncio
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import Page

from src.model_client import ModelClient
from src.agent_config import get_config
from src.progress_tracker import ProgressTracker
from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult
from .receipt import ReceiptEvidence, capture_receipt_evidence, verify_receipt

logger = logging.getLogger("job-agent.adapters.recovery_browseruse")


class BrowserUseRecoveryRefactored(AtsAdapter):
    """Fallback LLM browser agent with integrated loop detection and progress tracking."""

    name = "browser_use_recovery_refactored"
    SKILLS_FILE = Path(__file__).parent.parent.parent.parent / "state" / "browseruse_skills.json"

    def __init__(self):
        self.skills_dir = self.SKILLS_FILE.parent
        self.skills_dir.mkdir(exist_ok=True)
        self.mc = ModelClient()
        self.config = get_config()
        self.browser_config = self.config.browser_recovery

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        return 0.05

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        if not self.browser_config.enabled:
            return AtsApplyResult.blocked(
                status="form_not_reached",
                detail="Browser Use recovery is disabled via configuration.",
            )

        policy = getattr(ctx, "policy", None)
        allow_submit = False
        if policy is not None:
            try:
                allow_submit = bool(await policy.confirm_submit(ctx, {"via": "browser_use_recovery_refactored"}))
            except Exception as e:
                logger.warning(f"policy.confirm_submit raised, failing closed: {e}")
        if not allow_submit:
            logger.info("BrowserUse recovery withheld: submission not authorized by policy.")
            return AtsApplyResult.blocked(
                status="submit_denied_by_policy",
                detail="BrowserUse recovery withheld: submission not authorized by policy.",
            )

        logger.info(f"Initiating Browser Use recovery for job URL: {ctx.url}")
        domain = urllib.parse.urlparse(ctx.url).netloc.lower()

        pm = self.browser_config.progress_metrics
        progress_tracker = ProgressTracker(
            max_repeated_states=self.browser_config.loop_detection.max_repeated_states,
            max_repeated_actions=self.browser_config.loop_detection.max_repeated_actions,
            state_hash_window=self.browser_config.loop_detection.state_hash_window,
            min_progress_threshold=pm.min_progress_threshold,
            recent_action_window=pm.recent_action_window,
            top_selectors_count=pm.top_selectors_count,
            state_change_target_ratio=pm.state_change_target_ratio,
        )

        # Replay recorded skills first. Receipt evidence is only eligible after a
        # submit-like action may have dispatched, and is compared with the exact
        # snapshot captured immediately before that action.
        skills = self._load_skills(domain)
        if skills:
            logger.info(f"Replaying {len(skills)} recorded domain skills for {domain}...")
            success, replay_possible_submit, replay_baseline = await self._replay_skills(
                ctx.page, skills, ctx.resume_path
            )
            if success:
                logger.info(f"Domain skills replay succeeded for {domain}!")
                if replay_possible_submit and replay_baseline is not None:
                    verified, signal = await verify_receipt(ctx.page, baseline=replay_baseline)
                    if verified:
                        return AtsApplyResult.ok(
                            detail=f"Application submitted via domain skills replay (receipt {signal})."
                        )
                    return AtsApplyResult.unverified(
                        detail="Domain skills replay clicked a submit control but no fresh receipt was confirmed."
                    )
                return AtsApplyResult.blocked(
                    status="review_ready",
                    detail="Domain skills completed, form ready for review (not confirmed submitted).",
                )

            if replay_possible_submit:
                return AtsApplyResult.unverified(
                    detail="Skill replay failed after clicking a submit control; not re-driving the form."
                )
            logger.warning(f"Replay failed for {domain}, falling back to LLM agent loop.")

        steps_recorded = []
        step = 0
        possible_submit = False
        receipt_baseline: ReceiptEvidence | None = None

        while step < self.browser_config.max_steps:
            step += 1
            logger.info(f"Running LLM agent step {step}/{self.browser_config.max_steps}...")

            elements = await self._get_interactive_elements(ctx.page)
            body_text = await ctx.page.locator("body").inner_text()
            title = await ctx.page.title()
            page_snapshot = body_text + "|inputs:" + await self._get_input_values_snapshot(ctx.page)

            progress_tracker.record_state(
                body_text=page_snapshot,
                title=title,
                interactive_count=len(elements),
                form_fields_count=sum(1 for e in elements if e["tag"] in ("input", "select", "file_input")),
                has_submit=any(e["tag"] == "button" and "submit" in e.get("text", "").lower() for e in elements),
            )

            # Never trust a receipt that predates this recovery attempt's submit.
            # Before possible_submit becomes true, a confirmation-looking banner is
            # merely page state. After dispatch, only evidence fresh relative to the
            # pre-submit snapshot can complete the application.
            if possible_submit and receipt_baseline is not None:
                verified, signal = await verify_receipt(ctx.page, baseline=receipt_baseline)
                if verified:
                    logger.info(f"Fresh receipt verified at step {step}: {signal}")
                    if steps_recorded:
                        self._save_skills(domain, steps_recorded)
                    if self.config.telemetry.track_step_efficiency:
                        logger.info(f"Loop completed successfully in {step} steps")
                    return AtsApplyResult.ok(detail=f"Application submitted (receipt {signal}).")

            if self.browser_config.loop_detection.enabled:
                loop_result = progress_tracker.detect_loop()
                if loop_result.is_looping:
                    logger.warning(
                        f"Loop detected at step {step}: {loop_result.reason} "
                        f"(confidence={loop_result.confidence:.2f})"
                    )
                    if self.config.telemetry.track_loop_events:
                        logger.warning(
                            f"loop_detected domain={domain} step={step} reason={loop_result.reason}"
                        )
                    if possible_submit:
                        return AtsApplyResult.unverified(
                            detail=f"Loop detected after a submit-like click ({loop_result.reason}); submission unconfirmed."
                        )
                    return AtsApplyResult.blocked(
                        status="form_not_reached",
                        detail=f"Loop detected: {loop_result.reason}. Agent not making progress toward submission."
                    )

            state_desc = {
                "url": ctx.page.url,
                "title": title,
                "text_snippet": body_text[:self.browser_config.body_text_snippet_len],
                "interactive_elements": elements
            }

            progress_context = progress_tracker.get_progress_context()
            system_prompt = self._build_system_prompt(progress_context)
            user_prompt = self._build_user_prompt(state_desc, ctx, progress_context)
            messages = [{"role": "user", "content": user_prompt}]

            try:
                response = await self.mc.complete(
                    messages=messages,
                    system=system_prompt,
                    task_type=self.config.llm_prompting.model_task,
                    temperature=self.config.llm_prompting.temperature,
                )
                action_data = self._clean_json_response(response)
                logger.info(f"LLM Action decision: {json.dumps(action_data)}")
            except Exception as e:
                logger.error(f"Failed to get LLM action decision: {e}")
                if possible_submit:
                    return AtsApplyResult.unverified(
                        detail=f"LLM decision failure after a submit-like click: {e}"
                    )
                return AtsApplyResult.blocked(status="external_ats_error", detail=f"LLM decision failure: {e}")

            action = action_data.get("action")
            selector = action_data.get("selector")
            val = action_data.get("value")

            if action == "done":
                logger.info("LLM declared form filling complete.")
                if steps_recorded:
                    self._save_skills(domain, steps_recorded)
                if possible_submit and receipt_baseline is not None:
                    verified, signal = await verify_receipt(ctx.page, baseline=receipt_baseline)
                    if verified:
                        return AtsApplyResult.ok(detail=f"Application submitted (receipt {signal}).")
                    return AtsApplyResult.unverified(
                        detail="LLM declared done after a submit-like click, but no fresh receipt was confirmed."
                    )
                return AtsApplyResult.blocked(
                    status="review_ready",
                    detail="Form filled by LLM agent, ready for manual review (not confirmed submitted).",
                )

            elif action == "fail":
                logger.warning(f"LLM declared failure: {action_data.get('explanation')}")
                if possible_submit:
                    return AtsApplyResult.unverified(
                        detail=f"LLM declared failure after a submit-like click: {action_data.get('explanation')}"
                    )
                return AtsApplyResult.blocked(status="submit_not_found", detail=f"LLM failed: {action_data.get('explanation')}")

            # Capture immediately before every action. If _guarded_execute reports
            # that this action may have submitted, this exact snapshot becomes the
            # immutable attempt baseline. Non-submit snapshots are discarded.
            pre_action_baseline = await capture_receipt_evidence(ctx.page)
            success, may_submit, fenced = await self._guarded_execute(
                ctx.page, action, selector, val, ctx.resume_path,
                fence_active=possible_submit,
            )
            if fenced:
                logger.warning(
                    f"Submission fence: refusing to dispatch a second submit-like "
                    f"click ({selector}) while a prior submit is unconfirmed."
                )
                return AtsApplyResult.unverified(
                    detail="A further submit-like action was requested while a prior "
                           "submit was unconfirmed; stopped instead of re-submitting."
                )
            if may_submit:
                if not possible_submit:
                    receipt_baseline = pre_action_baseline
                possible_submit = True

            progress_tracker.record_action(
                step=step,
                action=action,
                selector=selector,
                value=val,
                success=success,
            )

            if success:
                steps_recorded.append({
                    "action": action,
                    "selector": selector,
                    "value": val
                })
                await asyncio.sleep(self.browser_config.post_action_delay_ms / 1000)
            else:
                logger.warning(f"Failed to execute action {action} on selector {selector}.")

        logger.error(
            f"Browser Use recovery loop exceeded step limit {self.browser_config.max_steps} "
            f"without reaching submission"
        )
        if self.config.telemetry.track_loop_events:
            logger.error(
                f"loop_max_steps_exceeded domain={domain} max_steps={self.browser_config.max_steps} "
                f"progress={progress_tracker.last_progress_score:.2f}"
            )

        summary = progress_tracker.get_summary()
        logger.error(f"Progress summary: {summary}")

        if possible_submit:
            return AtsApplyResult.unverified(
                detail=f"Step limit reached after a submit-like click; submission unconfirmed "
                       f"(progress {progress_tracker.last_progress_score:.2f}/1.0)."
            )
        return AtsApplyResult.blocked(
            status="form_not_reached",
            detail=f"Browser Use recovery loop exceeded {self.browser_config.max_steps} steps. "
                   f"Progress: {progress_tracker.last_progress_score:.2f}/1.0. "
                   f"Agent may be stuck on complex form.",
        )

    def _check_success_indicators(self, body_text: str) -> bool:
        lowered = body_text.lower()
        return any(indicator in lowered for indicator in self.browser_config.success_indicators)

    async def _get_input_values_snapshot(self, page: Page) -> str:
        try:
            values = await page.evaluate(
                """() => {
                    const els = document.querySelectorAll(
                        'input:not([type=hidden]):not([type=file]), textarea, select'
                    );
                    return Array.from(els)
                        .map(el => (el.name || el.id || '') + ':' + (el.value || ''))
                        .join('|');
                }"""
            )
            return values or ""
        except Exception as e:
            logger.debug(f"Could not snapshot input values: {e}")
            return ""

    def _build_system_prompt(self, progress_context: dict) -> str:
        return f"""You are an autonomous browser agent filling out a job application form.
Your goal is to populate required fields, upload the resume, and progress or submit the form.
Understand the page state and select the best action from the interactive elements provided.

CRITICAL GUARDRAILS TO AVOID LOOPS:
- You have taken {progress_context['total_steps']} steps so far
- Success rate: {progress_context['success_rate']:.1%}
- Progress score: {progress_context['progress_score']:.2f}/1.0

DO NOT repeat the same action on the same selector. Each step must show visible progress:
- Filling a new field
- Selecting a different option
- Navigating to a new page
- Entering different data

If you've attempted the same field multiple times, try a different approach or declare "fail".

Respond ONLY with valid JSON matching this structure:
{{
  "action": "fill" | "click" | "select" | "upload" | "done" | "fail",
  "selector": "CSS selector targeting the element",
  "value": "Value to enter or select options (if applicable)",
  "explanation": "Why this step will make progress (not just repeat prior attempts)"
}}"""

    def _build_user_prompt(
        self,
        state_desc: dict,
        ctx: AtsApplyContext,
        progress_context: dict,
    ) -> str:
        recent_actions = progress_context.get("action_history_recent", [])
        most_used = progress_context.get("most_used_selectors", [])

        prompt_parts = [
            "Applicant Profile:",
            json.dumps(ctx.profile, indent=2),
            "",
            "Page State:",
            json.dumps(state_desc, indent=2),
            "",
            "Resume file path:",
            str(ctx.resume_path),
            "",
        ]

        if recent_actions:
            window = self.browser_config.progress_metrics.recent_action_window
            prompt_parts.append(f"Recent Actions (last {window} steps):")
            for action in recent_actions:
                status = "✓" if action["success"] else "✗"
                prompt_parts.append(f"  {status} Step {action['step']}: {action['action']}")
            prompt_parts.append("")

        if most_used:
            prompt_parts.append("Selectors Used Multiple Times (watch for loops):")
            for item in most_used:
                prompt_parts.append(f"  {item['selector']}: {item['attempts']} attempts")
            prompt_parts.append("")

        prompt_parts.extend([
            f"Progress Score: {progress_context['progress_score']:.2f}/1.0",
            f"  (higher = making progress; lower = repeating same state)",
            "",
            "Choose the next action. Prioritize:",
            "1. Fields not yet attempted",
            "2. Required fields (marked with *)",
            "3. Submit button (if all required fields filled)",
            "4. 'done' if form is ready for review",
            "5. 'fail' if the form cannot be progressed",
        ])

        return "\n".join(prompt_parts)

    def _clean_json_response(self, text: str) -> dict:
        from src.json_utils import clean_model_json
        return clean_model_json(text)

    async def _get_interactive_elements(self, page: Page) -> List[Dict[str, Any]]:
        elements = []
        try:
            inputs = await page.query_selector_all('input:not([type="hidden"]):not([type="submit"]):not([type="file"])')
            for el in inputs[:self.browser_config.max_input_elements]:
                name = await el.get_attribute("name") or ""
                id_val = await el.get_attribute("id") or ""
                placeholder = await el.get_attribute("placeholder") or ""
                type_val = await el.get_attribute("type") or "text"
                aria_label = await el.get_attribute("aria-label") or ""

                selector = "input"
                if id_val:
                    selector += f"#{id_val}"
                elif name:
                    selector += f"[name='{name}']"

                elements.append({
                    "tag": "input",
                    "type": type_val,
                    "name": name,
                    "id": id_val,
                    "placeholder": placeholder,
                    "aria_label": aria_label,
                    "selector": selector
                })

            file_inputs = await page.query_selector_all('input[type="file"]')
            for el in file_inputs[:self.browser_config.max_file_input_elements]:
                name = await el.get_attribute("name") or ""
                id_val = await el.get_attribute("id") or ""
                selector = "input[type='file']"
                if id_val:
                    selector += f"#{id_val}"
                elif name:
                    selector += f"[name='{name}']"
                elements.append({
                    "tag": "file_input",
                    "name": name,
                    "id": id_val,
                    "selector": selector
                })

            selects = await page.query_selector_all("select")
            for el in selects:
                name = await el.get_attribute("name") or ""
                id_val = await el.get_attribute("id") or ""
                selector = "select"
                if id_val:
                    selector += f"#{id_val}"
                elif name:
                    selector += f"[name='{name}']"
                elements.append({
                    "tag": "select",
                    "name": name,
                    "id": id_val,
                    "selector": selector
                })

            buttons = await page.query_selector_all('button, input[type="submit"], [role="button"]')
            for el in buttons[:self.browser_config.max_button_elements]:
                text = (await el.inner_text() or "").strip()
                type_val = await el.get_attribute("type") or ""
                id_val = await el.get_attribute("id") or ""

                selector = "button"
                if id_val:
                    selector += f"#{id_val}"
                elif text:
                    selector = f"button:has-text('{text}')"

                if text or id_val or type_val:
                    elements.append({
                        "tag": "button",
                        "text": text,
                        "type": type_val,
                        "id": id_val,
                        "selector": selector
                    })
        except Exception as e:
            logger.error(f"Error extracting interactive elements: {e}")

        return elements

    @staticmethod
    def _is_submit_like(action: str, selector: str, value: Any = None) -> bool:
        if action != "click":
            return False
        text = f"{selector or ''} {value or ''}".lower()
        return bool(re.search(r"submit|apply|send[_\- ]?application", text))

    _PROBE_JS = """(sel) => {
        const el = document.querySelector(sel);
        if (!el) return "absent";
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute("type") || "").toLowerCase();
        const inForm = !!el.closest("form");
        if (tag === "button") return (type ? type === "submit" : inForm) ? "submit" : "other";
        if (tag === "input") return (type === "submit" || type === "image") ? "submit" : "other";
        return "other";
    }"""

    async def _probe_click_target(self, page: Page, selector: str) -> str:
        try:
            res = await page.evaluate(self._PROBE_JS, selector)
            return res if res in ("absent", "submit", "other") else "unknown"
        except Exception:
            return "unknown"

    async def _guarded_execute(
        self, page: Page, action: str, selector: str, value: Any,
        resume_path: Optional[str], fence_active: bool = False,
    ) -> tuple[bool, bool, bool]:
        if action != "click":
            return await self._execute_action(page, action, selector, value, resume_path), False, False

        probe = await self._probe_click_target(page, selector)
        submit_like = self._is_submit_like(action, selector, value) or probe == "submit"
        if not submit_like:
            return await self._execute_action(page, action, selector, value, resume_path), False, False

        if fence_active:
            return False, False, True

        success = await self._execute_action(page, action, selector, value, resume_path)
        if probe == "absent" and not success:
            return False, False, False
        return success, True, False

    async def _execute_action(self, page: Page, action: str, selector: str, value: Any, resume_path: Optional[str]) -> bool:
        try:
            if action == "fill":
                await page.fill(selector, str(value))
                return True
            elif action == "click":
                await page.click(selector)
                return True
            elif action == "select":
                await page.select_option(selector, str(value))
                return True
            elif action == "upload":
                if resume_path:
                    async with page.expect_file_chooser() as fc_info:
                        await page.click(selector)
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(resume_path)
                    return True
                return False
        except Exception as e:
            logger.error(f"Action execution failed on {selector} ({action}): {e}")

        return False

    async def _replay_skills(
        self, page: Page, skills: List[Dict[str, Any]], resume_path: Optional[str]
    ) -> tuple[bool, bool, Optional[ReceiptEvidence]]:
        """Replay recorded domain skills with attempt-scoped receipt evidence.

        Returns (success, possible_submit, receipt_baseline). The baseline is the
        immutable snapshot captured immediately before the first action that may
        have submitted. A second submit-like action remains fenced.
        """
        delay_s = self.browser_config.skill_replay_delay_ms / 1000
        timeout_ms = self.browser_config.step_timeout_ms
        possible_submit = False
        receipt_baseline: ReceiptEvidence | None = None

        for idx, step in enumerate(skills):
            action = step.get("action")
            selector = step.get("selector")
            val = step.get("value")
            logger.info(f"Replaying skill step {idx + 1}: {action} on {selector}")

            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
            except Exception as e:
                logger.error(f"Skill replay failed waiting for step {idx + 1}: {e}")
                return False, possible_submit, receipt_baseline

            try:
                pre_action_baseline = await capture_receipt_evidence(page)
                success, may_submit, fenced = await self._guarded_execute(
                    page, action, selector, val, resume_path,
                    fence_active=possible_submit,
                )
                if fenced:
                    logger.warning(
                        f"Submission fence: skill step {idx + 1} ({selector}) is a "
                        f"second submit-like action after an unresolved submit — stopping replay."
                    )
                    return False, True, receipt_baseline
                if may_submit:
                    if not possible_submit:
                        receipt_baseline = pre_action_baseline
                    possible_submit = True
                if not success:
                    return False, possible_submit, receipt_baseline
                await asyncio.sleep(delay_s)
            except Exception as e:
                logger.error(f"Skill replay failed at step {idx + 1}: {e}")
                return False, possible_submit, receipt_baseline
        return True, possible_submit, receipt_baseline

    def _load_skills(self, domain: str) -> List[Dict[str, Any]]:
        if not self.SKILLS_FILE.exists():
            return []
        try:
            with open(self.SKILLS_FILE, "r") as f:
                data = json.load(f)
            return data.get(domain, [])
        except Exception as e:
            logger.error(f"Failed to load skills JSON: {e}")
            return []

    def _save_skills(self, domain: str, steps: List[Dict[str, Any]]):
        data = {}
        if self.SKILLS_FILE.exists():
            try:
                with open(self.SKILLS_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                pass

        data[domain] = steps
        try:
            with open(self.SKILLS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Successfully saved {len(steps)} domain skills for {domain}.")
        except Exception as e:
            logger.error(f"Failed to save skills JSON: {e}")
