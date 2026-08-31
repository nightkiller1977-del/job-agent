"""Grounded LLM Answer Generator for open-ended screening questions.

Provides factual, 2-3 sentence answers to open-ended ATS questions (e.g.
"Why are you interested in this role?", "Describe a difficult distributed system challenge")
strictly grounded in the applicant's verified work history from state/profile.json.

Key Guardrails:
1. Autonomous Blacklist: Immediately returns None for legal, compliance, sponsorship,
   clearance, salary, or EEO demographic fields (routed to unanswered tracker).
2. Fact Grounding: Ingests only verified work history bullets and skills.
3. Post-Generation Validator: Enforces <= 3 sentences, character limits, and verifies
   that mentioned entities/technologies exist in candidate data.
4. Normalized Caching: Deterministic answer caching in state/answer_cache.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Blacklist of questions that MUST NOT be autonomously answered by an LLM
_BLACKLIST_PATTERNS = [
    r"authorized to work",
    r"legally authorized",
    r"legal right to work",
    r"visa sponsorship",
    r"require.*sponsorship",
    r"require.*visa",
    r"u\.?s\.?\s*citizen",
    r"citizenship",
    r"permanent resident",
    r"security clearance",
    r"clearance level",
    r"top secret",
    r"desired salary",
    r"expected salary",
    r"target salary",
    r"compensation expectation",
    r"minimum salary",
    r"base salary",
    r"willing.*relocate",
    r"open to relocation",
    r"background check",
    r"drug screen",
    r"drug test",
    r"criminal",
    r"felony",
    r"convicted",
    r"misdemeanor",
    r"non-compete",
    r"\bgender\b",
    r"\bsex\b",
    r"race",
    r"ethnicity",
    r"hispanic or latino",
    r"veteran",
    r"military service",
    r"disability",
    r"disabled",
]

_CACHE_FILE = Path(__file__).resolve().parents[2] / "state" / "answer_cache.json"


def normalize_question(question: str) -> str:
    """Normalize question string for deterministic cache keying."""
    if not question:
        return ""
    q = re.sub(r"[^\w\s]", "", question.lower())
    return re.sub(r"\s+", " ", q).strip()


class LLMAnswerGenerator:
    """Grounded generator for open-ended screening questions."""

    def __init__(self, profile_data: Optional[Dict[str, Any]] = None, cache_path: Optional[Path] = None):
        self.profile = profile_data or self._load_default_profile()
        self.cache_path = cache_path or _CACHE_FILE
        self._cache = self._load_cache()

    def _load_default_profile(self) -> Dict[str, Any]:
        p_path = Path(__file__).resolve().parents[2] / "state" / "profile.json"
        if p_path.exists():
            try:
                with open(p_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_cache(self) -> Dict[str, str]:
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            logger.warning("Could not save answer cache: %s", exc)

    def is_blacklisted(self, question_text: str) -> bool:
        """Returns True if the question must NOT be answered by LLM."""
        if not question_text:
            return True
        q = question_text.lower().strip()
        return any(re.search(pat, q, re.IGNORECASE) for pat in _BLACKLIST_PATTERNS)

    def generate_answer(
        self,
        question_text: str,
        job: Optional[Dict[str, Any]] = None,
        max_chars: int = 500,
    ) -> Optional[str]:
        """Generate a grounded, validated answer for an open-ended screening question."""
        if not question_text or self.is_blacklisted(question_text):
            return None

        job_dict = job or {}
        job_key = job_dict.get("job_id") or job_dict.get("company", "") or "generic"
        norm_q = normalize_question(question_text)
        cache_key = hashlib.sha256(f"{norm_q}:{job_key}".encode("utf-8")).hexdigest()

        if cache_key in self._cache:
            return self._cache[cache_key]

        # Extract verified facts from candidate profile
        facts = self._extract_grounding_facts()
        if not facts:
            return None

        # Build local prompt
        prompt = self._build_prompt(question_text, facts, job_dict)
        raw_answer = self._call_model_cascade(prompt)
        if not raw_answer:
            return None

        validated_answer = self._validate_answer(raw_answer, facts, max_chars)
        if validated_answer:
            self._cache[cache_key] = validated_answer
            self._save_cache()
            return validated_answer

        return None

    def _extract_grounding_facts(self) -> List[str]:
        """Extract verified work history bullets and skills."""
        facts = []
        work_history = self.profile.get("work_history", [])
        for item in work_history:
            if isinstance(item, dict):
                company = item.get("company", "")
                role = item.get("role") or item.get("title", "")
                bullets = item.get("bullets", [])
                if role and company:
                    facts.append(f"Role: {role} at {company}")
                for b in bullets[:3]:
                    if b:
                        facts.append(f"- {b}")

        skills = self.profile.get("skills", [])
        if skills:
            skill_names = [s.get("name", s) if isinstance(s, dict) else str(s) for s in skills[:20]]
            facts.append(f"Core technical competencies: {', '.join(skill_names)}")

        return facts

    def _build_prompt(self, question: str, facts: List[str], job: Dict[str, Any]) -> str:
        job_title = job.get("title", "this role")
        company = job.get("company", "this company")
        facts_block = "\n".join(facts[:15])

        return (
            f"You are writing a professional screening answer for an application to {job_title} at {company}.\n\n"
            f"Candidate Verified Background:\n{facts_block}\n\n"
            f"Screening Question: {question}\n\n"
            "Instructions:\n"
            "1. Write a direct, professional answer in exactly 2-3 sentences.\n"
            "2. Ground your response STRICTLY in the Candidate Verified Background above.\n"
            "3. DO NOT invent employers, metrics, technologies, or accomplishments not listed.\n"
            "4. Do not include introductory filler like 'Sure!' or 'Here is the answer'. Return only the final text."
        )

    def _call_model_cascade(self, prompt: str) -> Optional[str]:
        """Dynamically routes through ModelClient / Ollama available models with fallback."""
        # 1. Try unified ModelClient (handles resource gates, local Ollama, Claude, OpenAI)
        try:
            from ..model_client import ModelClient
            import asyncio
            import concurrent.futures

            client = ModelClient()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(lambda: asyncio.run(client.complete([{"role": "user", "content": prompt}], task_type="general", max_tokens=150)))
                    resp = future.result(timeout=15)
            else:
                resp = asyncio.run(client.complete([{"role": "user", "content": prompt}], task_type="general", max_tokens=150))

            if resp and resp.strip():
                return resp.strip()
        except Exception as exc:
            logger.debug("ModelClient completion failed in LLMAnswerGenerator: %s", exc)

        # 2. Dynamic Ollama discovery fallback (no hardcoded model names)
        try:
            base_url = os.environ.get("OLLAMA_BASE_URL", os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")
            req = urllib.request.Request(f"{base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=3) as r:
                tags_data = json.loads(r.read().decode("utf-8"))
                installed_models = [m.get("name") for m in tags_data.get("models", []) if m.get("name")]

            for model in installed_models:
                ans = self._call_ollama(model, prompt)
                if ans:
                    return ans
        except Exception as exc:
            logger.debug("Dynamic Ollama discovery failed in LLMAnswerGenerator: %s", exc)

        return None

    def _call_ollama(self, model: str, prompt: str) -> Optional[str]:
        try:
            url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/api/generate"
            payload = json.dumps({
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 120},
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = data.get("response", "").strip()
                    if text:
                        return text
        except Exception:
            return None
        return None

    def _validate_answer(self, text: str, facts: List[str], max_chars: int) -> Optional[str]:
        """Post-generation validator enforcing <=3 sentences, character limits, and fact/metric consistency."""
        clean = text.strip().strip('"').strip("'")
        if not clean:
            return None

        # Check sentence limit (strictly 1 to 3 sentences)
        sentences = [s.strip() for s in re.split(r"[.!?]+", clean) if s.strip()]
        if len(sentences) > 3 or len(sentences) == 0:
            return None

        if len(clean) > max_chars:
            clean = clean[:max_chars].rsplit(" ", 1)[0] + "."

        # Grounding & anti-hallucination validation against supplied facts
        facts_text = " ".join(facts).lower()

        # 1. Metric / Number grounding: any specific numbers (>3 digits, percentages, dollar amounts) must exist in facts
        metric_matches = re.findall(r"(?:\$\d+(?:\.\d+)?[kmbKMB]?|\b\d+%\b|\b\d{2,}\b)", clean)
        for m in metric_matches:
            if m.lower() not in facts_text:
                logger.warning("Rejecting answer due to unsourced metric/number hallucination: %s", m)
                return None

        # 2. Company / Entity grounding: check capitalised proper nouns that look like employers
        # (Ignore sentence starters and common generic words)
        words = re.findall(r"\b[A-Z][a-zA-Z0-9]+\b", clean)
        common_words = {
            "I", "At", "In", "As", "My", "The", "Our", "We", "With", "For", "By", "To", "On", "This",
            "Software", "Engineering", "Platform", "Cloud", "Distributed", "Director", "Manager",
            "Lead", "Senior", "Principal", "Architect", "Executive", "VP", "CTO", "Tech", "Systems"
        }
        for w in words:
            if w not in common_words and w.lower() not in facts_text:
                # If a proper noun entity is not anywhere in the facts, reject
                logger.warning("Rejecting answer due to unsourced entity hallucination: %s", w)
                return None

        return clean
