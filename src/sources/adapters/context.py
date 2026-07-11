"""7a contract: data carried into/out of an ATS adapter.

Kept import-light on purpose — no runtime `playwright` import (the Page type is
referenced only for type-checking) so the contract modules import anywhere,
including test environments without the browser stack.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # type-only; no runtime dependency on playwright
    from playwright.async_api import Page


@dataclass
class AtsApplyContext:
    """Everything an adapter needs to fill + submit one application.

    Populated by ExternalApplySession AFTER the browser has navigated to the ATS
    page, so `page`/`url` are live when the registry picks and the adapter runs.
    Adapters must NOT launch or close the browser — the session owns lifecycle.
    """
    page: "Page"                       # live Playwright page (post-navigation)
    job: dict                          # job row (title, company, source, url, ...)
    profile: Any                       # ProfileService (7c); dict-like until then
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    auto_submit: bool = False
    policy: Any = None                 # PolicyGate (P5b/P6); no-op until wired
    url: str = ""                      # convenience mirror of page.url at pick time
    extra: dict = field(default_factory=dict)


@dataclass
class AtsApplyResult:
    """Outcome of an adapter's apply(). `status` uses the existing state-manager
    vocabulary (e.g. "applied", "submit_not_found", "workday_session_expired")
    so it flows straight into record_apply_attempt + the P2 classifier."""
    submitted: bool = False
    status: str = "blocked"
    detail: str = ""
    analytics: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, detail: str = "Application submitted.", **analytics) -> "AtsApplyResult":
        return cls(submitted=True, status="applied", detail=detail, analytics=analytics)

    @classmethod
    def blocked(cls, status: str, detail: str = "", **analytics) -> "AtsApplyResult":
        return cls(submitted=False, status=status, detail=detail, analytics=analytics)
