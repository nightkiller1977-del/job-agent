"""7a contract: registry selection, context/result helpers, fallback behavior.

Pure — exercises the contract with fake adapters, no browser stack required
(the contract modules deliberately avoid a runtime playwright import).
"""
import pytest

from src.sources.adapters import (
    AtsAdapter,
    AtsApplyContext,
    AtsApplyResult,
    AtsAdapterRegistry,
    GenericAtsAdapter,
)


class FakeAdapter(AtsAdapter):
    def __init__(self, name, score, raises=False):
        self.name = name
        self._score = score
        self._raises = raises

    async def can_handle(self, ctx):
        if self._raises:
            raise RuntimeError("boom")
        return self._score

    async def apply(self, ctx):
        return AtsApplyResult.ok(detail=f"applied via {self.name}")


def _ctx(url="https://boards.greenhouse.io/acme/jobs/1"):
    return AtsApplyContext(page=None, job={"url": url}, profile=None, url=url)


@pytest.mark.asyncio
async def test_pick_highest_scorer():
    reg = AtsAdapterRegistry()
    reg.register(FakeAdapter("lever", 0.3))
    reg.register(FakeAdapter("greenhouse", 0.9))
    reg.register(FakeAdapter("workday", 0.5))
    chosen = await reg.pick(_ctx())
    assert chosen.name == "greenhouse"


@pytest.mark.asyncio
async def test_pick_falls_back_when_none_match():
    fallback = GenericAtsAdapter()
    reg = AtsAdapterRegistry(fallback=fallback)
    reg.register(FakeAdapter("lever", 0.0))  # abstains
    chosen = await reg.pick(_ctx())
    assert chosen is fallback


@pytest.mark.asyncio
async def test_pick_raises_when_no_match_and_no_fallback():
    reg = AtsAdapterRegistry()
    reg.register(FakeAdapter("lever", 0.0))
    with pytest.raises(LookupError):
        await reg.pick(_ctx())


@pytest.mark.asyncio
async def test_raising_can_handle_scores_zero_and_does_not_break_selection():
    reg = AtsAdapterRegistry()
    reg.register(FakeAdapter("broken", 0.9, raises=True))  # would win if not for raise
    reg.register(FakeAdapter("greenhouse", 0.4))
    chosen = await reg.pick(_ctx())
    assert chosen.name == "greenhouse"


@pytest.mark.asyncio
async def test_generic_is_weak_fallback_and_outscored_by_real_adapters():
    reg = AtsAdapterRegistry(fallback=GenericAtsAdapter())
    reg.register(GenericAtsAdapter())          # 0.05
    reg.register(FakeAdapter("workday", 0.8))
    chosen = await reg.pick(_ctx())
    assert chosen.name == "workday"


def test_result_helpers():
    ok = AtsApplyResult.ok(atsScore=88)
    assert ok.submitted is True and ok.status == "applied" and ok.analytics["atsScore"] == 88
    blk = AtsApplyResult.blocked("submit_not_found", "no button")
    assert blk.submitted is False and blk.status == "submit_not_found"


def test_contract_imports_without_browser_stack():
    """The contract package must import with no playwright/pypdf present."""
    import importlib
    mod = importlib.import_module("src.sources.adapters")
    assert hasattr(mod, "AtsAdapter") and hasattr(mod, "AtsAdapterRegistry")
