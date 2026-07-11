from .generic import GenericAtsAdapter, detect_vendor
from .context import AtsApplyContext, AtsApplyResult

class GreenhouseAdapter(GenericAtsAdapter):
    """Specialized adapter for Greenhouse job boards."""
    name = "greenhouse"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Greenhouse token detected in URL
        return 0.9 if detect_vendor(ctx.url) == "greenhouse" else 0.0
