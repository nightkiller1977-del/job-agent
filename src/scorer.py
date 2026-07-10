"""
Job scorer — uses Ollama (local) first, Claude as fallback, to score jobs.
Returns a score 0-100, a reason string, and a flags string.

Model routing: resource-aware Ollama → Claude (Sonnet) → OpenAI, via ModelClient.
All tiers emit model_span() logs so Grafana shows which provider handled each call.

Profile and thresholds are read from config.json at init time so a single
config change propagates everywhere without restarting.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from src.model_client import ModelClient

# ---------------------------------------------------------------------------
# Default profile (used only when config is absent — never hardcoded in prod)
# ---------------------------------------------------------------------------
_DEFAULT_USER_PROFILE = """
Current role: Director of Software Engineering
Location: Miami, Florida. Open to: fully remote, Miami on-site, DC/DMV area.
US Citizen. Top Secret (TS) clearance. 18+ years experience.
"""

_DEFAULT_TARGET_ROLES = """
Director of Software Engineering, Director of IT, VP of IT,
VP of Software Engineering, AVP of Software Engineering, CTO, CIO,
Engineering Manager (managing managers preferred), Program Manager,
GS-15, SES (Senior Executive Service), SL (Senior Level).
"""

_DEFAULT_REJECT_ROLES = """
Software Engineer, Software Developer, Staff Engineer, Principal Engineer,
Data Engineer, DevOps Engineer, any individual contributor (IC) role.
"""

_DEFAULT_COMPENSATION_RULES = """
remote: minimum $180,000/year
miami_onsite: minimum $230,000/year
dc_area_onsite: minimum $260,000, maximum $320,000
other_onsite: ALWAYS SKIP
cleared_dod_remote: minimum $170,000/year
cleared_dod_hybrid: minimum $230,000/year
federal_gs15: ALWAYS APPLY regardless of salary
federal_ses: ALWAYS APPLY regardless of salary
federal_sl: ALWAYS APPLY regardless of salary
contract_remote: minimum $180,000/year
If salary not listed: FLAG FOR REVIEW (do not skip)
"""

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
SCORE_PROMPT_TEMPLATE = """
You are a job-fit scorer for a senior technology executive.

USER PROFILE:
{user_profile}

TARGET ROLES (must match one or be a close semantic match):
{target_roles}

ROLES TO REJECT (auto-reject if this is clearly an IC/non-management role):
{reject_roles}

COMPENSATION & LOCATION RULES:
{compensation_rules}

---

JOB LISTING TO SCORE:
Title: {title}
Company: {company}
Location: {location}
Remote/Hybrid/Onsite: {remote_type}
Salary: {salary}
Source: {source}
Description:
{description}

---

Evaluate this job and return a JSON object with EXACTLY these fields:
{{
  "score": <integer 0-100>,
  "reason": "<1-3 sentence explanation of the score>",
  "flags": "<comma-separated flags from: ALWAYS_APPLY, SKIP, FLAG_FOR_REVIEW, BELOW_THRESHOLD, ROLE_MISMATCH, CLEARED_ROLE, FEDERAL_ROLE, SALARY_MISSING, SALARY_BELOW_MIN, LOCATION_MISMATCH, IC_ROLE>",
  "recommended_action": "<one of: apply | skip | review>"
}}

Scoring guidance:
- 90-100: Perfect fit — right seniority, salary meets threshold, location acceptable
- 70-89: Strong fit — minor concerns
- 50-69: Moderate fit — needs review
- 30-49: Weak fit — significant concerns
- 0-29: Poor fit — wrong role type, IC role, far below compensation

For ALWAYS_APPLY (GS-15/SES/SL/federal), score should be 85+ unless clearly wrong.
For SKIP roles, score should be 0-30.

