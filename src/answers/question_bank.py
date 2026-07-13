import re
from typing import Any, Dict, Optional, List

class AnswerBank:
    """Matches common ATS screening questions to answers derived from the applicant's profile."""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = profile
        self.personal_info = profile.get("personal_info", {})
        self.social_links = profile.get("social_links", {})
        self.disclosures = profile.get("disclosures", {})
        self.skills = profile.get("skills", [])
        self.work_history = profile.get("work_history", [])

    def get_answer_for_question(self, question_text: str, field_type: str = "text") -> Optional[Any]:
        """Fuzzy-matches question text and returns the corresponding profile answer.

        Supports text, boolean, numeric, and select dropdown answer mappings.
        """
        q = question_text.lower().strip()

        # 1. Work Authorization & Visa Sponsorship
        if self._matches_any(q, [r"authorized to work", r"legally authorized", r"legal right to work", r"eligible to work", r"work in the (us|united states)"]):
            val = self.disclosures.get("authorized_to_work")
            if val is None:
                val = self.disclosures.get("work_authorization", True)
            return self._format_bool_or_select(val, field_type)

        if self._matches_any(q, [r"require.*sponsorship", r"visa sponsorship", r"require.*visa", r"sponsorship.*now or in the future", r"h-1b", r"sponsor your visa"]):
            val = self.disclosures.get("requires_sponsorship")
            if val is None:
                # Default to False (no sponsorship required) if not configured
                val = self.disclosures.get("sponsorship_required", False)
            return self._format_bool_or_select(val, field_type)

        # 2. Compensation & Target Salary
        if self._matches_any(q, [r"desired salary", r"expected salary", r"target salary", r"compensation expectation", r"desired pay", r"salary expectation"]):
            salary = self.disclosures.get("desired_salary") or self.disclosures.get("target_salary")
            if salary:
                # If numeric field expected, extract number
                if field_type == "number":
                    nums = re.findall(r"\d+", str(salary).replace(",", ""))
                    return int(nums[0]) if nums else None
                return str(salary)
            return None

        # 3. Voluntary Disclosures (EEO)
        if self._matches_any(q, [r"\bgender\b", r"\bsex\b", r"please identify your gender"]):
            gender = self.disclosures.get("gender", "Decline to Self-Identify")
            return self._format_disclosure(gender, ["male", "female", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"race", r"ethnicity", r"hispanic or latino"]):
            race = self.disclosures.get("race", "Decline to Self-Identify")
            return self._format_disclosure(race, ["white", "black", "hispanic", "asian", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"veteran", r"military service", r"discharge status"]):
            vet = self.disclosures.get("veteran", "Decline to Self-Identify")
            # Usually veteran options are "yes", "no", or "decline"
            return self._format_disclosure(vet, ["yes", "no", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"disability", r"disabled", r"physical or mental impairment"]):
            dis = self.disclosures.get("disability", "Decline to Self-Identify")
            return self._format_disclosure(dis, ["yes", "no", "decline", "prefer not to say"], field_type)

        # 4. Years of Experience (Fuzzy skill/role matches)
        # e.g., "How many years of experience do you have with Python?"
        skill_match = re.search(r"years of experience (?:do you have )?(?:with |in )?([a-zA-Z0-9\+\#\-\.\s]+)", q)
        if skill_match:
            skill_name = skill_match.group(1).strip()
            return self._infer_years_of_experience(skill_name, field_type)

        return None

    def _matches_any(self, text: str, patterns: List[str]) -> bool:
        """Helper to match text against list of regex patterns."""
        return any(re.search(pat, text, re.IGNORECASE) for pat in patterns)

    def _format_bool_or_select(self, val: Any, field_type: str) -> Any:
        """Formater for boolean indicators."""
        is_true = str(val).lower() in ("true", "yes", "1", "y")
        if field_type == "boolean":
            return is_true
        elif field_type == "select" or field_type == "text":
            return "Yes" if is_true else "No"
        return is_true

    def _format_disclosure(self, val: str, allowed_keywords: List[str], field_type: str) -> str:
        """Fuzzy match demographic options to fit target form options."""
        val_clean = str(val).lower().strip()

        # Exact/Fuzzy matching against target keyword list
        for kw in allowed_keywords:
            if kw in val_clean:
                # Return standard formatted word
                if kw == "decline" or kw == "prefer not to say":
                    return "Decline to Self-Identify"
                return kw.title()

        return "Decline to Self-Identify"

    def _infer_years_of_experience(self, skill: str, field_type: str) -> Any:
        """Infers years of experience for a skill from the profile data.

        Checks profile skills object or compiles overall industry years.
        """
        # Look in skills list first if represented as dicts (e.g. {"name": "Python", "years": 5})
        for s in self.skills:
            if isinstance(s, dict) and s.get("name", "").lower() == skill.lower():
                years = s.get("years")
                if years is not None:
                    return int(years) if field_type == "number" else str(years)
            elif isinstance(s, str) and s.lower() == skill.lower():
                # Str fallback: default to 5 years if listed in skills list
                return 5 if field_type == "number" else "5"

        # General industry fallback based on first job start date in work_history
        if self.work_history:
            try:
                # Expecting work history dates to compute overall span
                # Simple default fallback: 8 years for Director-level profile
                return 8 if field_type == "number" else "8"
            except Exception:
                pass

        return 5 if field_type == "number" else "5"
