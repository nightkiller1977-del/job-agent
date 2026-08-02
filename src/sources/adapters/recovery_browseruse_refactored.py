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
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import Page

from src.model_client import ModelClient
from src.agent_config import get_config
from src.progress_tracker import ProgressTracker, LoopDetectionResult
from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult

logger = logging.getLogger("job-agent.adapters.recovery_browseruse")


class BrowserUseRecoveryRefactored(AtsAdapter):
    """Fallback LLM browser agent with integrated loop detection and progress tracking."""

    name = "browser_use_recovery_refactored"
    # Path is resolved relative to the project root (4 levels up from this file).
    SKILLS_FILE = Path(__file__).parent.parent.parent.parent / "state" / "browseruse_skills.json"

    def __init__(self):
        self.skills_dir = self.SKILLS_FILE.parent
        self.skills_dir.mkdir(exist_ok=True)
        self.mc = ModelClient()
        self.config = get_config()
        self.browser_config = self.config.browser_recovery

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Lowest priority routing (0.05) — only picked as explicit fallback
        return 0.05

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        logger.info(f"Initiating Browser Use recovery for job URL: {ctx.url}")
        domain = urllib.parse.urlparse(ctx.url).netloc.lower()

        # Initialize progress tracker with config
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

        # 1. Attempt to replay existing domain skills if recorded
        skills = self._load_skills(domain)
        if skills:
            logger.info(f"Replaying {len(skills)} recorded domain skills for {domain}...")
            success = await self._replay_skills(ctx.page, skills, ctx.resume_path)
            if success:
                logger.info(f"Domain skills replay succeeded for {domain}!")
                body_text = await ctx.page.locator("body").inner_text()
                if self._check_success_indicators(body_text):
                    return AtsApplyResult.ok(detail="Application submitted via domain skills replay.")
                return AtsApplyResult(
                    submitted=True,
                    status="review_ready",
                    detail="Domain skills completed, form ready for review."
                )

            logger.warning(f"Replay failed for {domain}, falling back to LLM agent loop.")

        # 2. Run LLM-guided browser agent loop with progress tracking
        steps_recorded = []
        step = 0

        while step < self.browser_config.max_steps:
            step += 1
            logger.info(f"Running LLM agent step {step}/{self.browser_config.max_steps}...")

            # Gather page state
            elements = await self._get_interactive_elements(ctx.page)
            body_text = await ctx.page.locator("body").inner_text()
            title = await ctx.page.title()

            # Record state for progress tracking
            progress_tracker.record_state(
                body_text=body_text,
                title=title,
                interactive_count=len(elements),
                form_fields_count=sum(1 for e in elements if e["tag"] in ("input", "select", "file_input")),
                has_submit=any(e["tag"] == "button" and "submit" in e.get("text", "").lower() for e in elements),
            )

            # Check success indicators
            if self._check_success_indicators(body_text):
                logger.info("Success screen detected in visible body text.")
                if steps_recorded:
                    self._save_skills(domain, steps_recorded)
                if self.config.telemetry.track_step_efficiency:
                    logger.info(f"Loop completed successfully in {step} steps")
                return AtsApplyResult.ok(detail="Application submitted successfully.")

            # Check for loops BEFORE exceeding max_steps
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
                    return AtsApplyResult.blocked(
                        status="form_not_reached",
                        detail=f"Loop detected: {loop_result.reason}. Agent not making progress toward submission."
                    )

            # Build improved LLM prompt with progress context
            state_desc = {
                "url": ctx.page.url,
                "title": title,
                "text_snippet": body_text[:self.browser_config.body_text_snippet_len],
                "interactive_elements": elements
            }

            # Get progress context for the LLM
            progress_context = progress_tracker.get_progress_context()

            # Build enhanced system and user prompts
            system_prompt = self._build_system_prompt(progress_context)
            user_prompt = self._build_user_prompt(state_desc, ctx, progress_context)

            messages = [{"role": "user", "content": user_prompt}]

            try:
                response = await self.mc.complete(
                    messages=messages,
                    system=system_prompt,
                    task_type=self.config.llm_prompting.model_task,
                )
                action_data = self._clean_json_response(response)
                logger.info(f"LLM Action decision: {json.dumps(action_data)}")
            except Exception as e:
                logger.error(f"Failed to get LLM action decision: {e}")
                return AtsApplyResult.blocked(status="external_ats_error", detail=f"LLM decision failure: {e}")

            action = action_data.get("action")
            selector = action_data.get("selector")
            val = action_data.get("value")

            if action == "done":
                logger.info("LLM declared form filling complete.")
                if steps_recorded:
                    self._save_skills(domain, steps_recorded)
                return AtsApplyResult(submitted=True, status="review_ready", detail="Form filled, ready for manual review.")

            elif action == "fail":
                logger.warning(f"LLM declared failure: {action_data.get('explanation')}")
                return AtsApplyResult.blocked(status="submit_not_found", detail=f"LLM failed: {action_data.get('explanation')}")

            # Execute selected action
            success = await self._execute_action(ctx.page, action, selector, val, ctx.resume_path)

            # Record action for progress tracking
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

        # Exceeded step limit without success
        logger.error(
            f"Browser Use recovery loop exceeded step limit {self.browser_config.max_steps} "
            f"without reaching submission"
        )
        if self.config.telemetry.track_loop_events:
            logger.error(
                f"loop_max_steps_exceeded domain={domain} max_steps={self.browser_config.max_steps} "
                f"progress={progress_tracker.last_progress_score:.2f}"
            )

        # Log summary for debugging
        summary = progress_tracker.get_summary()
        logger.error(f"Progress summary: {summary}")

        return AtsApplyResult.blocked(
            status="form_not_reached",
            detail=f"Browser Use recovery loop exceeded {self.browser_config.max_steps} steps. "
                   f"Progress: {progress_tracker.last_progress_score:.2f}/1.0. "
                   f"Agent may be stuck on complex form.",
        )

    def _check_success_indicators(self, body_text: str) -> bool:
        """Check if page contains success indicators from configuration."""
        lowered = body_text.lower()
        return any(indicator in lowered for indicator in self.browser_config.success_indicators)

    def _build_system_prompt(self, progress_context: dict) -> str:
        """Build system prompt with progress guidance and loop warnings."""
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
        """Build user prompt with detailed progress visualization."""
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

        # Add progress visualization
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
        """Parse JSON response from LLM. Raises json.JSONDecodeError on malformed output."""
        text_clean = text.strip()
        if text_clean.startswith("```json"):
            text_clean = text_clean[7:]
        elif text_clean.startswith("```"):
            text_clean = text_clean[3:]
        if text_clean.endswith("```"):
            text_clean = text_clean[:-3]
        return json.loads(text_clean.strip())

    async def _get_interactive_elements(self, page: Page) -> List[Dict[str, Any]]:
        """Extract interactive elements from page."""
        elements = []
        try:
            # Inputs
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

            # File inputs
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

            # Dropdowns
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

            # Buttons
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

    async def _execute_action(self, page: Page, action: str, selector: str, value: Any, resume_path: Optional[str]) -> bool:
        """Execute a single action on the page."""
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

    async def _replay_skills(self, page: Page, skills: List[Dict[str, Any]], resume_path: Optional[str]) -> bool:
        """Replay recorded domain skills."""
        delay_s = self.browser_config.skill_replay_delay_ms / 1000
        timeout_ms = self.browser_config.step_timeout_ms

        for idx, step in enumerate(skills):
            action = step.get("action")
            selector = step.get("selector")
            val = step.get("value")
            logger.info(f"Replaying skill step {idx + 1}: {action} on {selector}")

            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                success = await self._execute_action(page, action, selector, val, resume_path)
                if not success:
                    return False
                await asyncio.sleep(delay_s)
            except Exception as e:
                logger.error(f"Skill replay failed at step {idx + 1}: {e}")
                return False
        return True

    def _load_skills(self, domain: str) -> List[Dict[str, Any]]:
        """Load recorded domain skills if they exist."""
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
        """Save recorded domain skills for future reuse."""
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
