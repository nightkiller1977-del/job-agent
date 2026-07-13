from .generic import GenericAtsAdapter, detect_vendor
from .context import AtsApplyContext

class LeverAdapter(GenericAtsAdapter):
    """Specialized adapter for Lever job boards."""
    name = "lever"

    async def can_handle(self, ctx: AtsApplyContext) -> float:
        # Match if Lever detected in URL
        return 0.9 if detect_vendor(ctx.url) == "lever" else 0.0
