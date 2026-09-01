"""LeverAdapter — Lever-specific fill logic on top of GenericAtsAdapter.

Lever's `#application-form` is single-page (no wizard), so unlike WorkdayAdapter
this does NOT override `apply()` — GenericAtsAdapter's flow (blocker check, identity
fill, resume upload, `_answer_questions`, gated submit) is reused as-is; only the
small overridable helpers below add Lever-specific behaviour:

  - `_answer_questions`: after Generic's <label for> text-input scan, also
      (a) fills the `org` (current employer) field — defined in
          SELECTORS["lever"]["fields"] but not in Generic's `_IDENTITY_MAP`, so the
          identity-fill loop silently skips it;
      (b) fills the "Additional Information" textarea ONLY when the profile carries
          an explicit value (never auto-generates cover-note text via the LLM
          fallback in AnswerBank); and
      (c) scans native <select> EEO/voluntary-disclosure dropdowns via AnswerBank,
          since Generic's `.fill()`-based scan throws on (and thus silently skips) a
          <select>.
  - `_gated_submit`: reuses the inherited policy+receipt gate unchanged, then — only
    when the receipt check comes back unverified — checks whether Lever's SPA has
    already replaced `#application-form` with a confirmation panel, as a secondary,
    structural signal for companies whose custom confirmation copy misses the
    generic text patterns in receipt.py. This never substitutes for the click or the
    policy gate; it only reclassifies an already-clicked, already-authorized submit
    whose text-based receipt check found nothing.
"""
from __future__ import annotations

import re
from typing import Optional

from .context import AtsApplyContext, AtsApplyResult
from .generic import GenericAtsAdapter, detect_vendor

# Lever's default "Additional Information" box is `textarea[name="comments"]`; some
# companies rename it, so also fall back to a name-contains match.
_ADDITIONAL_INFO_SELECTOR = "textarea[name='comments' i], textarea[name*='additional' i]"

# Substrings that mark a dropdown option (or an AnswerBank answer) as a "decline to
# self-identify" choice. Lever-hosted companies word the option differently company
# to company ("I don't wish to answer", "Prefer not to say", "Decline to
# self-identify", ...), so this is a keyword set, not an exact-text match.
_DECLINE_SYNONYMS = (
    "decline", "prefer not", "don't wish", "do not wish",
    "not to disclose", "not disclose", "self-identify",
)

# JS-only comment token ("lever_eeo_select_scan") lets fake-page unit tests dispatch
# this specific script by a substring no other evaluate() call in the codebase
# shares (see tests/test_ats_generic_adapter.py / test_vendor_cta_adapters.py for the
# existing dispatch-by-substring convention this follows).
_EEO_SELECT_JS = r"""() => {
    // lever_eeo_select_scan
    const labelFor = (sel) => {
        if (sel.id) {
            const lbl = document.querySelector(`label[for="${sel.id}"]`);
            if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
        }
        const aria = sel.getAttribute('aria-label');
        if (aria) return aria.trim();
        const container = sel.closest('.application-question, .application-field, fieldset, div');
        if (container) {
            const lbl = container.querySelector('label, .application-label, legend');
            if (lbl && lbl !== sel) return (lbl.innerText || lbl.textContent || '').trim();
        }
        return '';
    };
    return Array.from(document.querySelectorAll('select')).map((sel) => ({
        id: sel.id || '',
        name: sel.getAttribute('name') || '',
        label: labelFor(sel).slice(0, 200),
        options: Array.from(sel.options || []).map((o) => ({
            value: o.value,
            text: (o.textContent || '').trim(),
        })),
    })).filter((s) => s.label && s.options.length);
}"""

# JS-only comment token ("lever_form_removed_check") for the same dispatch-by-
# substring reason.
_FORM_REMOVED_JS = "() => !document.querySelector('#application-form') /* lever_form_removed_check */"


