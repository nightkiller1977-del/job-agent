"""GreenhouseAdapter (Phase 6) — real vendor-specific apply logic on top of
GenericAtsAdapter's shared fill/submit pipeline.

Greenhouse's default `#application_form` is a single page (no multi-step wizard),
so this adapter deliberately does NOT override `apply()` — GenericAtsAdapter.apply()
already does blocker-detection, identity fill, resume upload, and the policy-gated
submit correctly for Greenhouse (see SELECTORS["greenhouse"] in
src/adapters_patterns/ats_selectors.py). Reimplementing apply() here would just
duplicate that flow for no benefit, and three agents are touching sibling vendor
files in parallel, so keeping this change local to an override (rather than a new
shared base class) avoids merge risk (see ACES-68 research report §4).

This subclass fills two vendor-specific gaps GenericAtsAdapter's flow does not
cover, both folded into an override of `_answer_questions` — the one hook that
runs after resume upload and before the submit gate:

  1. Cover-letter upload. SELECTORS["greenhouse"]["file_inputs"]["cover_letter"]
     is defined but GenericAtsAdapter.apply() only ever reads
     file_inputs["resume"]; ctx.cover_letter_path is otherwise never consumed.

  2. Any <select> dropdown or radio group with a proper `<label for=...>` --
     most commonly voluntary self-identification (EEO) questions (gender,
     race/ethnicity, veteran status, disability status), but the scan itself
     is not EEO-specific: it matches every labeled select/radio group on the
     page, including non-EEO ones (citizenship, security clearance, education,
     work arrangement) if a company renders them this way. Greenhouse's
     generated markup renders these wrapped in a `.field` div, which
     GenericAtsAdapter._answer_questions() cannot fill: it only `.fill()`s
     <label for>-linked text inputs, and Playwright's `.fill()` raises
     (silently swallowed) on a <select>. The answer for *what* to fill still
     comes exclusively from the existing AnswerBank
     (src/answers/question_bank.py) via field_type="select" -- nothing here
     hardcodes a per-vendor answer; this only adds the DOM-side scan +
     option-matching AnswerBank lacks, and resolves AnswerBank's canonical
     keyword (e.g. "Male", "Decline to Self-Identify") against whatever text a
     given company's option copy actually uses.

Submission is never reimplemented here — every path ends in the inherited,
unmodified `_gated_submit`, so the policy-gate / verified-receipt invariant
(src/sources/adapters/context.py, AtsApplyResult) holds identically to every
other adapter.
"""
from __future__ import annotations

import re

from .generic import GenericAtsAdapter, detect_vendor
from .context import AtsApplyContext

# Decline/opt-out phrasing Greenhouse-hosted companies word differently across
# their own custom EEO copy. AnswerBank (src/answers/question_bank.py,
# _format_disclosure) already resolves a "decline to answer" disclosure down to
# its own canonical string ("Decline to Self-Identify") — this is only the
# vendor-side synonym set used to match THAT canonical string against whatever
# option text a given company actually rendered.
_DECLINE_SYNONYMS = (
    "decline", "prefer not", "don't wish", "do not wish", "not to disclose",
    "not disclose", "choose not", "rather not", "n/a", "none",
)

# JS: collect every visible <select> together with its best-guess label text
# (label[for=id] first, falling back to the nearest ancestor `.field`'s label —
# Greenhouse's generated markup wraps each question in a `.field` div) and each
# option's index/value/visible text, so the Python side can fuzzy-match an
# AnswerBank answer without needing to know a company's exact option values.
_SELECT_SCAN_JS = r"""() => {
    const selects = Array.from(document.querySelectorAll('select'));
    return selects.map((sel) => {
        let label = '';
        if (sel.id) {
            const l = document.querySelector(`label[for="${sel.id}"]`);
            if (l) label = (l.innerText || l.textContent || '').trim();
        }
        if (!label) {
            const field = sel.closest('.field');
            const l = field ? field.querySelector('label') : null;
            if (l) label = (l.innerText || l.textContent || '').trim();
        }
        const options = Array.from(sel.options || []).map((o, oi) => ({
            index: oi, value: o.value, text: (o.textContent || '').trim(),
        }));
        return { id: sel.id || '', name: sel.name || '', label, options };
    }).filter((s) => s.label && s.options.length > 1);
}"""

# JS: collect radio-button groups (by `name`) with a best-guess group label,
# defensively covering the case where a Greenhouse company configured EEO
# questions as radios instead of a <select>.
_RADIO_SCAN_JS = r"""() => {
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
    const seen = new Set();
    const groups = [];
    for (const r of radios) {
        const name = r.name || '';
        if (!name || seen.has(name)) continue;
        seen.add(name);
        const groupRadios = radios.filter((x) => x.name === name);
        const field = r.closest('.field');
        let label = '';
        if (field) {
            const l = field.querySelector('label, legend');
            if (l) label = (l.innerText || l.textContent || '').trim();
        }
        const options = groupRadios.map((x, oi) => {
            const lab = x.closest('label') ||
                (x.id ? document.querySelector(`label[for="${x.id}"]`) : null);
            const text = lab ? (lab.innerText || lab.textContent || '').trim() : (x.value || '');
            return { index: oi, value: x.value, text };
        });
        if (label && options.length > 1) groups.push({ name, label, options });
    }
    return groups;
}"""

