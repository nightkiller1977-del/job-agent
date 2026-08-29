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

    def get_answer_for_question(self, question_text: str, field_type: str = "text", job: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        """Fuzzy-matches question text and returns the corresponding profile answer.

        Supports text, boolean, numeric, and select dropdown answer mappings.
        """
        if not question_text:
            return None
        q = question_text.lower().strip()

        # 1. Work Authorization & Visa Sponsorship & Citizenship
        if self._matches_any(q, [r"authorized to work", r"legally authorized", r"legal right to work", r"eligible to work", r"work in the (us|united states)"]):
            val = self.disclosures.get("authorized_to_work")
            if val is None:
                val = self.disclosures.get("work_authorization")
            if val is None:
                return None
            return self._format_bool_or_select(val, field_type)

        if self._matches_any(q, [r"require.*sponsorship", r"visa sponsorship", r"require.*visa", r"sponsorship.*now or in the future", r"h-1b", r"sponsor your visa"]):
            val = self.disclosures.get("requires_sponsorship")
            if val is None:
                val = self.disclosures.get("sponsorship_required")
            if val is None:
                return None
            return self._format_bool_or_select(val, field_type)

        if self._matches_any(q, [r"u\.?s\.?\s*citizen", r"citizenship", r"permanent resident", r"green card"]):
            val = self.disclosures.get("us_citizen")
            if val is None:
                return None
            if field_type == "select" and not isinstance(val, bool):
                return str(val)
            return self._format_bool_or_select(val, field_type)

        # 2. Security Clearance
        if self._matches_any(q, [r"security clearance", r"clearance level", r"top secret", r"ts/sci", r"active clearance", r"government clearance"]):
            clearance = self.disclosures.get("security_clearance")
            if not clearance:
                return None
            if field_type == "boolean":
                return True
            if field_type == "select":
                # Match common dropdown options
                return self._format_disclosure(clearance, ["top secret", "secret", "ts/sci", "public trust", "yes"], field_type)
            return str(clearance)

        # 3. Compensation & Target Salary
        if self._matches_any(q, [r"desired salary", r"expected salary", r"target salary", r"compensation expectation", r"desired pay", r"salary expectation", r"minimum salary", r"base salary"]):
            salary = self.disclosures.get("desired_salary") or self.disclosures.get("target_salary")
            if not salary:
                return None
            if field_type == "number":
                nums = re.findall(r"\d+", str(salary).replace(",", ""))
                return int(nums[0]) if nums else None
            return str(salary)

        # 4. Notice Period & Start Date Availability
        if self._matches_any(q, [r"notice period", r"available to start", r"earliest start", r"how soon.*start", r"start date"]):
            notice = self.disclosures.get("notice_period") or self.disclosures.get("available_start_date")
            if not notice:
                return None
            if field_type == "number":
                nums = re.findall(r"\d+", str(notice))
                return int(nums[0]) if nums else None
            return str(notice)

        # 5. Location, Relocation & Remote Work Preferences
        if self._matches_any(q, [r"willing.*relocate", r"open to relocation", r"relocation"]):
            relocate = self.disclosures.get("willing_to_relocate")
            if relocate is None:
                return None
            return self._format_bool_or_select(relocate, field_type)

        if self._matches_any(q, [r"work arrangement", r"remote.*hybrid", r"preference.*remote", r"workplace type"]):
            pref = self.disclosures.get("work_arrangement")
            if not pref:
                return None
            if field_type == "select":
                return self._format_disclosure(pref, ["remote", "hybrid", "onsite"], field_type)
            return str(pref)

        if self._matches_any(q, [r"willing.*commute", r"commute to the office"]):
            commute = self.disclosures.get("willing_to_commute")
            if commute is None:
                return None
            return self._format_bool_or_select(commute, field_type)

        # 6. Education Level
        if self._matches_any(q, [r"highest.*education", r"highest degree", r"degree obtained", r"level of education"]):
            edu = self.disclosures.get("highest_education") or self.disclosures.get("education_level")
            if not edu:
                return None
            if field_type == "select":
                return self._format_disclosure(edu, ["master", "bachelor", "doctorate", "graduate"], field_type)
            return str(edu)

        # 7. Management, Leadership & Overall Experience Scope
        if self._matches_any(q, [r"years of.*(management|leadership|director|managing|supervis)", r"management experience"]):
            mgmt_yrs = self.disclosures.get("years_management_experience")
            if mgmt_yrs is None:
                return None
            return int(mgmt_yrs) if field_type == "number" else str(mgmt_yrs)

        if self._matches_any(q, [r"direct reports", r"team size", r"people managed", r"managed a team", r"largest team"]):
            team_sz = self.disclosures.get("max_team_size_managed")
            if team_sz is None:
                return None
            return int(team_sz) if field_type == "number" else str(team_sz)

        if self._matches_any(q, [r"budget.*managed", r"department budget", r"p&l"]):
            budget = self.disclosures.get("budget_managed")
            if not budget:
                return None
            if field_type == "number":
                nums = re.findall(r"\d+", str(budget).replace(",", ""))
                return int(nums[0]) if nums else None
            return str(budget)

        if self._matches_any(q, [r"total years of experience", r"overall experience", r"years of professional experience"]):
            tot_yrs = self.disclosures.get("total_years_experience")
            if tot_yrs is None:
                return None
            return int(tot_yrs) if field_type == "number" else str(tot_yrs)

        # 8. Legal, Consent & Background Checks
        if self._matches_any(q, [r"background check", r"background screening", r"consumer report"]):
            consent = self.disclosures.get("background_check_consent")
            if consent is None:
                return None
            return self._format_bool_or_select(consent, field_type)

        if self._matches_any(q, [r"drug screen", r"drug test"]):
            consent = self.disclosures.get("drug_test_consent")
            if consent is None:
                return None
            return self._format_bool_or_select(consent, field_type)

        if self._matches_any(q, [r"previously employed", r"previously worked", r"ever worked", r"worked for (us|this company)", r"former employee", r"worked at\b", r"prior employment with"]):
            prev = self.disclosures.get("previously_employed_at_company")
            if prev is None:
                return None
            return self._format_bool_or_select(prev, field_type)

        if self._matches_any(q, [r"relatives.*employed", r"family.*employed", r"conflict of interest"]):
            fam = self.disclosures.get("family_at_company")
            if fam is None:
                return None
            return self._format_bool_or_select(fam, field_type)

        if self._matches_any(q, [r"non-compete", r"restrictive covenant", r"non-disclosure"]):
            nc = self.disclosures.get("non_compete_agreement")
            if nc is None:
                return None
            return self._format_bool_or_select(nc, field_type)

        if self._matches_any(q, [r"criminal", r"felony", r"convicted", r"misdemeanor"]):
            crim = self.disclosures.get("criminal_record")
            if crim is None:
                return None
            return self._format_bool_or_select(crim, field_type)

        if self._matches_any(q, [r"driver.*license", r"valid driver"]):
            dl = self.disclosures.get("valid_driver_license")
            if dl is None:
                return None
            return self._format_bool_or_select(dl, field_type)

        if self._matches_any(q, [r"18 years of age", r"at least 18", r"legal age"]):
            over18 = self.disclosures.get("over_18")
            if over18 is None:
                return None
            return self._format_bool_or_select(over18, field_type)

        # 9. Contact & Social Links
        if self._matches_any(q, [r"linkedin"]):
            return self.social_links.get("linkedin")

        if self._matches_any(q, [r"github"]):
            return self.social_links.get("github")

        if self._matches_any(q, [r"portfolio", r"website", r"personal site"]):
            return self.social_links.get("portfolio") or self.social_links.get("website")

        # 10. Voluntary Disclosures (EEO)
        if self._matches_any(q, [r"\bgender\b", r"\bsex\b", r"please identify your gender"]):
            gender = self.disclosures.get("gender")
            if not gender:
                return None
            return self._format_disclosure(gender, ["male", "female", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"race", r"ethnicity", r"hispanic or latino"]):
            race = self.disclosures.get("race")
            if not race:
                return None
            return self._format_disclosure(race, ["white", "black", "hispanic", "asian", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"veteran", r"military service", r"discharge status"]):
            vet = self.disclosures.get("veteran")
            if not vet:
                return None
            return self._format_disclosure(vet, ["yes", "no", "decline", "prefer not to say"], field_type)

        if self._matches_any(q, [r"disability", r"disabled", r"physical or mental impairment"]):
            dis = self.disclosures.get("disability")
            if not dis:
                return None
            return self._format_disclosure(dis, ["yes", "no", "decline", "prefer not to say"], field_type)

        # 11. Years of Experience (Fuzzy skill/role matches)
        skill_match = re.search(r"years of experience (?:do you have )?(?:with |in )?([a-zA-Z0-9\+\#\-\.\s]+)", q)
        if skill_match:
            skill_name = skill_match.group(1).strip()
            return self._infer_years_of_experience(skill_name, field_type)

        # Record unanswered question to tracker
        try:
            from .unanswered_tracker import tracker
            tracker.record_unanswered(question_text, field_type=field_type, job=job)
        except Exception:
            pass

        return None

    def _matches_any(self, text: str, patterns: List[str]) -> bool:
        """Helper to match text against list of regex patterns."""
        return any(re.search(pat, text, re.IGNORECASE) for pat in patterns)

    def _format_bool_or_select(self, val: Any, field_type: str) -> Any:
        """Formatter for boolean indicators."""
        is_true = str(val).lower() in ("true", "yes", "1", "y")
        if field_type == "boolean":
            return is_true
        elif field_type == "select" or field_type == "text":
            return "Yes" if is_true else "No"
        return is_true

    def _format_disclosure(self, val: str, allowed_keywords: List[str], field_type: str) -> str:
        """Fuzzy match demographic options to fit target form options."""
        val_clean = str(val).lower().strip()

        for kw in allowed_keywords:
            if kw in val_clean:
                if kw == "decline" or kw == "prefer not to say":
                    return "Decline to Self-Identify"
                return kw.title()

        return "Decline to Self-Identify"

    def _infer_years_of_experience(self, skill: str, field_type: str) -> Any:
        """Infers years of experience for a skill from the profile data.

        Checks profile skills object or compiles overall industry years.
        """
        for s in self.skills:
            if isinstance(s, dict) and s.get("name", "").lower() == skill.lower():
                years = s.get("years")
                if years is not None:
                    return int(years) if field_type == "number" else str(years)
            elif isinstance(s, str) and s.lower() == skill.lower():
                return 5 if field_type == "number" else "5"

        # General industry fallback based on executive profile
        return 5 if field_type == "number" else "5"
