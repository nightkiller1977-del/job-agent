"""7a contract: the AtsAdapter interface.

Two methods, deliberately minimal (see ATS_ADAPTER_PLAN.md Feature 1b for why the
richer 6-method sketch was dropped). Adapters are pure form-fillers: they receive
a live page via AtsApplyContext, they do NOT manage the browser. Submission gating
runs through ctx.policy (no-op until P5b/P6).

Antigravity's P5 (Browser Use recovery) and P6 (greenhouse/lever/ashby apply)
adapters implement this interface and register into AtsAdapterRegistry.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .context import AtsApplyContext, AtsApplyResult


class AtsAdapter(ABC):
    #: stable identifier used in logs, evidence, and registry diagnostics
    name: str = "adapter"

    @abstractmethod
    async def can_handle(self, ctx: AtsApplyContext) -> float:
        """Confidence in [0.0, 1.0] that this adapter should handle ctx.

        Score on ctx.url / ctx.page. Cheap and side-effect-free — the registry
        may call can_handle on every registered adapter to pick the best match.
        Return 0.0 to abstain.
        """
        raise NotImplementedError

    @abstractmethod
    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        """Fill (and, if permitted, submit) the application on ctx.page.

        MUST call `await ctx.policy.confirm_submit(...)` before any final submit
        once a PolicyGate is wired. MUST NOT launch or close the browser.
        """
        raise NotImplementedError
