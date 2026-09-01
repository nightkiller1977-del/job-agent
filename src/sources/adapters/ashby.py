"""AshbyAdapter (ACES-68) — Ashby job board apply flow.

Ashby's application UI is a heavily componentized React app: `SELECTORS["ashby"]["form"]`
is deliberately unspecific (just `"form"`) because there is no stable Ashby-wide form
id, and its EEO/voluntary-disclosure dropdowns are commonly custom listbox widgets — a
button that opens a popup of options on click — not native `<select>` elements, so
Playwright's `select_option()` cannot drive those directly. Larger companies also
sometimes render the Ashby application as a multi-step wizard rather than one page.

This adapter follows the `WorkdayAdapter` shape (the existing precedent for a vendor
that needs more than GenericAtsAdapter's single-pass fill): it fully overrides
`apply()`, keeps its own small overridable helpers, and reuses the inherited
`_gated_submit` for the policy + receipt-verified final step — never reimplemented.

  1. blocker/login-wall check up front, via the inherited `_detect_blocker`
     (never burn a submit on a login/captcha wall);
  2. step the form (usually one page, occasionally a wizard) for up to
     `_MAX_WIZARD_STEPS` iterations: fill identity fields, upload the resume, answer
     EEO/custom questions on the current step — every answer is resolved through the
     existing `AnswerBank` (never hardcoded) — then either a submit control is
     present (stop and hand off) or a Next/Continue control is clicked to advance
     (stop if neither is found);
  3. the shared policy-gated submit + receipt verification, with an Ashby-specific
     secondary confirmation signal layered on top: Ashby's SPA commonly swaps the
     form out for a bare confirmation panel with no distinctive copy the generic
     text/URL receipt patterns would catch, so a submit click followed by the
     application form disappearing from the DOM is treated as a fallback receipt.

EEO/demographic questions are answered in two passes per step:
  - native controls first (a real `<select>` or `input[type=radio]` group) — cheap,
    reliable, and what smaller Ashby-hosted companies often use;
  - a click-open-listbox JS fallback second, for Ashby's custom combobox widgets.
    This is the most fragile piece of the whole adapter — it depends on generic
    ARIA/role heuristics rather than a stable Ashby-wide selector, so treat it as
    best-effort and verify against a real Ashby posting before trusting it blindly.

Every answer is resolved via `AnswerBank.get_answer_for_question` (field_type="select"
so boolean answers normalize to "Yes"/"No" the same way select-driven questions do
elsewhere) — the EEO section there ignores field_type entirely, so this is also the
single source of truth for *what* to answer; this module only supplies *how* to find
and drive the DOM control for a given label/legend text.
"""
from __future__ import annotations

import asyncio

from .context import AtsApplyContext, AtsApplyResult
from .generic import GenericAtsAdapter, detect_vendor, ev_to_dict, _IDENTITY_MAP

# Ashby has no stable Next/Continue selector across companies (heavily componentized
# React app, no data-automation-id-style hooks like Workday) -> text-match click, the
# same technique as Workday's `_click_chooser` / vendor_cta's `_CLICK_CTA_JS`.
_NEXT_TEXTS = ("continue", "next", "next step", "next page")

# Ashby's multi-step wizard is uncommon (mostly larger companies) and shorter than
# Workday's enterprise flows; bound the loop the same way Workday bounds its (longer)
# wizard so a stuck page can never hang an attempt.
_MAX_WIZARD_STEPS = 12

# Decline-to-answer synonyms actual option copy uses, beyond AnswerBank's own
# "Decline to Self-Identify" string (question_bank.py `_format_disclosure`).
_DECLINE_SYNONYMS = (
    "decline", "prefer not", "don't wish", "do not wish", "not disclose", "rather not",
)

