from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from rich.console import Console
from playwright.async_api import Page
from pypdf import PdfReader

console = Console()


RESUME_EXTENSIONS = {".pdf", ".doc", ".docx"}


def resolve_resume_path(config: dict | None = None, preferred: str = "") -> str:
    """Return the best existing resume file path for uploads.

    Order of preference:
    1. Explicit tailored/downloaded resume path.
    2. Environment override.
    3. config.json local_resume_path/resume_path.
    4. Common local job-application folders.
    """
    config = config or {}
    candidates: list[Path] = []

    def add_path(value: str | None) -> None:
        if value:
            candidates.append(Path(value).expanduser())

    add_path(preferred)
    add_path(os.environ.get("LOCAL_RESUME_PATH"))
    add_path(os.environ.get("RESUME_PATH"))
    add_path(config.get("local_resume_path"))
    add_path(config.get("resume_path"))

    project_root = Path(__file__).parent.parent
    search_dirs = [
        project_root / "state" / "tailored_resumes",
        project_root / "state" / "resumes",
        project_root,
        Path.home() / "Documents" / "Job App",
        Path.home() / "Documents" / "Job App 2",
        Path.home() / "Documents",
        Path.home() / "Downloads",
        Path.home() / "Desktop",
    ]

    for path in candidates:
        resolved = _existing_resume(path)
        if resolved:
            return str(resolved)

    discovered = _discover_latest_resume(search_dirs)
    return str(discovered) if discovered else ""


def _existing_resume(path: Path) -> Path | None:
    if path.is_file() and path.suffix.lower() in RESUME_EXTENSIONS:
        return path
    if path.is_dir():
        return _discover_latest_resume([path])
    return None


def _discover_latest_resume(dirs: Iterable[Path]) -> Path | None:
    matches: list[Path] = []
    for directory in dirs:
        try:
            if not directory.exists() or not directory.is_dir():
                continue
            for child in directory.rglob("*"):
                if not child.is_file() or child.suffix.lower() not in RESUME_EXTENSIONS:
                    continue
                name = child.name.lower()
                if "resume" in name or "cv" in name:
                    matches.append(child)
        except Exception:
            continue
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)

