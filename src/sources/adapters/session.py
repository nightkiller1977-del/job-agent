"""7a: ExternalApplySession — owns browser lifecycle for external ATS applies.

Real implementation (live-verify on the Mac — no browser in the CI sandbox). Not
yet wired into the live apply path: LinkedIn/Indeed/`external` still call
JobrightScraper.apply_external_ats_job until P4 flips the call sites over, so this
can be exercised in isolation first.

Layering (see ATS_ADAPTER_PLAN.md → Three-Layer Decomposition):
    Site Scraper → ExternalApplySession → AtsAdapterRegistry.pick(ctx).apply(ctx)

Verified corrections honored here:
  1. Extension load is a PRE-LAUNCH decision keyed off the external URL
     (jobright.py:258-268 sets load_extensions = not is_teamtailor before launch).
  2. Replicates the direct-nav-with-extension path (jobright.py:237), NOT the
     Jobright-card autofill handoff (jobright.py:1534/1602).
  3. Binds the "jobright" profile — shared with JobrightScraper — so callers MUST
     avoid a concurrent second Chrome on that profile (ProcessSingleton lock, see
     CLAUDE.md). Reuse a live context or serialize; _clear_profile_locks only
     clears STALE locks.
"""
from __future__ import annotations

from rich.console import Console

from ..base import BaseScraper
from .context import AtsApplyContext, AtsApplyResult
from .registry import AtsAdapterRegistry
from .generic import GenericAtsAdapter

console = Console()


def _detect_pre_launch_vendor(external_url: str) -> dict:
    """Cheap URL-only pre-launch hints (before the browser exists).

    Returns hints the session needs to configure the browser — notably whether
    to load the Jobright extension. Full vendor selection happens AFTER navigation
    via the registry (which can read the live DOM); this is only what must be
    known pre-launch.
    """
    url = (external_url or "").lower()
    is_teamtailor = "teamtailor.com" in url
    return {
        "is_teamtailor": is_teamtailor,
        # Teamtailor is handled by a dedicated form-filler; the extension is not
        # loaded for it (mirrors jobright.py:268).
        "load_extensions": not is_teamtailor,
    }


class ExternalApplySession(BaseScraper):
    """Launches Chrome on the jobright profile, navigates to the ATS page, then
    delegates form-filling to the registry-selected adapter."""

    name = "jobright"  # forces state/sessions/jobright_profile (extension lives there)

    def __init__(self, config, registry: AtsAdapterRegistry | None = None):
        super().__init__(config)
        self.registry = registry or AtsAdapterRegistry(fallback=GenericAtsAdapter())

    async def scrape(self, *args, **kwargs):  # BaseScraper abstractmethod; not used here
        raise NotImplementedError("ExternalApplySession does not scrape; it applies.")

    async def apply(self, job: dict, auto_submit: bool = False) -> AtsApplyResult:
        """Parity target for the three migrated call sites (linkedin:1265,
        indeed:397, SOURCE_MAP['external']). Live-verify on the Mac before
        flipping those call sites off JobrightScraper.
        """
        external_url = job.get("url") or job.get("external_url") or ""
        hints = _detect_pre_launch_vendor(external_url)

        # 1. Pre-launch: start Chrome on the jobright profile with the correct
        #    extension setting (decided from the URL, before launch).
        page = await self._start_browser(load_extensions=hints["load_extensions"])

        # 2. Navigate directly to the ATS URL (the :237 path, not the card handoff).
        await page.goto(external_url, wait_until="domcontentloaded", timeout=45000)

        # 3. Build the context with the LIVE page, then let the registry pick.
        ctx = AtsApplyContext(
            page=page,
            job=job,
            profile=getattr(self, "profile", None),
            resume_path=job.get("resume_path"),
            cover_letter_path=job.get("cover_letter_path"),
            auto_submit=auto_submit,
            url=page.url,
        )
        adapter = await self.registry.pick(ctx)
        console.print(f"[cyan]ExternalApplySession:[/cyan] adapter={adapter.name} url={page.url[:80]}")
        return await adapter.apply(ctx)
