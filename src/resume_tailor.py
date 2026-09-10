"""
Resume tailoring gate: start from a baseline resume, score it against a job,
iteratively tailor it toward the job description, and only allow the apply
flow to proceed once the match score clears a threshold (default 90).

Design (mirrors existing repo patterns):
  - Model calls go through ModelClient (Ollama → OpenRouter → Claude → OpenAI
    cascade) exactly like src/scorer.py, with strict-JSON prompts parsed via
    src/json_utils.extract_json.
  - Rendering reuses the repo's existing PDF engine — Playwright's Chromium
    print pipeline (the same approach as JobrightScraper._generate_tailored_resume_pdf
    and LaTeXCompiler's HTML fallback) — so no new dependencies are needed and
    the output always has a real text layer (passes resume_helper.check_ats_readability).
  - Artifacts persist under state/resumes/<job_id>.{md,pdf,json}; the sidecar
    JSON records score, subscores and a baseline hash used for cache
    invalidation.

HARD INTEGRITY CONSTRAINT: tailoring may only rephrase, reorder, select and
emphasize content already present in the baseline resume. It must never invent
employers, titles, dates, degrees, certifications, clearances, or skills. This
is enforced twice: in the tailoring prompt AND in a post-hoc verification pass
that extracts the tailored resume's claimed facts and diffs them against the
baseline text (see check_facts_against_baseline).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console

from src.json_utils import extract_json
from src.model_client import ModelClient
from src.resume_helper import resolve_resume_path

console = Console()
_log = logging.getLogger(__name__)

# Filename of the test fixture that must NEVER be uploaded to a real employer.
DUMMY_RESUME_FILENAME = "dummy_resume.pdf"

DEFAULT_MIN_SCORE = 90
DEFAULT_MAX_ITERATIONS = 3
DEFAULT_OUTPUT_DIR = os.path.join("state", "resumes")

# Statuses recorded via StateManager.record_apply_attempt (surface in the
# dashboard through extra_json.apply_last_status).
STATUS_NEEDS_RESUME_REVIEW = "needs_resume_review"
STATUS_DUMMY_RESUME_BLOCKED = "dummy_resume_blocked"
STATUS_TAILOR_ERROR = "resume_tailor_error"
STATUS_RENDER_FAILED = "resume_render_failed"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SCORE_PROMPT_TEMPLATE = """
You are a deterministic resume-vs-job match scorer. Apply the rubric below
EXACTLY. Do not reward or penalize anything outside the rubric.

RUBRIC (total = keyword_coverage + title_alignment + experience_relevance):
- keyword_coverage (0-40): fraction of the job description's concrete required
  skills/technologies/domain keywords that appear (verbatim or as a clear
  synonym) in the resume, scaled to 40.
- title_alignment (0-30): how closely the resume's most recent titles and
  summary align with the target job title and seniority. Exact/equivalent
  title = 26-30, adjacent seniority or sibling function = 15-25, different
  function = 0-14.
- experience_relevance (0-30): how directly the resume's accomplishments and
  scope map to the job's stated responsibilities. Direct = 24-30,
  partial = 12-23, weak = 0-11.

JOB:
Title: {title}
Company: {company}
Description:
{description}

RESUME:
{resume_text}

Return ONLY a JSON object with EXACTLY these fields:
{{
  "score": <integer 0-100, MUST equal the sum of the three subscores>,
  "subscores": {{
    "keyword_coverage": <integer 0-40>,
    "title_alignment": <integer 0-30>,
    "experience_relevance": <integer 0-30>
  }},
  "missing_keywords": ["<job keywords absent from the resume>"],
  "reasoning": "<1-2 sentences>"
}}
"""

TAILOR_PROMPT_TEMPLATE = """
You are an expert resume writer. Rewrite the candidate's resume (Markdown) to
maximize its match against the job below. Current match score: {score}/100.
Job keywords currently missing from the resume: {missing_keywords}

HARD INTEGRITY CONSTRAINTS — violating any of these makes the output unusable:
1. You may ONLY rephrase, reorder, select, and emphasize content that already
   exists in the BASELINE RESUME below.
