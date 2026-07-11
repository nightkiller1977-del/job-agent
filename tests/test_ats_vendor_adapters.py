import pytest
from src.sources.adapters.context import AtsApplyContext, AtsApplyResult
from src.sources.adapters.greenhouse import GreenhouseAdapter
from src.sources.adapters.lever import LeverAdapter
from src.sources.adapters.ashby import AshbyAdapter
from src.sources.adapters.registry import AtsAdapterRegistry

def _ctx(url):
    return AtsApplyContext(page=None, job={"url": url}, profile=None, url=url)

@pytest.mark.asyncio
async def test_vendor_adapters_matching():
    gh = GreenhouseAdapter()
    lv = LeverAdapter()
    ash = AshbyAdapter()
    
    # Greenhouse match
    assert await gh.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.9
    assert await gh.can_handle(_ctx("https://jobs.lever.co/acme/1")) == 0.0
    
    # Lever match
    assert await lv.can_handle(_ctx("https://jobs.lever.co/acme/1")) == 0.9
    assert await lv.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.0
    
    # Ashby match
    assert await ash.can_handle(_ctx("https://jobs.ashbyhq.com/acme/1")) == 0.9
    assert await ash.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.0

@pytest.mark.asyncio
async def test_registry_picking_with_vendors():
    reg = AtsAdapterRegistry()
    reg.register(GreenhouseAdapter())
    reg.register(LeverAdapter())
    reg.register(AshbyAdapter())
    
    # Check Greenhouse
    chosen = await reg.pick(_ctx("https://boards.greenhouse.io/acme/jobs/1"))
    assert chosen.name == "greenhouse"
    
    # Check Lever
    chosen = await reg.pick(_ctx("https://jobs.lever.co/acme/1"))
    assert chosen.name == "lever"
    
    # Check Ashby
    chosen = await reg.pick(_ctx("https://jobs.ashbyhq.com/acme/1"))
    assert chosen.name == "ashby"