_QUESTION_SCAN_JS = r"""/* ashby-question-scan */() => {
    const out = [];
    const seenRadioGroups = new Set();
    let counter = 0;
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const nearbyText = (el) => {
        if (el.id) {
            const l = document.querySelector(`label[for="${el.id}"]`);
            if (l) return (l.innerText || l.textContent || '').trim();
        }
        const wrap = el.closest('label');
        if (wrap) return (wrap.innerText || wrap.textContent || '').trim();
        const group = el.closest('fieldset, [role="radiogroup"], [role="group"], div');
        if (group) {
            const lbl = group.querySelector('legend, label, [class*="label" i]');
            if (lbl && lbl !== el && !el.contains(lbl)) return (lbl.innerText || lbl.textContent || '').trim();
        }
        return el.getAttribute('aria-label') || '';
    };

    // 1. native <select> — cheap, reliable, what smaller companies often configure.
    document.querySelectorAll('select').forEach((sel) => {
        if (!visible(sel)) return;
        const text = nearbyText(sel);
        if (!text) return;
        const id = 'ashby-q-' + (counter++);
        sel.setAttribute('data-ashby-scan-id', id);
        out.push({ kind: 'select', id, text: text.slice(0, 200) });
    });

    // 2. native radio groups, deduped by name.
    document.querySelectorAll('input[type="radio"]').forEach((r) => {
        if (!visible(r)) return;
        const name = r.name || '';
        if (!name || seenRadioGroups.has(name)) return;
        const group = r.closest('fieldset, [role="radiogroup"], [role="group"], div') || document;
        const legend = group.querySelector('legend, label, [class*="label" i]');
        const text = legend ? (legend.innerText || legend.textContent || '').trim() : nearbyText(r);
        if (!text) return;
        seenRadioGroups.add(name);
        const id = 'ashby-q-' + (counter++);
        document.querySelectorAll(`input[type="radio"][name="${name}"]`)
            .forEach((rb) => rb.setAttribute('data-ashby-scan-id', id));
        out.push({ kind: 'radio', id, text: text.slice(0, 200) });
    });

    // 3. custom listbox trigger controls — Ashby's own EEO dropdowns are commonly
    //    a button/combobox that opens a popup of options, not a native <select>.
    document.querySelectorAll(
        '[role="button"][aria-haspopup], button[aria-haspopup], [role="combobox"], [aria-haspopup="listbox"]'
    ).forEach((btn) => {
        if (!visible(btn)) return;
        const text = nearbyText(btn) || btn.getAttribute('aria-label') || '';
        if (!text) return;
        const id = 'ashby-q-' + (counter++);
        btn.setAttribute('data-ashby-scan-id', id);
        out.push({ kind: 'listbox', id, text: text.slice(0, 200) });
    });

    return out.slice(0, 40);
}"""

_FILL_SELECT_JS = r"""/* ashby-question-fill:select */({ id, keywords }) => {
    const el = document.querySelector(`[data-ashby-scan-id="${id}"]`);
    if (!el) return false;
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const opts = Array.from(el.options || []);
    const opt = opts.find((o) => keywords.some(
        (k) => new RegExp('\\b' + esc(k) + '\\b', 'i').test(o.textContent || o.value || '')
    ));
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}"""

_FILL_RADIO_JS = r"""/* ashby-question-fill:radio */({ id, keywords }) => {
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const radios = Array.from(document.querySelectorAll(`input[type="radio"][data-ashby-scan-id="${id}"]`));
    const match = radios.find((r) => {
        const label = r.closest('label');
        const text = (label ? (label.innerText || label.textContent || '') : '') + ' ' + (r.value || '');
        return keywords.some((k) => new RegExp('\\b' + esc(k) + '\\b', 'i').test(text));
    });
    if (!match) return false;
    match.click();
    return true;
}"""

# Two-phase: open the popup, then click the matching option — the same shape as any
# custom combobox widget. Highest-risk piece of this adapter (see module docstring);
# the selector list is a generic best-effort guess, not an Ashby-specific contract.
_FILL_LISTBOX_JS = r"""/* ashby-question-fill:listbox */async ({ id, keywords }) => {
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const trigger = document.querySelector(`[data-ashby-scan-id="${id}"]`);
    if (!trigger) return false;
    trigger.click();
    await new Promise((r) => setTimeout(r, 60));
    const options = Array.from(document.querySelectorAll(
        '[role="option"], [role="menuitem"], ul[class*="menu" i] li, div[class*="option" i]'
    ));
    const match = options.find((o) => {
        const text = o.innerText || o.textContent || '';
        return keywords.some((k) => new RegExp('\\b' + esc(k) + '\\b', 'i').test(text));
    });
    if (!match) return false;
    match.click();
    return true;
}"""