2. NEVER invent or alter employers, job titles, employment dates, degrees,
   schools, certifications, or security clearances. Copy them verbatim.
3. NEVER add a skill, tool, or technology that is not present in the baseline.
   A missing job keyword may only be added if the baseline contains it or an
   unambiguous equivalent (e.g. baseline "AWS EC2/S3" justifies "AWS").
4. Do not exaggerate scope (team sizes, budgets, metrics) beyond the baseline.

Tailoring you SHOULD do:
- Rewrite the professional summary toward this job's title and priorities.
- Reorder skills and bullets so the most job-relevant ones come first.
- Rephrase bullets using the job description's terminology where the baseline
  supports the same fact.
- Trim content irrelevant to this job.

JOB:
Title: {title}
Company: {company}
Description:
{description}

BASELINE RESUME (source of truth for all facts):
{baseline_text}

CURRENT RESUME DRAFT (to improve):
{resume_text}

Return ONLY a JSON object: {{"resume_markdown": "<the full tailored resume as Markdown>"}}
"""

FACTS_PROMPT_TEMPLATE = """
Extract every verifiable career fact CLAIMED in the resume below. Be literal —
list exactly what the resume claims, do not infer.

RESUME:
{resume_text}

Return ONLY a JSON object with EXACTLY these fields (empty lists if none):
{{
  "employers": ["<company/organization names worked at>"],
  "titles": ["<job titles held>"],
  "dates": ["<employment/education date ranges as written>"],
  "degrees": ["<degrees, e.g. 'BS Computer Science'>"],
  "certifications": ["<certifications/clearances claimed>"]
}}
"""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class ResumeScore:
    total: int
    subscores: dict = field(default_factory=dict)
    missing_keywords: list = field(default_factory=list)
    reasoning: str = ""


@dataclass
class TailorResult:
    status: str  # ready | below_threshold | no_baseline | render_failed | error
    resume_path: str = ""
    markdown_path: str = ""
    score: int = 0
    subscores: dict = field(default_factory=dict)
    iterations: int = 0
    from_cache: bool = False
    detail: str = ""


@dataclass
class GateDecision:
    proceed: bool
    resume_path: str = ""
    status: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a model)
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace for tolerant matching."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def check_facts_against_baseline(facts: dict, baseline_text: str) -> list[str]:
    """Diff claimed facts against the baseline resume text.

    Returns a list of human-readable violations (empty = grounded). A fact
    passes when its normalized form appears as a substring of the normalized
    baseline; date claims pass when every 4-digit year they mention exists in
    the baseline.
    """
    violations: list[str] = []
    norm_baseline = _normalize(baseline_text)
    squashed_baseline = norm_baseline.replace(" ", "")
    baseline_years = set(re.findall(r"\b(?:19|20)\d{2}\b", baseline_text or ""))

    def _grounded(claim: str) -> bool:
        norm_claim = _normalize(claim)
        if norm_claim in norm_baseline:
            return True
        # Abbreviation tolerance: "B.S. Computer Science" vs "BS Computer
        # Science" differ only in where punctuation split tokens — compare
        # with all spaces removed as a fallback.
        return norm_claim.replace(" ", "") in squashed_baseline

    for category in ("employers", "titles", "degrees", "certifications"):
        for claim in facts.get(category, []) or []:
            claim_str = str(claim).strip()
            if not claim_str:
                continue
            if not _grounded(claim_str):
                violations.append(f"{category[:-1]} not in baseline: {claim_str!r}")

    for claim in facts.get("dates", []) or []:
        claim_str = str(claim).strip()
        if not claim_str:
            continue
        for year in re.findall(r"\b(?:19|20)\d{2}\b", claim_str):
            if year not in baseline_years:
                violations.append(f"date year not in baseline: {year!r} (from {claim_str!r})")

    return violations


def _markdown_to_html(md: str) -> str:
    """Minimal Markdown → HTML for resume rendering (headings, bullets, bold,
    italics). Deliberately tiny — no new dependency for a constrained subset."""
    out: list[str] = []
    in_list = False

    def _inline(s: str) -> str:
        s = _html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        return s

    for raw_line in (md or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        is_bullet = stripped.startswith(("- ", "* "))
        if in_list and not is_bullet:
            out.append("</ul>")
            in_list = False
        if not stripped:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)) + 0, 4)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif is_bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:].strip())}</li>")
        else:
            out.append(f"<p>{_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _wrap_resume_html(body_html: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>"
        "body{font-family:Arial,sans-serif;font-size:11pt;color:#222;margin:0;padding:20px 30px}"
        "h1{font-size:20pt;margin:0 0 4px;color:#1e293b}"
        "h2{font-size:12pt;border-bottom:1px solid #999;padding-bottom:2px;"
        "margin:12px 0 6px;text-transform:uppercase;letter-spacing:.05em;color:#1e293b}"
        "h3{font-size:11pt;margin:10px 0 2px}"
        "h4{font-size:10pt;margin:8px 0 2px;color:#555}"
        "p{margin:4px 0;line-height:1.45}"
        "ul{margin:4px 0 8px 18px;padding:0}li{margin-bottom:2px;line-height:1.4}"
        "</style></head><body>"
        f"{body_html}"
        "</body></html>"
    )


def _safe_job_filename(job: dict) -> str:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        seed = f"{job.get('title','')}|{job.get('company','')}|{job.get('url','')}"
        job_id = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return re.sub(r"[^\w\-.]", "_", job_id)[:80]


def is_dummy_resume(path: str) -> bool:
    """True when a resolved resume path is the tests/ fixture (or any file
    named like it) — that file must never reach a real application."""
    if not path:
        return False
    return Path(path).name.lower() == DUMMY_RESUME_FILENAME


# ---------------------------------------------------------------------------
# ResumeTailor
# ---------------------------------------------------------------------------
class ResumeTailor:
    """Baseline-driven per-job resume tailoring with an integrity post-check."""

    def __init__(
        self,
        config: dict | None = None,
        model_client: Optional[ModelClient] = None,
        project_root: str | Path | None = None,
    ):
        self.config = config or {}
        rcfg = self.config.get("resume") or {}
        self.min_score = int(rcfg.get("min_score", DEFAULT_MIN_SCORE))
        self.max_iterations = int(rcfg.get("max_iterations", DEFAULT_MAX_ITERATIONS))
        self.baseline_path = str(rcfg.get("baseline_path") or "").strip()
        self.project_root = Path(project_root) if project_root else Path.cwd()
        out_dir = rcfg.get("output_dir") or DEFAULT_OUTPUT_DIR
        self.output_dir = Path(os.path.expanduser(str(out_dir)))
        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir
        self._model_client = model_client or ModelClient(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )
        self._baseline_cache: str | None = None

    # -- baseline ----------------------------------------------------------
    def load_baseline(self) -> str:
        """Load the structured baseline resume text.

        Supports .md/.markdown/.txt/.json directly. A .pdf baseline is
        extracted ONCE (via pypdf) into <output_dir>/baseline_extracted.md with
        a one-time setup message; re-extracted only if the PDF changes.
        """
        if self._baseline_cache is not None:
            return self._baseline_cache
        if not self.baseline_path:
            self._baseline_cache = ""
            return ""
        path = Path(os.path.expanduser(self.baseline_path))
        if not path.is_absolute():
            path = self.project_root / path
        if not path.is_file():
            console.print(
                f"[red]ResumeTailor:[/red] resume.baseline_path does not exist: {path}"
            )
            self._baseline_cache = ""
            return ""

        suffix = path.suffix.lower()
        text = ""
        if suffix in {".md", ".markdown", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".json":
            try:
                text = json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2)
            except Exception:
                text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".pdf":
            text = self._extract_pdf_baseline(path)
        else:
            console.print(
                f"[red]ResumeTailor:[/red] Unsupported baseline format '{suffix}' "
                "(use .md, .txt, .json, or .pdf)."
            )
        self._baseline_cache = text.strip()
        return self._baseline_cache

    def _extract_pdf_baseline(self, pdf_path: Path) -> str:
        """One-time extraction of a PDF baseline into an editable Markdown file."""
        extracted = self.output_dir / "baseline_extracted.md"
        try:
            if extracted.is_file() and extracted.stat().st_mtime >= pdf_path.stat().st_mtime:
                return extracted.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            text = "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception as exc:
            console.print(f"[red]ResumeTailor:[/red] Could not extract PDF baseline: {exc}")
            return ""
        if not text:
            console.print(
                f"[red]ResumeTailor:[/red] PDF baseline has no extractable text layer: {pdf_path}"
            )
            return ""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        extracted.write_text(text, encoding="utf-8")
        console.print(
            f"[cyan]ResumeTailor one-time setup:[/cyan] extracted baseline text from "
            f"{pdf_path} → {extracted}\n"
            "  Review/clean that Markdown file and point resume.baseline_path at it "
            "for best tailoring quality (the PDF keeps working; the extracted text "
            "is refreshed automatically if the PDF changes)."
        )
        return text

    def baseline_hash(self) -> str:
        text = self.load_baseline()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""

    # -- model steps -------------------------------------------------------
    async def score_resume_against_job(
        self, resume_text: str, job: dict, force_provider: str | None = None
    ) -> Optional[ResumeScore]:
        """Score resume vs job (0-100 with subscores). None on parse failure."""
        prompt = SCORE_PROMPT_TEMPLATE.format(
            title=job.get("title", ""),
            company=job.get("company", ""),
            description=(job.get("description") or "")[:4000],
            resume_text=(resume_text or "")[:6000],
        )
        raw = await self._model_client.complete(
            messages=[{"role": "user", "content": prompt}],
            task_type="reasoning",
            max_tokens=700,
            temperature=0.0,
            force_provider=force_provider,
        )
        return self.parse_score_response(raw)

    @staticmethod
    def parse_score_response(raw: str) -> Optional[ResumeScore]:
        if not raw or raw.startswith("No model available"):
            return None
        data = extract_json(raw, expect="object")
        if data is None:
            return None
        subscores_raw = data.get("subscores") or {}
        subscores: dict = {}
        for key, cap in (
            ("keyword_coverage", 40),
            ("title_alignment", 30),
            ("experience_relevance", 30),
        ):
            try:
                subscores[key] = max(0, min(cap, int(float(subscores_raw.get(key, 0)))))
            except (TypeError, ValueError):
                subscores[key] = 0
        try:
            total = max(0, min(100, int(float(data.get("score")))))
        except (TypeError, ValueError):
            total = sum(subscores.values()) if subscores_raw else None
            if total is None:
                return None
        missing = data.get("missing_keywords") or []
        if not isinstance(missing, list):
            missing = []
        return ResumeScore(
            total=total,
            subscores=subscores,
            missing_keywords=[str(k) for k in missing if k],
            reasoning=str(data.get("reasoning", "")),
        )

    async def _tailor_once(
        self,
        baseline_text: str,
        resume_text: str,
        job: dict,
        score: ResumeScore,
        force_provider: str | None = None,
    ) -> str:
        prompt = TAILOR_PROMPT_TEMPLATE.format(
            score=score.total,
            missing_keywords=", ".join(score.missing_keywords[:25]) or "(none reported)",
            title=job.get("title", ""),
            company=job.get("company", ""),
            description=(job.get("description") or "")[:3500],
            baseline_text=baseline_text[:6000],
            resume_text=(resume_text or "")[:6000],
        )
        raw = await self._model_client.complete(
            messages=[{"role": "user", "content": prompt}],
            task_type="reasoning",
            max_tokens=2500,
            temperature=0.2,
            force_provider=force_provider,
        )
        if not raw or raw.startswith("No model available"):
            return ""
        data = extract_json(raw, expect="object")
        if not data:
            return ""
        text = str(data.get("resume_markdown") or "").strip()
        # A tailored resume dramatically shorter than the baseline usually means
        # a truncated/degenerate generation — reject it.
        if len(text) < 200:
            return ""
        return text

    async def extract_claimed_facts(self, resume_text: str) -> Optional[dict]:
        raw = await self._model_client.complete(
            messages=[{"role": "user", "content": FACTS_PROMPT_TEMPLATE.format(resume_text=resume_text[:6000])}],
            task_type="reasoning",
            max_tokens=800,
            temperature=0.0,
        )
        if not raw or raw.startswith("No model available"):
            return None
        data = extract_json(raw, expect="object")
        return data if isinstance(data, dict) else None

    async def verify_integrity(self, tailored_text: str, baseline_text: str) -> tuple[bool, list[str]]:
        """Post-check: extract the tailored resume's claimed facts and diff them
        against the baseline. Fails CLOSED on extraction failure (rejects the
        draft rather than risking a fabricated resume going out)."""
        facts = await self.extract_claimed_facts(tailored_text)
        if facts is None:
            return False, ["fact extraction failed — rejecting draft (fail closed)"]
        violations = check_facts_against_baseline(facts, baseline_text)
        return (not violations), violations

    # -- rendering ---------------------------------------------------------
    async def render_pdf(self, markdown_text: str, out_path: str | Path) -> bool:
        """Render tailored Markdown to a text-layer PDF via Playwright Chromium
        (the repo's existing print engine — no new dependencies)."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html_doc = _wrap_resume_html(_markdown_to_html(markdown_text))
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content(html_doc, wait_until="domcontentloaded")
                    await page.pdf(
                        path=str(out_path),
                        format="Letter",
                        margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                    )
                finally:
                    await browser.close()
        except Exception as exc:
            console.print(f"[red]ResumeTailor:[/red] PDF render failed: {exc}")
            return False
        return out_path.is_file()

    # -- artifacts / cache ---------------------------------------------------
    def _artifact_paths(self, job: dict) -> tuple[Path, Path, Path]:
        stem = _safe_job_filename(job)
        return (
            self.output_dir / f"{stem}.pdf",
            self.output_dir / f"{stem}.md",
            self.output_dir / f"{stem}.json",
        )

    def _load_cached(self, job: dict) -> Optional[TailorResult]:
        """Reuse a previously tailored resume when it already clears the
        threshold AND the baseline hasn't changed since it was generated."""
        pdf_path, md_path, meta_path = self._artifact_paths(job)
        if not (meta_path.is_file() and pdf_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if meta.get("baseline_hash") != self.baseline_hash():
            return None
        score = int(meta.get("resume_score", 0) or 0)
        if score < self.min_score:
            return None
        return TailorResult(
            status="ready",
            resume_path=str(pdf_path),
            markdown_path=str(md_path),
            score=score,
            subscores=meta.get("resume_score_subscores", {}) or {},
            iterations=int(meta.get("iterations", 0) or 0),
            from_cache=True,
            detail="reused cached tailored resume",
        )

    def _write_artifacts(self, job: dict, markdown: str, score: ResumeScore, iterations: int) -> tuple[Path, Path]:
        pdf_path, md_path, meta_path = self._artifact_paths(job)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        meta = {
            "job_id": job.get("job_id", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "resume_score": score.total,
            "resume_score_subscores": score.subscores,
            "missing_keywords": score.missing_keywords[:25],
            "iterations": iterations,
            "min_score": self.min_score,
            "baseline_hash": self.baseline_hash(),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return pdf_path, md_path

    # -- main entry ----------------------------------------------------------
    async def ensure_tailored(self, job: dict) -> TailorResult:
        """Tailor (or reuse) a resume for this job and gate on min_score."""
        baseline = self.load_baseline()
        if not baseline:
            return TailorResult(status="no_baseline", detail="resume.baseline_path missing or unreadable")

        cached = self._load_cached(job)
        if cached:
            console.print(
                f"[green]ResumeTailor:[/green] cache hit — score {cached.score} ≥ {self.min_score} "
                f"({cached.resume_path})"
            )
            return cached

        try:
            best_score = await self.score_resume_against_job(baseline, job)
        except Exception as exc:
            return TailorResult(status="error", detail=f"baseline scoring failed: {exc}")
        if best_score is None:
            return TailorResult(status="error", detail="baseline scoring returned unparseable output")

        best_text = baseline
        current = baseline
        iterations = 0
        console.print(
            f"[cyan]ResumeTailor:[/cyan] baseline score {best_score.total}/100 "
            f"(threshold {self.min_score}) for {job.get('title','?')} @ {job.get('company','?')}"
        )

        while best_score.total < self.min_score and iterations < self.max_iterations:
            iterations += 1
            try:
                draft = await self._tailor_once(baseline, current, job, best_score)
            except Exception as exc:
                console.print(f"[yellow]ResumeTailor:[/yellow] iteration {iterations} failed: {exc}")
                break
            if not draft:
                console.print(f"[yellow]ResumeTailor:[/yellow] iteration {iterations}: no usable draft")
                continue
            try:
                ok, violations = await self.verify_integrity(draft, baseline)
            except Exception as exc:
                ok, violations = False, [f"integrity check errored: {exc}"]
            if not ok:
                console.print(
                    f"[red]ResumeTailor:[/red] iteration {iterations} REJECTED — integrity violations: "
                    + "; ".join(violations[:5])
                )
                # Do not accept, and do not iterate from a fabricated draft.
                continue
            try:
                score = await self.score_resume_against_job(draft, job)
            except Exception as exc:
                console.print(f"[yellow]ResumeTailor:[/yellow] scoring iteration {iterations} failed: {exc}")
                continue
            if score is None:
                continue
            console.print(f"[cyan]ResumeTailor:[/cyan] iteration {iterations}: score {score.total}/100")
            current = draft
            if score.total > best_score.total:
                best_text, best_score = draft, score

        # Local tier exhausted iterations without clearing the gate — escalate
        # ONE final attempt through the AI-OpenRouter gateway if it's configured
        # and the caller opted in via allowPaidCloud. The gateway's own budget
        # cap is what actually bounds spend; this gate just ensures we don't
        # silently start racking up cloud spend by default. Local-first behavior
        # is preserved: we only ever call the gateway after the free local tier
        # genuinely couldn't hit the gate.
        rcfg = self.config.get("resume") or {}
        allow_paid = bool(rcfg.get("allowPaidCloud"))
        gateway_configured, _, _ = self._model_client.get_gateway_config()
        if (
            best_score.total < self.min_score
            and allow_paid
            and gateway_configured
        ):
            console.print(
                f"[yellow]ResumeTailor:[/yellow] local exhausted at {best_score.total}/100 — "
                f"escalating one attempt to AI-OpenRouter gateway"
            )
            try:
                draft = await self._tailor_once(
                    baseline, current, job, best_score, force_provider="openrouter"
                )
                if draft:
                    ok, violations = await self.verify_integrity(draft, baseline)
                    if ok:
                        gw_score = await self.score_resume_against_job(
                            draft, job, force_provider="openrouter"
                        )
                        if gw_score is not None:
                            iterations += 1
                            console.print(
                                f"[cyan]ResumeTailor:[/cyan] gateway escalation: "
                                f"score {gw_score.total}/100"
                            )
                            if gw_score.total > best_score.total:
                                best_text, best_score = draft, gw_score
                    else:
                        console.print(
                            f"[red]ResumeTailor:[/red] gateway draft REJECTED — integrity: "
                            + "; ".join(violations[:5])
                        )
            except Exception as exc:
                # Escalation is best-effort — failure keeps the local best draft.
                console.print(f"[yellow]ResumeTailor:[/yellow] gateway escalation failed: {exc}")

        pdf_path, md_path = self._write_artifacts(job, best_text, best_score, iterations)

        if best_score.total < self.min_score:
            return TailorResult(
                status="below_threshold",
                markdown_path=str(md_path),
                score=best_score.total,
                subscores=best_score.subscores,
                iterations=iterations,
                detail=(
                    f"best score {best_score.total} < {self.min_score} after "
                    f"{iterations} iteration(s); best draft saved to {md_path}"
                ),
            )

        rendered = await self.render_pdf(best_text, pdf_path)
        if not rendered:
            return TailorResult(
                status="render_failed",
                markdown_path=str(md_path),
                score=best_score.total,
                subscores=best_score.subscores,
                iterations=iterations,
                detail=f"score {best_score.total} met threshold but PDF render failed",
            )
        return TailorResult(
            status="ready",
            resume_path=str(pdf_path),
            markdown_path=str(md_path),
            score=best_score.total,
            subscores=best_score.subscores,
            iterations=iterations,
            detail=f"tailored resume scored {best_score.total} ≥ {self.min_score}",
        )


# ---------------------------------------------------------------------------
# Apply-flow gate (called by Orchestrator.apply_approved before each attempt)
# ---------------------------------------------------------------------------
async def evaluate_resume_gate(
    job: dict,
    config: dict,
    state,
    tailor: Optional[ResumeTailor] = None,
) -> GateDecision:
    """Decide whether an apply attempt may proceed, and with which resume.

    - resume.enabled (default true) + baseline configured → tailor & gate on
      resume.min_score; block with `needs_resume_review` when unreachable.
    - No baseline configured → log loudly, fall back to current behavior.
    - In ALL fallback paths: refuse to apply if the resume that would be
      uploaded is the tests/dummy_resume.pdf fixture.
    """
    rcfg = config.get("resume") or {}
    enabled = bool(rcfg.get("enabled", True))
    job_id = str(job.get("job_id", ""))
    min_score = int(rcfg.get("min_score", DEFAULT_MIN_SCORE))

    def _fallback(reason: str = "") -> GateDecision:
        resolved = resolve_resume_path(config)
        if is_dummy_resume(resolved):
            detail = (
                f"Refusing to apply: resolved resume is the test fixture "
                f"({resolved}). Set resume.baseline_path or local_resume_path "
                "to a real resume."
            )
            console.print(f"[red]Resume gate:[/red] {detail}")
            state.record_apply_attempt(job_id, STATUS_DUMMY_RESUME_BLOCKED, detail)
            return GateDecision(False, status=STATUS_DUMMY_RESUME_BLOCKED, detail=detail)
        if reason:
            console.print(f"[yellow]Resume gate:[/yellow] {reason}")
        return GateDecision(True, detail=reason)

    if not enabled:
        return _fallback("resume tailoring disabled (resume.enabled=false) — using configured resume")

    tailor = tailor or ResumeTailor(config)
    if not tailor.load_baseline():
        return _fallback(
            "resume.baseline_path is NOT configured (or unreadable) — resume tailoring "
            "is SKIPPED and the statically configured resume will be used. Set "
            "resume.baseline_path in config.json to enable per-job tailoring."
        )

    result = await tailor.ensure_tailored(job)

    if result.status == "ready":
        state.record_application_analytics(
            job_id,
            {
                "tailored_resume_path": result.resume_path,
                "resume_score": result.score,
                "resume_score_subscores": result.subscores,
                "resume_tailor_iterations": result.iterations,
                "resume_tailor_cached": result.from_cache,
            },
        )
        console.print(
            f"[green]Resume gate:[/green] PASS — score {result.score} ≥ {min_score} "
            f"→ {result.resume_path}"
        )
        return GateDecision(True, resume_path=result.resume_path, status="resume_ready", detail=result.detail)

    if result.status == "below_threshold":
        detail = result.detail or f"best resume score {result.score} < {min_score}"
        console.print(f"[red]Resume gate:[/red] BLOCKED — {detail}")
        state.record_apply_attempt(job_id, STATUS_NEEDS_RESUME_REVIEW, detail)
        state.record_application_analytics(
            job_id,
            {
                "resume_score": result.score,
                "resume_score_subscores": result.subscores,
                "resume_tailor_iterations": result.iterations,
                "tailored_resume_draft_path": result.markdown_path,
            },
        )
        return GateDecision(False, status=STATUS_NEEDS_RESUME_REVIEW, detail=detail)

    if result.status == "render_failed":
        detail = result.detail or "tailored resume PDF render failed"
        console.print(f"[red]Resume gate:[/red] BLOCKED — {detail}")
        state.record_apply_attempt(job_id, STATUS_RENDER_FAILED, detail)
        return GateDecision(False, status=STATUS_RENDER_FAILED, detail=detail)

    # "error" (model unavailable / unparseable) — block THIS job as transient
    # rather than silently applying with an ungated resume.
    detail = result.detail or "resume tailoring errored"
    console.print(f"[red]Resume gate:[/red] BLOCKED — {detail}")
    state.record_apply_attempt(job_id, STATUS_TAILOR_ERROR, detail)
    return GateDecision(False, status=STATUS_TAILOR_ERROR, detail=detail)
