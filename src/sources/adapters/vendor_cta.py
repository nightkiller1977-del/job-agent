"""Phase 3 — CTA-entry vendor adapters: Microsoft, BrassRing, SmartRecruiters, Teamtailor.

These vendors, in the legacy god module (`_handle_microsoft_apply`,
`_handle_brassring_apply`, and the SmartRecruiters/Teamtailor branches of
`_click_ats_apply_button`), share one shape: **click an entry CTA to reach the
application form, and detect a login/account wall** so an auth blocker is reported
(routed to re-auth) instead of a burned submit. Field-filling itself was the Jobright
extension's job; here we reach the form and hand off to the shared GenericAtsAdapter
fill + the Phase 0.1/0.3 policy+receipt submit gate.

One `CtaApplyAdapter` base captures that shape; each vendor is a thin subclass with
its own entry CTAs (and, for Teamtailor, a `/applications/new` URL rewrite). All are
unit-testable with a fake page.
"""
from __future__ import annotations

from .context import AtsApplyContext, AtsApplyResult
from .generic import GenericAtsAdapter, detect_vendor

# Reused from the legacy _looks_like_login_wall.
_LOGIN_WALL_JS = r"""() => {
    const body = (document.body && document.body.innerText || '').toLowerCase();
    const password = !!document.querySelector('input[type="password"]');
    const email = !!document.querySelector('input[type="email"], input[name*="email" i], input[placeholder*="email" i]');
    const loginText = /(sign in|log in|login|create account|forgot password|sso|single sign-on)/i.test(body);
    return password || (email && loginText);
}"""

# Click the first visible control whose text matches one of the regex patterns
# (anchors navigate via href; buttons click). Mirrors _click_first_matching_link_or_button.
_CLICK_CTA_JS = r"""(patterns) => {
    const regexes = patterns.map(p => new RegExp(p, 'i'));
    const els = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
    const visible = el => {
        const s = window.getComputedStyle(el); const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled && r.width > 0 && r.height > 0;
    };
    for (const el of els) {
        if (!visible(el)) continue;
        const text = [el.innerText, el.textContent, el.value, el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' ').trim();
        if (!regexes.some(re => re.test(text))) continue;
        const href = el.href || el.getAttribute('href');
        if (href && !href.startsWith('#')) { window.location.href = href; } else { el.click(); }
        return true;
    }
    return false;
}"""

_FORM_PROBE_JS = r"""() => {
    const hasField = !!document.querySelector('input:not([type="hidden"]), textarea, select, [data-automation-id]');
    const applied = /application|apply|resume|cover letter/i.test(document.body && document.body.innerText || '');
    return hasField || applied;
}"""


class CtaApplyAdapter(GenericAtsAdapter):
    """Reach a vendor form via an entry CTA, guarding the login wall, then reuse the
    generic fill + shared gated submit."""

    # `name` (inherited slot) IS the vendor identity for these adapters.
    cta_patterns: list[str] = []

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        url = ctx.url or getattr(ctx.page, "url", "")
        return 0.9 if detect_vendor(url) == self.name else 0.0

    def _rewrite_url(self, url: str) -> str:
        """Override to navigate straight to the application form. Default: no rewrite."""
        return url

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        page = ctx.page
        current = ctx.url or getattr(page, "url", "") or ""

        # Optional direct-to-form navigation (e.g. Teamtailor /applications/new).
        target = self._rewrite_url(current)
        if target and target != current:
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        # Login/account wall BEFORE the form -> auth_required, never burn a submit.
        if await self._login_wall(page):
            return AtsApplyResult.blocked(
                f"{self.name}_login_required",
                f"{self.name}: login/account wall before the form — route to re-auth.",
            )

        entered = await self._click_cta(page)

        # Some portals redirect to login only after the CTA.
        if entered and await self._login_wall(page):
            return AtsApplyResult.blocked(
                f"{self.name}_login_required",
                f"{self.name}: redirected to login after the apply CTA — route to re-auth.",
            )

        if not entered and not await self._looks_like_form(page):
            return AtsApplyResult.blocked(
                f"{self.name}_apply_not_reached",
                f"{self.name}: could not enter the application flow (no apply CTA / form).",
            )

        # Reached the form — reuse generic fill + the shared policy+receipt submit gate.
        return await super().apply(ctx)

    # ---- helpers (fake-able) -------------------------------------------------
    async def _login_wall(self, page) -> bool:
        try:
            return bool(await page.evaluate(_LOGIN_WALL_JS))
        except Exception:
            return False

    async def _click_cta(self, page) -> bool:
        if not self.cta_patterns:
            return False
        try:
            return bool(await page.evaluate(_CLICK_CTA_JS, list(self.cta_patterns)))
        except Exception:
            return False

    async def _looks_like_form(self, page) -> bool:
        try:
            return bool(await page.evaluate(_FORM_PROBE_JS))
        except Exception:
            return False


class MicrosoftAdapter(CtaApplyAdapter):
    name = "microsoft"
    cta_patterns = ["^apply now$", "^apply$", "apply for this job",
                    "start application", "continue application"]


class BrassRingAdapter(CtaApplyAdapter):
    name = "brassring"
    cta_patterns = ["^apply to job$", "^apply$", "^apply now$", "apply for this job",
                    "start application"]


class SmartRecruitersAdapter(CtaApplyAdapter):
    name = "smartrecruiters"
    cta_patterns = ["i'?m interested", "^apply$", "^apply now$", "apply for this job"]


class TeamtailorAdapter(CtaApplyAdapter):
    name = "teamtailor"
    cta_patterns = ["^apply here$", "^apply$", "^apply now$"]

    def _rewrite_url(self, url: str) -> str:
        u = (url or "")
        if u and "/applications/new" not in u.lower():
            return u.split("?")[0].rstrip("/") + "/applications/new"
        return url