_FORM_GONE_JS = r"""/* ashby-form-gone */() => {
    const forms = document.querySelectorAll('form');
    if (forms.length === 0) return true;
    // A wrapper <form> sometimes survives with its contents swapped for a thank-you
    // message — treat "no fillable/submittable controls left" as gone too.
    return Array.from(forms).every((f) => f.querySelectorAll(
        'input:not([type="hidden"]), textarea, select, button[type="submit"]'
    ).length === 0);
}"""

_CLICK_NEXT_JS = r"""/* ashby-click-next */(texts) => {
    const visible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
    };
    const els = Array.from(document.querySelectorAll(
        'button, [role="button"], input[type="submit"], input[type="button"]'
    ));
    for (const el of els) {
        if (!visible(el)) continue;
        const t = (el.innerText || el.textContent || el.value || '').trim().toLowerCase();
        if (t && texts.some((x) => t === x || t.includes(x))) { el.click(); return true; }
    }
    return false;
}"""


def _answer_keywords(ans) -> list[str]:
    """Turn an AnswerBank answer into the set of option-text keywords to match.

    `_format_disclosure` (question_bank.py) always returns one capitalized keyword
    (e.g. "Male", "Decline to Self-Identify") regardless of field_type — real Ashby
    companies word a decline option differently ("I don't wish to answer", "Prefer
    not to disclose", ...), so a decline answer expands to a synonym set; anything
    else matches on its own word (word-boundary, so "male" never matches "female").
    """
    base = str(ans).strip().lower()
    if not base:
        return []
    kws = [base]
    if "decline" in base or "prefer not" in base:
        kws.extend(k for k in _DECLINE_SYNONYMS if k not in kws)
    return kws


