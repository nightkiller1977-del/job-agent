"""7a contract: confidence-scored adapter selection.

Replaces the implicit vendor branching inside JobrightScraper (`_click_ats_apply_button`
/ `_detect_portal_family`) with explicit, testable scoring. Highest can_handle()
score wins; falls back to a registered fallback (GenericAtsAdapter) when nothing
scores above the threshold.
"""
from __future__ import annotations

from .base import AtsAdapter
from .context import AtsApplyContext


class AtsAdapterRegistry:
    def __init__(self, fallback: AtsAdapter | None = None, threshold: float = 0.0):
        self._adapters: list[AtsAdapter] = []
        self._fallback = fallback
        self._threshold = threshold

    def register(self, adapter: AtsAdapter) -> AtsAdapter:
        """Register a vendor adapter. Returns it for convenient inline use."""
        self._adapters.append(adapter)
        return adapter

    @property
    def adapters(self) -> list[AtsAdapter]:
        return list(self._adapters)

    async def score_all(self, ctx: AtsApplyContext) -> list[tuple[AtsAdapter, float]]:
        """Score every registered adapter. A raising can_handle scores 0.0 (an
        adapter must never break selection for the others)."""
        scored: list[tuple[AtsAdapter, float]] = []
        for a in self._adapters:
            try:
                s = float(await a.can_handle(ctx))
            except Exception:
                s = 0.0
            scored.append((a, s))
        return scored

    async def pick(self, ctx: AtsApplyContext) -> AtsAdapter:
        """Return the best-matching adapter, or the fallback.

        Raises LookupError only when nothing matches and no fallback is set.
        """
        best: AtsAdapter | None = None
        best_score = self._threshold
        for adapter, score in await self.score_all(ctx):
            if score > best_score:
                best, best_score = adapter, score
        if best is not None:
            return best
        if self._fallback is not None:
            return self._fallback
        raise LookupError(
            "AtsAdapterRegistry.pick: no adapter scored above "
            f"{self._threshold} and no fallback registered"
        )
