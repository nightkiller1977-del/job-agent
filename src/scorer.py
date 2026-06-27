"""
Job scorer — uses Claude API to score each job against user criteria.
Returns a score 0-100, a reason string, and a flags string.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import anthropic
from src.telemetry import model_span

# ---------------------------------------------------------------------------
# User profile and criteria embedded directly (also read from config)
# ---------------------------------------------------------------------------

USER_PROFILE = """
Current role: Director of Software Engineering
Managing multiple engineering teams.
Delivering new EdTech product to market.
18 years experience in government/defense space.
Top Secret (TS) security clearance.
Location: Miami, Florida (no state income tax — important for compensation math).
Open to: fully remote, Miami on-site (230k+), DC/DMV area on-site (260k-320k+).
US Citizen. Has 1 year specialized experience at GS-14 equivalent level or above.
"""

TARGET_ROLES = """
- Director of Software Engineering
- Director of IT
- VP of IT / VP of Software Engineering
- AVP of Software Engineering
- CTO / CIO
- Engineering Manager (managing managers preferred)
- Program Manager (government/DoD context)
- GS-15 / SES (Senior Executive Service) / SL (Senior Level) — federal only
- Any DoD/cleared position matching the above seniority level
"""

REJECT_ROLES = """
- Software Engineer / Software Developer
- Staff Engineer / Principal Engineer (individual contributor)
- Data Engineer / DevOps Engineer (unless it's a Manager/Director of those)
- Any individual contributor (IC) role
"""

COMPENSATION_RULES = """
remote: minimum $180,000/year
miami_onsite: minimum $230,000/year
dc_area_onsite: minimum $260,000, maximum $320,000 (relocation from Miami, no state tax)
other_onsite: ALWAYS SKIP — not worth relocation
cleared_dod_remote: minimum $170,000
cleared_dod_hybrid: minimum $250,000
federal_gs15: ALWAYS APPLY regardless of salary
federal_ses: ALWAYS APPLY regardless of salary
federal_sl: ALWAYS APPLY regardless of salary
contract_remote: minimum $180,000/year
contract_hybrid: FLAG FOR REVIEW
If salary not listed AND cleared/DoD/federal: FLAG FOR REVIEW (do not skip)
If salary not listed AND commercial: FLAG FOR REVIEW (do not skip)
Never auto-skip solely due to missing salary.
"""

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
- 90-100: Perfect fit — right seniority, salary meets threshold, location acceptable, relevant domain
- 70-89: Strong fit — minor concerns (salary unclear, hybrid instead of remote, slightly different title)
- 50-69: Moderate fit — needs review (salary not listed for commercial, title adjacent, hybrid unclear)
- 30-49: Weak fit — significant concerns (salary below threshold, title lower than target)
- 0-29: Poor fit — wrong role type, IC role, wrong location with no exception, far below compensation

For ALWAYS_APPLY (GS-15/SES/SL/federal), score should be 85+ unless the role is clearly wrong.
For SKIP roles (wrong location, IC role, below threshold), score should be 0-30.

Return ONLY the JSON object, no other text.
"""


class JobScorer:
    def __init__(self, api_key: Optional[str] = None):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = "claude-sonnet-4-5"

    def score(self, job: dict) -> tuple[int, str, str, str]:
        """
        Score a single job.
        Returns (score, reason, flags, recommended_action).
        """
        prompt = SCORE_PROMPT_TEMPLATE.format(
            user_profile=USER_PROFILE,
            target_roles=TARGET_ROLES,
            reject_roles=REJECT_ROLES,
            compensation_rules=COMPENSATION_RULES,
            title=job.get("title", ""),
            company=job.get("company", "Unknown"),
            location=job.get("location", "Unknown"),
            remote_type=job.get("remote_type", "Unknown"),
            salary=job.get("salary_raw", "Not listed"),
            source=job.get("source", ""),
            description=(job.get("description", "") or "")[:3000],
        )

        # Fast pre-filter: obvious IC roles get score 0 without API call
        ic_check = self._quick_ic_check(job)
        if ic_check:
            return 5, ic_check, "IC_ROLE,SKIP", "skip"

        try:
            with model_span("anthropic", self.model, agent="job-agent/scorer") as span:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                span["input_tokens"] = message.usage.input_tokens
                span["output_tokens"] = message.usage.output_tokens
            raw = message.content[0].text.strip()
            # Strip markdown code fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            score = max(0, min(100, int(data.get("score", 50))))
            reason = data.get("reason", "")
            flags = data.get("flags", "")
            action = data.get("recommended_action", "review")
            return score, reason, flags, action
        except Exception as exc:
            return 50, f"Scoring error: {exc}", "FLAG_FOR_REVIEW", "review"

    def _quick_ic_check(self, job: dict) -> Optional[str]:
        """Returns a rejection reason if the job title clearly matches an IC role, else None."""
        title_lower = (job.get("title") or "").lower()
        ic_patterns = [
            "software engineer",
            "software developer",
            "staff engineer",
            "principal engineer",
            "data engineer",
            "devops engineer",
            "sre ",
            "site reliability",
            "machine learning engineer",
            "ml engineer",
            "frontend engineer",
            "backend engineer",
            "full stack engineer",
            "fullstack engineer",
        ]
        # But allow if "manager", "director", "vp", "lead" etc. also in title
        seniority_patterns = ["manager", "director", "vp", "vice president", "head of", "lead"]
        has_seniority = any(s in title_lower for s in seniority_patterns)
        if has_seniority:
            return None
        for pattern in ic_patterns:
            if pattern in title_lower:
                return f"IC role rejected: '{job.get('title')}'"
        return None

    def batch_score(self, jobs: list[dict]) -> list[dict]:
        """Score a list of jobs in place, adding score/score_reason/flags/recommended_action fields."""
        results = []
        for job in jobs:
            score, reason, flags, action = self.score(job)
            job["score"] = score
            job["score_reason"] = reason
            job["flags"] = flags
            job["recommended_action"] = action
            results.append(job)
        return results
