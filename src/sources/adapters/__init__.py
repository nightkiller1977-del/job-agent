"""ATS adapter framework (7a contract).

Public contract — import-light (no runtime playwright), safe to import anywhere:
    AtsAdapter, AtsApplyContext, AtsApplyResult, AtsAdapterRegistry, GenericAtsAdapter

ExternalApplySession is NOT exported here — it subclasses BaseScraper (pulls in
playwright), so import it directly (`from .session import ExternalApplySession`)
only where the browser stack is available.
"""
from .base import AtsAdapter
from .context import AtsApplyContext, AtsApplyResult
from .registry import AtsAdapterRegistry
from .generic import GenericAtsAdapter

__all__ = [
    "AtsAdapter",
    "AtsApplyContext",
    "AtsApplyResult",
    "AtsAdapterRegistry",
    "GenericAtsAdapter",
]
