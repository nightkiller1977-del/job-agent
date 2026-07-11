import asyncio
import hashlib
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import Page

from src.model_client import ModelClient
from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult

logger = logging.getLogger("job-agent.adapters.recovery_browseruse")

class BrowserUseRecovery(AtsAdapter):
    """Fallback LLM browser agent to handle form-completion failures and unknown ATS interfaces.
    
    Acts as a self-healing layer, saving successfully resolved navigation/input steps
    to local 'domain skills' JSON to replay them deterministically on subsequent runs.
    """
    
    name = "browser_use_recovery"
    SKILLS_FILE = Path("state/browseruse_skills.json")

    def __init__(self):
        self.skills_dir = self.SKILLS_FILE.parent
        self.skills_dir.mkdir(exist_ok=True)
        self.mc = ModelClient()

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Lowest priority routing (0.05) so it is only picked as an explicit fallback
        # when specific vendor adapters and the Generic adapter fail or are not selected.
        return 0.05

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        logger.info(f"Initiating Browser Use recovery for job URL: {ctx.url}")
        domain = urllib.parse.urlparse(ctx.url).netloc.lower()
        
        # 1. Attempt to replay existing domain skills if recorded
        skills = self._load_skills(domain)
        if skills:
            logger.info(f"Replaying {len(skills)} recorded domain skills for {domain}...")
            success = await self._replay_skills(ctx.page, skills, ctx.resume_path)
            if success:
                logger.info(f"Domain skills replay succeeded for {domain}!")
                # Double-check page title or body to see if submitted
                body_text = await ctx.page.locator("body").inner_text()
                if any(w in body_text.lower() for w in ["thank you", "thanks", "thank", "received", "submitted", "success"]):
                    return AtsApplyResult.ok(detail="Application submitted via domain skills replay.")
                return AtsApplyResult(submitted=True, status="review_ready", detail="Domain skills completed, form ready for review.")

            logger.warning(f"Replay failed for {domain}, falling back to LLM agent loop.")

        # 2. Run LLM-guided browser agent loop (max 6 steps)
        steps_recorded = []
        max_steps = 6
        step = 0
        
        while step < max_steps:
            step += 1
            logger.info(f"Running LLM agent step {step}/{max_steps}...")
            
            # Analyze interactive elements and text
            elements = await self._get_interactive_elements(ctx.page)
            body_text = await ctx.page.locator("body").inner_text()
            title = await ctx.page.title()
            
            # Check success indicators
            if any(w in body_text.lower() for w in ["thank you", "thanks", "thank", "received", "submitted", "success"]):
                logger.info("Success screen detected in visible body text.")

                if steps_recorded:
                    self._save_skills(domain, steps_recorded)
                return AtsApplyResult.ok(detail="Application submitted successfully.")
                
            state_desc = {
                "url": ctx.page.url,
                "title": title,
                "text_snippet": body_text[:800],
                "interactive_elements": elements
            }
            
            # Build prompt
            system_prompt = (
                "You are an autonomous browser agent filling out a job application form.\n"
                "Your goal is to populate required fields, upload the resume, and progress or submit the form.\n"
                "Understand the page state and select the best action from the interactive elements provided.\n\n"
                "Respond ONLY with a valid JSON object matching the following structure:\n"
                "{\n"
                "  \"action\": \"fill\" | \"click\" | \"select\" | \"upload\" | \"done\" | \"fail\",\n"
                "  \"selector\": \"CSS selector targeting the element\",\n"
                "  \"value\": \"Value to enter or select options (if applicable)\",\n"
                "  \"explanation\": \"Short reason for this step\"\n"
                "}"
            )
            
            user_prompt = (
                f"Applicant Profile:\n{json.dumps(ctx.profile, indent=2)}\n\n"
                f"Page State:\n{json.dumps(state_desc, indent=2)}\n\n"
                f"Resume file path: {ctx.resume_path}\n"
                "Choose the next action."
            )
            
            messages = [{"role": "user", "content": user_prompt}]
            
            try:
                response = await self.mc.complete(messages=messages, system=system_prompt, task_type="general")
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
            if success:
                steps_recorded.append({
                    "action": action,
                    "selector": selector,
                    "value": val
                })
                # Settle brief timeout
                await asyncio.sleep(1.5)
            else:
                logger.warning(f"Failed to execute action {action} on selector {selector}.")
                
        return AtsApplyResult.blocked(status="form_not_reached", detail="Browser Use recovery loop exceeded step limit.")

    def _clean_json_response(self, text: str) -> dict:
        text_clean = text.strip()
        if text_clean.startswith("```json"):
            text_clean = text_clean[7:]
        elif text_clean.startswith("```"):
            text_clean = text_clean[3:]
        if text_clean.endswith("```"):
            text_clean = text_clean[:-3]
        return json.loads(text_clean.strip())

    async def _get_interactive_elements(self, page: Page) -> List[Dict[str, Any]]:
        elements = []
        try:
            # Inputs
            inputs = await page.query_selector_all('input:not([type="hidden"]):not([type="submit"]):not([type="file"])')
            for el in inputs[:20]: # Limit count to keep context window clean
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
            for el in file_inputs:
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
            for el in buttons[:15]:
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
        for idx, step in enumerate(skills):
            action = step.get("action")
            selector = step.get("selector")
            val = step.get("value")
            logger.info(f"Replaying skill step {idx + 1}: {action} on {selector}")
            
            try:
                # Wait briefly for selector to become visible before replay
                await page.wait_for_selector(selector, timeout=8000)
                success = await self._execute_action(page, action, selector, val, resume_path)
                if not success:
                    return False
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Skill replay failed at step {idx + 1}: {e}")
                return False
        return True

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
