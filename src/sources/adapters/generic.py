"""GenericAtsAdapter — real form-filling fallback built on the shared modules.

Uses Antigravity's data modules (no jobright coupling):
  - adapters_patterns.ats_selectors.SELECTORS  (per-vendor field/submit/file selectors)
  - answers.question_bank.AnswerBank           (screening-question answers from profile)
  - adapters.evidence.EvidenceBuilder          (structured per-attempt evidence)

Live-verify on the Mac (no browser in CI sandbox). Page operations are funneled
through small awaitable helpers so the logic is unit-testable with a fake page.
"""
from __future__ import annotations

from rich.console import Console

from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult

console = Console()

# Identity fields we can fill from the profile, in priority order of selector keys.
_IDENTITY_MAP = {
    "first_name": ("personal_info", "first_name"),
    "last_name": ("personal_info", "last_name"),
    "name": ("personal_info", "full_name"),
    "email": ("personal_info", "email"),
    "phone": ("personal_info", "phone"),
    "linkedin": ("social_links", "linkedin"),
    "github": ("social_links", "github"),
    "portfolio": ("social_links", "portfolio"),
}


def detect_vendor(url: str) -> str:
    u = (url or "").lower()
    if "greenhouse.io" in u or "boards.greenhouse" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u or "jobs.ashby" in u:
        return "ashby"
    if "myworkdayjobs.com" in u or "workday" in u:
        return "workday"
    return "generic"