class ResumeFieldFixer:
    """
    Intelligent second-pass agent that scans form pages for empty or incorrectly
    populated inputs and fills them using structured data from state/profile.json.
    """
    def __init__(self, profile_path: str = "state/profile.json"):
        self.profile_path = Path(profile_path)
        self.profile = {}
        self.load_profile()

    def load_profile(self) -> None:
        """Load personal profile from JSON."""
        if not self.profile_path.exists():
            console.print(f"[yellow]ResumeFieldFixer:[/yellow] Profile file not found at {self.profile_path}. Skipping second-pass fill.")
            return
        try:
            with open(self.profile_path, "r") as f:
                self.profile = json.load(f)
            console.print(f"[green]ResumeFieldFixer:[/green] Profile loaded successfully from {self.profile_path} ✓")
        except Exception as e:
            console.print(f"[red]ResumeFieldFixer: Error loading profile:[/red] {e}")

    async def fix_fields(self, page: Page) -> None:
        """
        Scan page for visible form inputs, analyze them semantically,
        and auto-fill any empty fields using the profile.
        """
        if not self.profile:
            return

        try:
            # Find all visible input, textarea, and select elements
            elements = await page.query_selector_all("input:visible, textarea:visible, select:visible")
            if not elements:
                return

            console.print(f"[magenta]ResumeFieldFixer:[/magenta] Evaluating {len(elements)} visible form element(s) on current step...")

            for el in elements:
                # Bypasses: skip hidden, disabled, or buttons/submits
                is_disabled = await el.evaluate("node => node.disabled")
                if is_disabled:
                    continue

                el_type = await el.evaluate("node => node.type")
                if el_type in ("submit", "button", "hidden", "file", "checkbox", "radio"):
                    # Checkboxes and radios need customized matching — handled separately below
                    continue

                # ── Step A: Get current value ──
                val = await el.evaluate("node => node.value")
                if val and val.strip():
                    # Already filled — skip
                    continue

                # ── Step B: Resolve semantic label ──
                label_text = await self._resolve_label(page, el)
                if not label_text:
                    continue

                # ── Step C: Match to profile and fill ──
                filled = await self._match_and_fill(el, label_text)
                if filled:
                    await el.evaluate("node => node.dispatchEvent(new Event('input', { bubbles: true }))")
                    await el.evaluate("node => node.dispatchEvent(new Event('change', { bubbles: true }))")


        except Exception as e:
            console.print(f"[yellow]ResumeFieldFixer:[/yellow] Scanner encountered an error (non-fatal): {e}")

    async def _resolve_label(self, page: Page, el) -> str:
        """Resolve a human-readable text label associated with an element."""
        # 1. Check for associated <label> using 'for' matching the element's id
        el_id = await el.evaluate("node => node.id")
        if el_id:
            label = await page.query_selector(f"label[for='{el_id}']")
            if label:
                txt = await label.inner_text()
                if txt.strip():
                    return txt.strip().lower()

        # 2. Check for parent <label>
        parent_label = await el.evaluate("""node => {
            let p = node.parentElement;
            while (p) {
                if (p.tagName.toLowerCase() === 'label') return p.innerText;
                p = p.parentElement;
            }
            return null;
        }""")
        if parent_label and parent_label.strip():
            return parent_label.strip().lower()

        # 3. Fall back to attributes: placeholder, name, aria-label, id, data-automation-id
        attrs = await el.evaluate("""node => {
            return {
                placeholder: node.getAttribute('placeholder'),
                name: node.getAttribute('name'),
                aria_label: node.getAttribute('aria-label'),
                data_auto: node.getAttribute('data-automation-id'),
                id: node.id
            }
        }""")

        for k in ("aria_label", "placeholder", "data_auto", "name", "id"):
            if attrs.get(k) and attrs[k].strip():
                # Clean up snake_case/kebab-case/camelCase for easy matching
                txt = re.sub(r'[-_]', ' ', attrs[k])
                txt = re.sub(r'([a-z])([A-Z])', r'\1 \2', txt)
                return txt.strip().lower()

        return ""


    async def _match_and_fill(self, el, label: str) -> bool:
        """Match a label string to a profile field and perform the fill."""
        p_info = self.profile.get("personal_info", {})
        s_links = self.profile.get("social_links", {})
        disclosures = self.profile.get("disclosures", {})

        tag_name = await el.evaluate("node => node.tagName.toLowerCase()")

        # --- A: Dropdowns / Selects ---
        if tag_name == "select":
            # Voluntary Disclosures
            if any(w in label for w in ("gender", "sex")):
                return await self._select_by_text(el, disclosures.get("gender", "Male"))
            if any(w in label for w in ("race", "ethnicity", "hispanic")):
                return await self._select_by_text(el, disclosures.get("race", ""))
            if "veteran" in label:
                return await self._select_by_text(el, disclosures.get("veteran", ""))
            if any(w in label for w in ("disability", "disabled")):
                return await self._select_by_text(el, disclosures.get("disability", ""))

            # General Country/State selects
            if "country" in label:
                return await self._select_by_text(el, p_info.get("country", "United States"))
            if any(w in label for w in ("state", "province")):
                return await self._select_by_text(el, p_info.get("state", ""))
            return False

        # --- B: Text / TextArea Fields ---
        matches = [
            (r"first.*name|given.*name", p_info.get("first_name")),
            (r"last.*name|family.*name|surname", p_info.get("last_name")),
            (r"email|e[- ]?mail", p_info.get("email")),
            (r"phone|tel|mobile", p_info.get("phone")),
            (r"address|street", p_info.get("address")),
            (r"city|town", p_info.get("city")),
            (r"zip|postal", p_info.get("zip")),
            (r"linked\s*in", s_links.get("linkedin")),
            (r"git\s*hub", s_links.get("github")),
            (r"portfolio|website|personal.*site", s_links.get("portfolio") or s_links.get("website")),
        ]

        for pattern, val in matches:
            if val and re.search(pattern, label):
                from rich.markup import escape
                console.print(f"  [dim]ResumeFieldFixer: Filling '{escape(label)}' → [yellow]{escape(val)}[/yellow][/dim]")
                await el.fill(val)
                return True

        return False

    async def _select_by_text(self, el, text: str) -> bool:
        """Fuzzy match and select an option inside a dropdown by its visible text."""
        if not text:
            return False
        try:
            # Query all options inside the select element
            options = await el.query_selector_all("option")
            for opt in options:
                opt_txt = await opt.inner_text()
                o_txt = opt_txt.strip().lower()
                val_txt = text.strip().lower()

                # Check for exact match or word-boundary matches
                if o_txt == val_txt or re.search(rf"\b{re.escape(val_txt)}\b", o_txt):
                    # Edge-case safety: ensure "male" does not match "female"
                    if val_txt == "male" and o_txt == "female":
                        continue
                    opt_val = await opt.evaluate("node => node.value")
                    await el.select_option(value=opt_val)
                    console.print(f"  [dim]ResumeFieldFixer: Selected dropdown option → [yellow]{opt_txt.strip()}[/yellow][/dim]")
                    return True
        except Exception:
            pass
        return False


@dataclass
class ATSValidationResult:
    """Carries detailed metrics from PDF verification."""
    passed: bool
    coverage: float
    matched_keywords: list[str]
    unmatched_keywords: list[str]
    failure_type: str | None = None
    detail: str = ""