Return ONLY the JSON object, no other text.
"""

_CLAUDE_SONNET = "claude-sonnet-4-5"

# IC patterns — matched with word boundaries to avoid "SRE" / "Senior SRE"
# mismatches. "engineer(?:ing)?" / "develop(?:er|ment)" also catch the gerund
# field forms so "…, Software Engineering" / "…, Software Development" are treated
# the same as "Software Engineer" (e.g. "Technical Lead, Software Engineering").
_IC_PATTERNS = re.compile(
    r"\b("
    r"software\s+engineer(?:ing)?|software\s+develop(?:er|ment)|staff\s+engineer(?:ing)?|principal\s+engineer(?:ing)?"
    r"|data\s+engineer(?:ing)?|devops\s+engineer(?:ing)?|site\s+reliability\s+engineer(?:ing)?|sre"
    r"|machine\s+learning\s+engineer(?:ing)?|ml\s+engineer(?:ing)?|ai\s+engineer(?:ing)?"
    r"|frontend\s+engineer(?:ing)?|backend\s+engineer(?:ing)?|full[- ]?stack\s+engineer(?:ing)?"
    r")\b",
    re.IGNORECASE,
)

# Management/executive seniority terms that clear the quick IC rejection.
# NOTE: "lead" is deliberately NOT here — "Lead Software Engineer" / "Lead DevOps
# Engineer" are individual-contributor roles and must still hit the IC check.
# Genuine management "lead" titles (e.g. "Team Lead") carry no IC keyword, so
# they aren't quick-rejected and fall through to full model scoring anyway.
# "avp" covers the abbreviated form of a configured target role ("AVP of
# Software Engineering"); the spelled-out "assistant vice president" already
# matches via "vice president".
_SENIORITY_PATTERNS = re.compile(
    r"\b(manager|director|avp|vp|vice\s+president|head\s+of|gs-1[3-5])\b",
    re.IGNORECASE,
)


def _build_profile_from_config(config: dict) -> tuple[str, str, str, str]:
    """Extract user-profile text strings from structured config.json."""
    profile = config.get("user_profile", {})
    target = config.get("target_roles", [])
    reject = config.get("reject_roles", [])
    comp = config.get("compensation_thresholds", {})

    user_profile = _DEFAULT_USER_PROFILE
    if profile:
        parts = []
        if profile.get("current_title"):
            parts.append(f"Current role: {profile['current_title']}")
        if profile.get("years_experience"):
            parts.append(f"{profile['years_experience']} years experience")
        if profile.get("clearance"):
            parts.append(f"Security clearance: {profile['clearance']}")
        if profile.get("location"):
            parts.append(f"Location: {profile['location']}")
        relocation = profile.get("open_to_relocation", [])
        if relocation:
            parts.append(f"Open to: {', '.join(relocation)}")
        if profile.get("us_citizen"):
            parts.append("US Citizen")
        if parts:
            user_profile = "\n".join(parts)

    target_roles = ", ".join(target) if target else _DEFAULT_TARGET_ROLES
    reject_roles = ", ".join(reject) if reject else _DEFAULT_REJECT_ROLES

    comp_lines = []
    for key, val in comp.items():
        if isinstance(val, dict):
            if val.get("action") == "always_apply":
                comp_lines.append(f"{key}: ALWAYS APPLY")
            elif val.get("action") == "skip":
                comp_lines.append(f"{key}: ALWAYS SKIP")
            elif val.get("action") == "flag_for_review":
                comp_lines.append(f"{key}: FLAG FOR REVIEW")
            elif val.get("min_comp"):
                max_part = f", maximum ${val['max_comp']:,}" if val.get("max_comp") else ""
                comp_lines.append(f"{key}: minimum ${val['min_comp']:,}{max_part}")
    compensation_rules = "\n".join(comp_lines) if comp_lines else _DEFAULT_COMPENSATION_RULES

    return user_profile, target_roles, reject_roles, compensation_rules


def _smart_excerpt(text: str, max_chars: int = 2500) -> str:
    """Return up to max_chars from a job description.

    Strategy: first 2000 chars (intro + requirements) + last 500 chars
    (benefits/qualifications often trail at the end).  This gives better
    signal than naively slicing at 2500.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = 2000
    tail = max_chars - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


