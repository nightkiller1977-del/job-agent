from .generic import GenericAtsAdapter, detect_vendor
from .context import AtsApplyContext, AtsApplyResult

class AshbyAdapter(GenericAtsAdapter):
    """Specialized adapter for Ashby job boards."""
    name = "ashby"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Ashby detected in URL
        return 0.9 if detect_vendor(ctx.url) == "ashby" else 0.0