class AshbyAdapter(GenericAtsAdapter):
    """Specialized adapter for Ashby job boards."""
    name = "ashby"

    # Pause after clicking Next/Continue so a React re-render settles before the next
    # step's fields are scanned. Instance attribute (not a bare sleep call) so tests
    # can zero it out instead of paying real wall-clock time per wizard step.
    _step_delay = 0.3

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Ashby detected in URL
        return 0.9 if detect_vendor(ctx.url) == "ashby" else 0.0

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        from ...adapters_patterns.ats_selectors import SELECTORS
        from ...adapters.evidence import EvidenceBuilder

        page = ctx.page
        selset = SELECTORS.get("ashby", {})
        ev = EvidenceBuilder("ashby", ctx.url or "")
        if ctx.resume_path:
            ev.with_resume(ctx.resume_path)

        # 1. Blocker/login-wall up front — never burn a submit on it.
        blocker = await self._detect_blocker(page)
        if blocker:
            ev.blocker_detected = blocker
            return AtsApplyResult.blocked(
                blocker, f"ashby: {blocker} detected before fill",
                evidence=ev_to_dict(ev),
            )

        # 2. Step the form: usually one page, occasionally a multi-step wizard.
        submit_selectors = selset.get("submit_button") or []
        resume_sel = (selset.get("file_inputs") or {}).get("resume")
        resume_uploaded = False

        for _ in range(_MAX_WIZARD_STEPS):
            await self._fill_identity(page, ctx, ev, selset.get("fields", {}))

            if not resume_uploaded and ctx.resume_path and resume_sel:
                if await self._upload(page, resume_sel, ctx.resume_path):
                    ev.add_field("resume")
                    resume_uploaded = True

            for q in await self._answer_custom_questions(page, ctx):
                ev.add_field(f"q:{q}")
            # Fallback: any plain text question with a proper <label for=...> that a
            # company configured natively (the generic scan Greenhouse/Lever rely on).
            for q in await self._answer_questions(page, ctx):
                ev.add_field(f"q:{q}")

            _, submit_el = await self._first_selector(page, submit_selectors)
            if submit_el:
                break
            if not await self._click_next(page):
                break
            if self._step_delay:
                await asyncio.sleep(self._step_delay)

        # 3. Shared policy-gated submit + receipt verification (Ashby-augmented).
        return await self._gated_submit(page, ctx, submit_selectors, ev, "ashby")

    # ---- Ashby-specific steps ------------------------------------------------
    async def _fill_identity(self, page, ctx: AtsApplyContext, ev, fields: dict) -> None:
        """Identity fill scoped to Ashby's own SELECTORS entry — reuses the shared
        _IDENTITY_MAP (its keys already match Ashby's name/email/phone/linkedin/
        github/portfolio field keys) without touching generic.py's apply() flow,
        since this adapter fully owns its own apply()."""
        for fkey, sels in (fields or {}).items():
            if fkey not in _IDENTITY_MAP:
                continue
            section, pkey = _IDENTITY_MAP[fkey]
            val = self._profile_get(ctx, section, pkey)
            # Ashby's "name" field is a single combined field (like Lever/Teamtailor).
            if not val and fkey == "name":
                first = self._profile_get(ctx, "personal_info", "first_name")
                last = self._profile_get(ctx, "personal_info", "last_name")
                val = " ".join(p for p in (first, last) if p).strip() or None
            if val and await self._fill(page, sels, val):
                ev.add_field(fkey)

    async def _click_next(self, page) -> bool:
        """Text-match click of a visible Next/Continue control (Ashby has no stable
        selector for this across companies). Returns whether one was found+clicked."""
        try:
            return bool(await page.evaluate(_CLICK_NEXT_JS, list(_NEXT_TEXTS)))
        except Exception:
            return False

    async def _answer_custom_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        """EEO/demographic + other custom-widget question handling for the current
        step. Every answer comes from AnswerBank (never hardcoded); this method only
        supplies the DOM matching/filling for whatever control renders the question:
        native <select>, native radio group, or (fallback) a click-open listbox."""
        from ...answers.question_bank import AnswerBank

        profile = ctx.profile if isinstance(ctx.profile, dict) else getattr(ctx.profile, "raw", {}) or {}
        bank = AnswerBank(profile if isinstance(profile, dict) else {})
        answered: list[str] = []
        try:
            questions = await page.evaluate(_QUESTION_SCAN_JS)
        except Exception:
            questions = []

        fill_scripts = {
            "select": _FILL_SELECT_JS,
            "radio": _FILL_RADIO_JS,
            "listbox": _FILL_LISTBOX_JS,
        }
        for q in questions or []:
            text = q.get("text", "")
            # field_type="select" so a boolean-shaped answer (work_auth, sponsorship)
            # normalizes to "Yes"/"No" the same way any select-driven question would;
            # EEO answers ignore field_type entirely (question_bank.py _format_disclosure).
            ans = bank.get_answer_for_question(text, field_type="select", job=ctx.job)
            if ans is None:
                continue
            keywords = _answer_keywords(ans)
            if not keywords:
                continue
            script = fill_scripts.get(q.get("kind"))
            if not script:
                continue
            try:
                filled = bool(await page.evaluate(script, {"id": q.get("id"), "keywords": keywords}))
            except Exception:
                filled = False
            if filled:
                answered.append(text[:40])
        return answered

    async def _gated_submit(self, page, ctx: AtsApplyContext, submit_selectors,
                            ev, vendor: str) -> AtsApplyResult:
        """The shared policy+receipt gate (never reimplemented), with an
        Ashby-specific secondary confirmation signal layered on top: Ashby's SPA
        commonly swaps the form out for a bare confirmation panel with no
        distinctive copy the generic text/URL receipt check would catch, so a
        submit click followed by the application form disappearing from the DOM
        is a secondary, structural signal -- checked only after the text-based
        receipt check already found nothing.

        This never upgrades the result to submitted/verified: a form
        disappearing is not a receipt (a validation re-render, a transient
        "processing..." swap, or a session-timeout redirect can remove it too),
        and a false positive here would mark the job permanently applied in the
        idempotency ledger, which is worse than the honest unverified() outcome
        it would replace. It only annotates the still-unverified result with a
        diagnostic signal for the reconciliation pass."""
        res = await super()._gated_submit(page, ctx, submit_selectors, ev, vendor)
        if res.status == "submission_unverified" and await self._form_gone(page):
            res.analytics["structural_signal"] = "form_removed"
        return res

    async def _form_gone(self, page) -> bool:
        try:
            return bool(await page.evaluate(_FORM_GONE_JS))
        except Exception:
            return False
