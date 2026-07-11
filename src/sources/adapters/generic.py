"""7a: GenericAtsAdapter — the registry fallback.

SCAFFOLD. The full behavior is the generic body of
`JobrightScraper.apply_external_ats_job` (jobright.py:237) — document upload,
`_looks_like_application_form`, `_confirm_and_submit`, visible-controls snapshot,
and the `ResumeFieldFixer.fix_fields` call. That extraction changes the live apply
path and therefore MUST be verified against a real browser on the Mac (this
sandbox has no playwright/pypdf), so it is intentionally NOT inlined here yet.

What IS delivered: an interface-conformant fallback adapter so the registry, the
contract, and Antigravity's P5/P6 adapters can be built and tested now. can_handle
returns a low baseline so any real vendor adapter outscores it.
"""
from __future__ import annotations

from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult


class GenericAtsAdapter(AtsAdapter):
    name = "generic"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Always a weak fallback; real vendor adapters (score ~0.8+) win.
        return 0.05

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        raise NotImplementedError(
            "GenericAtsAdapter.apply: extract the generic body of "
            "JobrightScraper.apply_external_ats_job (jobright.py:237) here and "
            "verify parity against a real browser on the Mac before wiring the "
            "registry into the live apply path. Until then the existing "
            "JobrightScraper path remains the live implementation."
        )