# JS: click one radio inside the named group by its index within that group
# (index was already resolved on the Python side via _best_option_match, so
# there is no boolean-semantics guessing here — the exact matched option wins).
_RADIO_CLICK_JS = r"""({name, index}) => {
    const radios = Array.from(document.querySelectorAll('input[type="radio"]'))
        .filter((r) => r.name === name);
    const target = radios[index];
    if (!target) return false;
    target.click();
    return true;
}"""


def _best_option_match(answer, options: list) -> "dict | None":
    """Fuzzy-match an AnswerBank answer string against visible option text.

    AnswerBank returns a canonical capitalized keyword (e.g. "Male", "White",
    "Decline to Self-Identify") that will rarely equal a company's exact option
    copy verbatim, so this matches on a whole-word/phrase boundary rather than a
    raw substring — a raw `"male" in text` check would wrongly match the option
    "Female" (which literally contains the substring "male"). Falls back to the
    decline-synonym set when the answer itself is a decline/opt-out.
    """
    if not answer or not options:
        return None
    ans = str(answer).strip().lower()
    if not ans:
        return None
    is_decline = ans.startswith("decline") or any(kw in ans for kw in _DECLINE_SYNONYMS)

    ans_pattern = re.compile(r"\b" + re.escape(ans) + r"\b")
    for opt in options:
        text = (opt.get("text") or "").strip().lower()
        if text and ans_pattern.search(text):
            return opt
    # Reverse direction: the option's own text is the short/canonical word
    # (e.g. option text "Male" contained inside a longer answer phrase).
    for opt in options:
        text = (opt.get("text") or "").strip().lower()
        if text and re.search(r"\b" + re.escape(text) + r"\b", ans):
            return opt
    if is_decline:
        for opt in options:
            text = (opt.get("text") or "").strip().lower()
            if text and any(kw in text for kw in _DECLINE_SYNONYMS):
                return opt
    return None


class GreenhouseAdapter(GenericAtsAdapter):
    """Specialized adapter for Greenhouse job boards."""
    name = "greenhouse"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Greenhouse token detected in URL
        return 0.9 if detect_vendor(ctx.url) == "greenhouse" else 0.0

    async def _answer_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        """Extends GenericAtsAdapter's label/text-input scan with the two
        Greenhouse-specific gaps described in the module docstring."""
        answered = await super()._answer_questions(page, ctx)
        answered += await self._upload_cover_letter(page, ctx)
        answered += await self._answer_select_questions(page, ctx)
        answered += await self._answer_radio_questions(page, ctx)
        return answered

    async def _upload_cover_letter(self, page, ctx: AtsApplyContext) -> list[str]:
        if not ctx.cover_letter_path:
            return []
        from ...adapters_patterns.ats_selectors import SELECTORS

        cl_sel = (SELECTORS.get("greenhouse", {}).get("file_inputs") or {}).get("cover_letter")
        if not cl_sel:
            return []
        if await self._upload(page, cl_sel, ctx.cover_letter_path):
            return ["cover_letter"]
        return []

    async def _answer_select_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        bank = self._answer_bank(ctx)
        try:
            selects = await page.evaluate(_SELECT_SCAN_JS)
        except Exception:
            selects = []

        answered: list[str] = []
        for s in selects or []:
            label = (s.get("label") or "").strip()
            if not label:
                continue
            ans = bank.get_answer_for_question(label, field_type="select")
            if ans is None:
                continue
            option = _best_option_match(ans, s.get("options") or [])
            if option is None:
                continue
            sel_id = s.get("id") or ""
            sel_name = s.get("name") or ""
            if sel_id:
                selector = f"#{sel_id}"
            elif sel_name:
                selector = f'select[name="{sel_name}"]'
            else:
                continue
            _, el = await self._first_selector(page, selector)
            if not el:
                continue
            try:
                if option.get("value"):
                    await el.select_option(value=option["value"])
                else:
                    await el.select_option(label=option.get("text"))
            except Exception:
                continue
            answered.append(label[:40])
        return answered

    async def _answer_radio_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        bank = self._answer_bank(ctx)
        try:
            groups = await page.evaluate(_RADIO_SCAN_JS)
        except Exception:
            groups = []

        answered: list[str] = []
        for g in groups or []:
            label = (g.get("label") or "").strip()
            if not label:
                continue
            ans = bank.get_answer_for_question(label, field_type="select")
            if ans is None:
                continue
            option = _best_option_match(ans, g.get("options") or [])
            if option is None:
                continue
            try:
                clicked = await page.evaluate(
                    _RADIO_CLICK_JS, {"name": g.get("name"), "index": option.get("index")}
                )
            except Exception:
                clicked = False
            if clicked:
                answered.append(label[:40])
        return answered

    def _answer_bank(self, ctx: AtsApplyContext):
        from ...answers.question_bank import AnswerBank

        profile = ctx.profile if isinstance(ctx.profile, dict) else getattr(ctx.profile, "raw", {}) or {}
        return AnswerBank(profile if isinstance(profile, dict) else {})