class JobScorer:
    def __init__(self, config: dict | None = None, api_key: Optional[str] = None):
        # Read profile from config so config.json changes propagate immediately
        cfg = config or {}
        self._user_profile, self._target_roles, self._reject_roles, self._comp_rules = (
            _build_profile_from_config(cfg)
        )
        _key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model_client = ModelClient(
            anthropic_api_key=_key,
            anthropic_model=_CLAUDE_SONNET,
        )

    async def score(self, job: dict) -> tuple[int, str, str, str]:
        """Score a single job via Ollama → Claude → OpenAI cascade.

        Returns (score, reason, flags, recommended_action).
        """
        ic_check = self._quick_ic_check(job)
        if ic_check:
            return 5, ic_check, "IC_ROLE,SKIP", "skip"

        prompt = SCORE_PROMPT_TEMPLATE.format(
            user_profile=self._user_profile,
            target_roles=self._target_roles,
            reject_roles=self._reject_roles,
            compensation_rules=self._comp_rules,
            title=job.get("title", ""),
            company=job.get("company", "Unknown"),
            location=job.get("location", "Unknown"),
            remote_type=job.get("remote_type", "Unknown"),
            salary=job.get("salary_raw", "Not listed"),
            source=job.get("source", ""),
            description=_smart_excerpt(job.get("description", "") or ""),
        )

        try:
            text = await self._model_client.complete(
                messages=[{"role": "user", "content": prompt}],
                task_type="reasoning",
                max_tokens=512,
            )
            if not text or text.startswith("No model available"):
                return 50, "No model available for scoring", "FLAG_FOR_REVIEW", "review"
            return self._parse_response(text)
        except Exception as exc:
            return 50, f"Scoring error: {exc}", "FLAG_FOR_REVIEW", "review"

    async def batch_score(self, jobs: list[dict], concurrency: int = 5) -> list[dict]:
        """Score jobs in parallel using a semaphore to cap concurrency.

        Default concurrency=5 balances Ollama load vs. throughput.
        Reduces 50-job batch time from ~100s to ~20s.
        """
        sem = asyncio.Semaphore(concurrency)

        async def _score_one(job: dict) -> dict:
            async with sem:
                score, reason, flags, action = await self.score(job)
                job["score"] = score
                job["score_reason"] = reason
                job["flags"] = flags
                job["recommended_action"] = action
                return job

        results = await asyncio.gather(*[_score_one(j) for j in jobs], return_exceptions=True)
        out = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                jobs[i]["score"] = 50
                jobs[i]["score_reason"] = f"Scoring failed: {result}"
                jobs[i]["flags"] = "FLAG_FOR_REVIEW"
                jobs[i]["recommended_action"] = "review"
                out.append(jobs[i])
            else:
                out.append(result)
        return out

    def _parse_response(self, raw: str) -> tuple[int, str, str, str]:
        """Parse a JSON scoring response into (score, reason, flags, action)."""
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)
        raw = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    return 50, "Could not parse model response", "FLAG_FOR_REVIEW", "review"
            else:
                return 50, "Could not parse model response", "FLAG_FOR_REVIEW", "review"
        score = max(0, min(100, int(data.get("score", 50))))
        return (
            score,
            data.get("reason", ""),
            data.get("flags", ""),
            data.get("recommended_action", "review"),
        )

    def _quick_ic_check(self, job: dict) -> Optional[str]:
        """Return rejection reason if the title is clearly an IC role, else None.

        A title is rejected only when it matches an IC pattern AND contains no
        management seniority term (manager/director/VP/head of/GS-13..15).

        Word-boundary regex handles:
        - "SRE" at end of string (not caught by the old "sre " trailing-space check)
        - "Senior SRE" — "senior" is NOT a management term, so this IS rejected as IC
        - "Engineering Manager" — has a seniority term, so NOT rejected
        """
        title = job.get("title") or ""
        title_lower = title.lower()

        if _SENIORITY_PATTERNS.search(title_lower):
            return None

        if _IC_PATTERNS.search(title_lower):
            return f"IC role rejected: '{title}'"

        return None