class LeverAdapter(GenericAtsAdapter):
    """Specialized adapter for Lever job boards."""
    name = "lever"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Lever detected in URL
        return 0.9 if detect_vendor(ctx.url) == "lever" else 0.0

    # ---- extra field-fill layered on Generic's single-pass flow --------------
    async def _answer_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        # 1. Generic's <label for> text-input scan (unchanged) — plain text
        #    screening questions still go through the exact same path.
        answered = await super()._answer_questions(page, ctx)

        # 2. "org" (current employer) — defined in SELECTORS["lever"]["fields"] but
        #    not in Generic's _IDENTITY_MAP, so the identity-fill loop silently
        #    skips it. Only fill when the profile actually names a current employer.
        from ...adapters_patterns.ats_selectors import SELECTORS

        org_sel = (SELECTORS.get("lever", {}).get("fields", {}) or {}).get("org")
        employer = self._current_employer(ctx)
        if org_sel and employer and await self._fill(page, org_sel, employer):
            answered.append("org")

        # 3. "Additional Information" — only ever fill an explicit profile value;
        #    never route this through AnswerBank's open-ended LLM fallback (that's a
        #    policy decision outside this adapter's scope).
        extra_info = self._additional_information(ctx)
        if extra_info and await self._fill(page, _ADDITIONAL_INFO_SELECTOR, extra_info):
            answered.append("additional_information")

        # 4. Native <select> EEO/voluntary-disclosure dropdowns.
        answered.extend(await self._answer_select_questions(page, ctx))
        return answered

    async def _answer_select_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        """Fill native <select> EEO fields via AnswerBank — the same source of truth
        Generic's text-input scan uses, just routed through select_option() instead
        of .fill() (which throws on, and Generic silently swallows, a <select>)."""
        from ...answers.question_bank import AnswerBank

        profile = ctx.profile if isinstance(ctx.profile, dict) else getattr(ctx.profile, "raw", {}) or {}
        bank = AnswerBank(profile if isinstance(profile, dict) else {})
        answered: list[str] = []
        try:
            selects = await page.evaluate(_EEO_SELECT_JS)
        except Exception:
            selects = []
        for s in selects or []:
            label = (s or {}).get("label") or ""
            options = (s or {}).get("options") or []
            if not label or not options:
                continue
            ans = bank.get_answer_for_question(label, field_type="select")
            if ans is None:
                continue
            opt = self._match_eeo_option(str(ans), options)
            if not opt:
                continue
            sel_id, sel_name = s.get("id"), s.get("name")
            target = f"#{sel_id}" if sel_id else (f"select[name='{sel_name}']" if sel_name else None)
            if not target:
                continue
            if await self._select_option(page, target, opt):
                answered.append(label[:40])
        return answered

    def _match_eeo_option(self, answer: str, options: list) -> Optional[dict]:
        """Resolve AnswerBank's capitalized keyword (e.g. "Male", "Decline to
        Self-Identify") against the dropdown's real, company-worded <option> text —
        never assume the keyword IS the literal option text."""
        if not answer or not options:
            return None
        ans = answer.strip().lower()
        is_decline = any(s in ans for s in _DECLINE_SYNONYMS)
        if not is_decline:
            pattern = re.compile(r"\b" + re.escape(ans) + r"\b", re.IGNORECASE)
            for opt in options:
                if pattern.search(opt.get("text") or ""):
                    return opt
        # Decline (or no direct keyword match) -> fall back to a decline-worded option.
        for opt in options:
            text = (opt.get("text") or "").lower()
            if any(s in text for s in _DECLINE_SYNONYMS):
                return opt
        return None

    async def _select_option(self, page, selector: str, opt: dict) -> bool:
        _, el = await self._first_selector(page, selector)
        if not el:
            return False
        try:
            if opt.get("value"):
                await el.select_option(value=opt["value"])
            else:
                await el.select_option(label=opt.get("text"))
            return True
        except Exception:
            return False

    def _current_employer(self, ctx: AtsApplyContext) -> Optional[str]:
        """Most recent work_history entry's employer, but only when it looks like a
        CURRENT role (no end_date, or end_date == "Present") — never guess a former
        employer into Lever's "org" field."""
        prof = ctx.profile
        history = None
        if isinstance(prof, dict):
            history = prof.get("work_history")
        else:
            history = getattr(prof, "work_history", None)
            if history is None:
                raw = getattr(prof, "raw", None)
                if isinstance(raw, dict):
                    history = raw.get("work_history")
        if not isinstance(history, list) or not history:
            return None
        first = history[0]
        if not isinstance(first, dict):
            return None
        end_date = str(first.get("end_date") or "").strip().lower()
        if end_date and end_date != "present":
            return None
        company = first.get("company_name")
        return str(company) if company else None

    def _additional_information(self, ctx: AtsApplyContext) -> Optional[str]:
        for key in ("additional_information", "additional_info", "cover_note"):
            val = self._profile_get(ctx, "disclosures", key)
            if val:
                return str(val)
        return None

    # ---- submit-confirmation: structural fallback on top of the shared gate --
    async def _gated_submit(self, page, ctx: AtsApplyContext, submit_selectors,
                            ev, vendor: str) -> AtsApplyResult:
        result = await super()._gated_submit(page, ctx, submit_selectors, ev, vendor)
        if result.status != "submission_unverified":
            return result
        # Lever's SPA replaces #application-form with a confirmation panel on
        # success, but a form disappearing is not itself a receipt -- plenty of
        # non-success events remove the form root too (validation re-render, a
        # transient "processing..." swap, a session-timeout redirect). Upgrading
        # to AtsApplyResult.ok() here would mark the job permanently applied in
        # the idempotency ledger on a false positive, which is worse than the
        # honest unverified() outcome it would replace (that one stays eligible
        # for reconciliation). So this only *annotates* the still-unverified
        # result with a diagnostic signal for the reconciliation pass -- it
        # never upgrades submitted/verified/status itself.
        if await self._lever_form_removed(page):
            result.analytics["structural_signal"] = "form_removed"
        return result

    async def _lever_form_removed(self, page) -> bool:
        try:
            return bool(await page.evaluate(_FORM_REMOVED_JS))
        except Exception:
            return False