class GenericAtsAdapter(AtsAdapter):
    name = "generic"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Weak fallback: any dedicated vendor adapter (≥~0.8) outscores this.
        return 0.05

    # ---- small page helpers (overridable / fake-able in tests) ---------------
    async def _first_selector(self, page, selectors):
        """Return the first selector (from a str or list) that matches an element."""
        if isinstance(selectors, str):
            selectors = [selectors]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    return sel, el
            except Exception:
                continue
        return None, None

    async def _fill(self, page, selectors, value) -> bool:
        sel, el = await self._first_selector(page, selectors)
        if not el or value in (None, ""):
            return False
        try:
            await el.fill(str(value))
            return True
        except Exception:
            return False

    # ---- profile lookup ------------------------------------------------------
    def _profile_get(self, ctx: AtsApplyContext, section: str, key: str):
        prof = ctx.profile
        if prof is None:
            return None
        # ProfileService (attr access) OR raw dict
        sec = getattr(prof, section, None)
        if sec is None and isinstance(prof, dict):
            sec = prof.get(section, {})
        if isinstance(sec, dict):
            return sec.get(key)
        return None

    # ---- main flow -----------------------------------------------------------
    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        from ...adapters_patterns.ats_selectors import SELECTORS
        from ...adapters.evidence import EvidenceBuilder

        page = ctx.page
        vendor = detect_vendor(ctx.url or getattr(page, "url", ""))
        selset = SELECTORS.get(vendor, {})
        ev = EvidenceBuilder(vendor, ctx.url or "")
        if ctx.resume_path:
            ev.with_resume(ctx.resume_path)

        # 1. Blocker detection up front — never burn a submit on a login/captcha wall.
        blocker = await self._detect_blocker(page)
        if blocker:
            ev.blocker_detected = blocker
            return AtsApplyResult.blocked(
                blocker, f"{vendor}: {blocker} detected before fill",
                evidence=ev_to_dict(ev),
            )

        # 2. Identity fields
        fields = selset.get("fields", {})
        for fkey, sels in fields.items():
            if fkey not in _IDENTITY_MAP:
                continue
            section, pkey = _IDENTITY_MAP[fkey]
            val = self._profile_get(ctx, section, pkey)
            if val and await self._fill(page, sels, val):
                ev.add_field(fkey)

        # 3. Resume upload
        resume_sel = (selset.get("file_inputs") or {}).get("resume")
        if ctx.resume_path and resume_sel:
            if await self._upload(page, resume_sel, ctx.resume_path):
                ev.add_field("resume")

        # 4. Screening questions (best-effort, via AnswerBank)
        answered = await self._answer_questions(page, ctx)
        for q in answered:
            ev.add_field(f"q:{q}")

        # 5. Submit gate — never auto-submit unless explicitly allowed + policy-approved.
        if not ctx.auto_submit:
            return AtsApplyResult.blocked(
                "review_ready",
                f"{vendor}: filled {len(ev.fields_filled)} field(s); submit withheld (auto_submit=False)",
                evidence=ev_to_dict(ev),
            )
        if ctx.policy is not None:
            approved = await _maybe_await(ctx.policy.confirm_submit(ctx, ev_to_dict(ev)))
            if not approved:
                return AtsApplyResult.blocked(
                    "submit_denied_by_policy", f"{vendor}: policy withheld submit",
                    evidence=ev_to_dict(ev),
                )

        submitted = await self._submit(page, selset.get("submit_button"))
        if submitted:
            return AtsApplyResult.ok(
                f"{vendor}: submitted with {len(ev.fields_filled)} field(s)",
                evidence=ev_to_dict(ev), vendor=vendor,
            )
        ev.blocker_detected = "submit_not_found"
        return AtsApplyResult.blocked(
            "submit_not_found", f"{vendor}: no submit control matched",
            evidence=ev_to_dict(ev),
        )

    # ---- sub-steps -----------------------------------------------------------
    async def _detect_blocker(self, page) -> str | None:
        try:
            return await page.evaluate(
                """() => {
                    const body = (document.body?.innerText || '').toLowerCase();
                    if (document.querySelector('iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="turnstile" i]')) return 'captcha';
                    if (/verify you are human|checking your browser|cloudflare/i.test(body)) return 'captcha';
                    const pw = document.querySelector('input[type="password"]');
                    const loginish = /(sign in|log in|create account|forgot password|sso|single sign-on)/i.test(body);
                    if (pw && loginish) return 'login_required';
                    return null;
                }"""
            )
        except Exception:
            return None

    async def _upload(self, page, selector, path) -> bool:
        try:
            await page.set_input_files(selector, path)
            return True
        except Exception:
            return False

    async def _submit(self, page, submit_selectors) -> bool:
        if not submit_selectors:
            return False
        sel, el = await self._first_selector(page, submit_selectors)
        if not el:
            return False
        try:
            await el.click()
            return True
        except Exception:
            return False

    async def _answer_questions(self, page, ctx: AtsApplyContext) -> list[str]:
        """Best-effort: read visible question labels, resolve via AnswerBank, fill.
        Returns the list of question snippets answered."""
        from ...answers.question_bank import AnswerBank

        profile = ctx.profile if isinstance(ctx.profile, dict) else getattr(ctx.profile, "raw", {}) or {}
        bank = AnswerBank(profile if isinstance(profile, dict) else {})
        answered: list[str] = []
        try:
            questions = await page.evaluate(
                """() => Array.from(document.querySelectorAll('label')).map((l,i) => ({
                    i, text: (l.innerText||'').trim(),
                    for: l.getAttribute('for') || ''
                })).filter(q => q.text && q.text.length < 200).slice(0, 40)"""
            )
        except Exception:
            questions = []
        for q in questions or []:
            ans = bank.get_answer_for_question(q.get("text", ""))
            if ans is None or not q.get("for"):
                continue
            if await self._fill(page, f'#{q["for"]}', ans):
                answered.append(q["text"][:40])
        return answered


def ev_to_dict(ev) -> dict:
    try:
        return ev.build().to_dict() if hasattr(ev, "build") else {}
    except Exception:
        return {"evidence_vendor": getattr(ev, "vendor", ""), "evidence_fields_filled": getattr(ev, "fields_filled", [])}


async def _maybe_await(v):
    if hasattr(v, "__await__"):
        return await v
    return v
