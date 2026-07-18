"""WorkdayAdapter (Phase 3) — the biggest missing vendor in the adapter path.

Workday URLs previously fell through to GenericAtsAdapter, which knows Workday
*field* selectors but never enters the apply wizard, so it never reached (let alone
submitted) a Workday form. This adapter replicates the legacy god-module flow
(jobright.py `_click_ats_apply_button` + `_workday_handle_post_chooser`) as a proper
adapter:

  1. enter the wizard: navigate to `<job>/apply/autofillWithResume` + click the
     "Start Your Application" chooser;
  2. detect the sign-in / session-expiry gate -> `workday_session_expired` (an
     auth_required outcome routed to re-auth; a submit is never burned on it);
  3. upload the resume so Workday autofills, best-effort identity fill;
  4. step the multi-page wizard (Next -> ... -> Review);
  5. submit through the shared policy + receipt gate (Phase 0.1/0.3).

Non-interactive by contract: unlike the legacy path it never prompts for manual
sign-in — it reports `workday_session_expired` and lets re-auth handle it.
Structured with small awaitable helpers so it is unit-testable with a fake page.
"""
from __future__ import annotations

import urllib.parse

from .context import AtsApplyContext, AtsApplyResult
from .generic import GenericAtsAdapter, detect_vendor, ev_to_dict
from .receipt import verify_receipt  # noqa: F401  (kept for symmetry / future use)

# Workday's wizard uses stable data-automation-id hooks.
_NEXT_SELECTORS = [
    "[data-automation-id='bottom-navigation-next-button']",
    "button[data-automation-id='nextButton']",
]
_SUBMIT_SELECTORS = [
    "[data-automation-id='submitButton']",
    "button[data-automation-id='bottomNavigationSubmitButton']",
]
_RESUME_SELECTOR = (
    "div[data-automation-id='file-upload-drop-zone'] input[type='file'], "
    "input[type='file']"
)
_CHOOSER_TEXTS = (
    "use my last application", "autofill with resume", "apply manually",
    "start application",
)
_LOGIN_URL_TOKENS = ("/login", "/signin", "/sign-in", "/auth", "login.", "sso.")

# Workday applications can run long (GDIT/CVS/Citi see 15-20+ pages); the legacy path
# used a 30-step bound.
_MAX_WIZARD_STEPS = 30