class ATSReadabilityError(Exception):
    """Base exception raised when a generated resume PDF fails verification checks."""
    pass


class ATSValidationError(ATSReadabilityError):
    """Base validation error for structured ATS checks."""
    pass


class PDFTextLayerError(ATSValidationError):
    """Raised when the generated PDF has no readable/extractable text layer."""
    def __init__(self, result: ATSValidationResult):
        self.result = result
        super().__init__(result.detail)


class KeywordCoverageError(ATSValidationError):
    """Raised when keyword coverage is below the minimum threshold."""
    def __init__(self, result: ATSValidationResult):
        self.result = result
        super().__init__(result.detail)


def check_ats_readability(
    pdf_path: str,
    target_keywords: list[str],
    *,
    minimum_coverage: float = 0.70,
) -> ATSValidationResult:
    """Extracts text layer from the PDF and verifies target keyword presence.

    Args:
        pdf_path: Path to the generated PDF.
        target_keywords: List of target keywords to find in the PDF.
        minimum_coverage: Required proportion of keywords that must match (0.0 to 1.0).

    Raises:
        FileNotFoundError: If the PDF does not exist.
        PDFTextLayerError: If the PDF is unreadable or has no text.
        KeywordCoverageError: If matching keywords fall below minimum_coverage.
    """
    if not (0.0 <= minimum_coverage <= 1.0):
        raise ValueError("minimum_coverage threshold must be between 0.0 and 1.0")

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    try:
        reader = PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + " "
    except Exception as e:
        result = ATSValidationResult(
            passed=False,
            coverage=0.0,
            matched_keywords=[],
            unmatched_keywords=[],
            failure_type="pdf_text_layer",
            detail=f"Failed to parse PDF text layer: {e}",
        )
        raise PDFTextLayerError(result)

    if not text.strip():
        result = ATSValidationResult(
            passed=False,
            coverage=0.0,
            matched_keywords=[],
            unmatched_keywords=list(target_keywords),
            failure_type="pdf_text_layer",
            detail=(
                "ATS check failed: Generated PDF has no extractable text layer. "
                "It might have compiled as a flat image or fonts are unreadable."
            ),
        )
        raise PDFTextLayerError(result)

    # Clean and deduplicate target keywords (case-insensitive)
    unique_targets = []
    seen = set()
    for kw in target_keywords:
        if not kw or not kw.strip():
            continue
        norm_kw = kw.strip().lower()
        if norm_kw not in seen:
            seen.add(norm_kw)
            unique_targets.append(kw.strip())

    if not unique_targets:
        return ATSValidationResult(
            passed=True,
            coverage=1.0,
            matched_keywords=[],
            unmatched_keywords=[],
            failure_type="keyword_validation_skipped",
            detail="Claude returned no matching keywords."
        )

    normalized_text = text.lower()
    
    # Text normalization for robust punctuation-invariant matching
    def normalize_str(s: str) -> str:
        s_clean = s.lower()
        s_clean = re.sub(r"[\-_/\\,.;:]", " ", s_clean)
        return " ".join(s_clean.split())

    def contains_keyword(keyword: str, haystack: str) -> bool:
        if not keyword:
            return False
        if " " in keyword:
            return keyword in haystack
        pattern = ""
        if keyword[0].isalnum() or keyword[0] == "_":
            pattern += r"(?<!\w)"
        pattern += re.escape(keyword)
        if keyword[-1].isalnum() or keyword[-1] == "_":
            pattern += r"(?!\w)"
        return bool(re.search(pattern, haystack))

    text_norm = normalize_str(normalized_text)

    matched_keywords = []
    unmatched_keywords = []

    for keyword in unique_targets:
        kw_clean = keyword.lower().strip()
        kw_norm = normalize_str(kw_clean)
        
        found = contains_keyword(kw_clean, normalized_text) or contains_keyword(kw_norm, text_norm)

        if found:
            matched_keywords.append(keyword)
        else:
            unmatched_keywords.append(keyword)

    total_count = len(unique_targets)
    matched_count = len(matched_keywords)
    coverage = matched_count / total_count if total_count > 0 else 1.0

    result = ATSValidationResult(
        passed=(coverage >= minimum_coverage),
        coverage=coverage,
        matched_keywords=matched_keywords,
        unmatched_keywords=unmatched_keywords,
    )

    if not result.passed:
        result.failure_type = "keyword_coverage"
        result.detail = (
            f"ATS check failed: Keyword coverage ({coverage*100:.1f}%) is below threshold "
            f"({minimum_coverage*100:.0f}%). Missing target keywords: {', '.join(unmatched_keywords)}"
        )
        raise KeywordCoverageError(result)

    return result