class WorkdayAdapter(GenericAtsAdapter):
    name = "workday"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        url = ctx.url or getattr(ctx.page, "url", "")
        return 0.9 if detect_vendor(url) == "workday" else 0.0

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        from ...adapters.evidence import EvidenceBuilder

        page = ctx.page
        ev = EvidenceBuilder("workday", ctx.url or "")
        if ctx.resume_path:
            ev.with_resume(ctx.resume_path)

        # 1. Enter the apply wizard (nav to autofillWithResume + chooser).
        await self._enter_apply(page)

        # 2. Session-expiry / sign-in gate — auth_required, never burn a submit.
        if await self._is_login_gate(page):
            ev.blocker_detected = "workday_session_expired"
            return AtsApplyResult.blocked(
                "workday_session_expired",
                "Workday session expired — route to re-auth (prepare-sessions).",
                evidence=ev_to_dict(ev),
            )

        # 3. Resume upload drives Workday's autofill; then best-effort identity fill.
        if ctx.resume_path and await self._upload(page, _RESUME_SELECTOR, ctx.resume_path):
            ev.add_field("resume")
        await self._fill_identity(page, ctx, ev)

        # 4. Advance through the wizard until a Review/Submit control is present.
        if not await self._advance_to_submit(page, ctx, ev):
            return AtsApplyResult.blocked(
                "form_not_reached",
                "Workday: could not reach a review/submit step within the wizard",
                evidence=ev_to_dict(ev),
            )

        # 5. Shared policy-gated submit + receipt verification. Include the Next
        #    selectors as a fallback: on the review page Workday renders its FINAL
        #    submit as `bottom-navigation-next-button`, and _advance_to_submit stops
        #    (without clicking) there — so the real submit is clicked here, through the
        #    policy + receipt gate, never auto-clicked while advancing.
        return await self._gated_submit(
            page, ctx, _SUBMIT_SELECTORS + _NEXT_SELECTORS, ev, "workday")

    # ---- Workday-specific steps ---------------------------------------------
    async def _enter_apply(self, page) -> None:
        url = ""
        try:
            url = getattr(page, "url", "") or ""
        except Exception:
            url = ""
        if url and "/apply" not in url:
            # modify the PATH only — string-concatenating onto a URL with a query or
            # fragment (…/job/R123?source=linkedin) would append into the query.
            parsed = urllib.parse.urlparse(url)
            new_path = parsed.path.rstrip("/") + "/apply/autofillWithResume"
            target = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        await self._click_chooser(page)

    async def _click_chooser(self, page) -> bool:
        """Click the first visible 'Start Your Application' chooser control by text.
        Uses a JS text-match click (Workday's synthetic-event sensitivity)."""
        try:
            return bool(await page.evaluate(
                """(texts) => {
                    const els = Array.from(document.querySelectorAll('button, a, [role=\"button\"]'));
                    for (const el of els) {
                        const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (t && texts.some(x => t === x || t.includes(x))) { el.click(); return true; }
                    }
                    return false;
                }""",
                list(_CHOOSER_TEXTS),
            ))
        except Exception:
            return False

    async def _is_login_gate(self, page) -> bool:
        url = ""
        try:
            url = (getattr(page, "url", "") or "").lower()
        except Exception:
            url = ""
        if any(tok in url for tok in _LOGIN_URL_TOKENS):
            return True
        try:
            return bool(await page.evaluate(
                """() => {
                    const t = (document.body && document.body.innerText || '').toLowerCase();
                    if (/create account\\/?sign in|sign in with email|sign in with google|sign in with linkedin/.test(t)) return true;
                    // plain Workday Sign In gate: a 'Sign In' control and no application inputs
                    const signIn = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'))
                        .some(el => /^\\s*sign in\\s*$/i.test((el.innerText || el.textContent || el.value || '').trim()));
                    const fields = document.querySelectorAll(
                        "input[data-automation-id='text-input'], textarea, input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search])").length;
                    return signIn && fields === 0;
                }"""
            ))
        except Exception:
            return False

    async def _fill_identity(self, page, ctx: AtsApplyContext, ev) -> None:
        """Best-effort identity fill on the current wizard page via Workday's
        data-automation-id text inputs."""
        mapping = {
            "legalNameSection_firstName": ("personal_info", "first_name"),
            "legalNameSection_lastName": ("personal_info", "last_name"),
            "email": ("personal_info", "email"),
            "phone-number": ("personal_info", "phone"),
        }
        for auto_id, (section, key) in mapping.items():
            val = self._profile_get(ctx, section, key)
            if not val:
                continue
            sel = f"input[data-automation-id='{auto_id}']"
            if await self._fill(page, sel, val):
                ev.add_field(auto_id)

    async def _advance_to_submit(self, page, ctx: AtsApplyContext, ev) -> bool:
        """Click Next through wizard pages until a submit/review control appears.
        Bounded, and stops if neither Next nor Submit is present (stuck)."""
        for _ in range(_MAX_WIZARD_STEPS):
            _, submit_el = await self._first_selector(page, _SUBMIT_SELECTORS)
            if submit_el:
                return True
            # On the final review page the "Next" control IS the real submit. Stop here
            # WITHOUT clicking so the shared gate (policy + receipt) performs that click,
            # rather than burning the submit while advancing.
            if await self._is_review_page(page):
                return True
            _, next_el = await self._first_selector(page, _NEXT_SELECTORS)
            if not next_el:
                return False  # neither next nor submit — stuck
            try:
                await next_el.click()
            except Exception:
                return False
            # each new page may carry more identity fields
            await self._fill_identity(page, ctx, ev)
        # exhausted steps — reached submit only if a submit/review control is present
        _, submit_el = await self._first_selector(page, _SUBMIT_SELECTORS)
        return bool(submit_el) or await self._is_review_page(page)

    async def _is_review_page(self, page) -> bool:
        """Detect the Workday final review/submit step by its copy."""
        try:
            return bool(await page.evaluate(
                """() => {
                    const t = (document.body && document.body.innerText || '').toLowerCase();
                    return /review your application|submit your application|by (clicking )?submit|i (accept|agree)|please review your/.test(t);
                }"""
            ))
        except Exception:
            return False
