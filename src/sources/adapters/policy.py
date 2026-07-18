"""Phase 0.3 — the submission policy gate as a SESSION responsibility.

The gate is not adapter convention: `ExternalApplySession` owns a SubmissionPolicy,
puts it on the context, and — crucially — refuses to accept any `submitted` result
that was not authorized through the gate. So even a route that ignores the policy
(e.g. the LLM recovery adapter historically did) cannot launder a submit past it:
the session downgrades an unauthorized "submitted" to `submission_unverified`.

`confirm_submit` records authorization on `ctx.extra["submit_authorized"]` keyed by
the attempt id, and `authorized(ctx)` reads it back — that record is what the session
cross-checks.
"""
from __future__ import annotations

from typing import Any

_AUTH_KEY = "submit_authorized_attempt"


class SubmissionPolicy:
    """Base gate. Default is deny — a no-op policy is still an explicit decision."""

    async def confirm_submit(self, ctx: Any, evidence: dict | None = None) -> bool:
        return False

    def authorized(self, ctx: Any) -> bool:
        """True iff this policy granted authorization for ctx's current attempt."""
        attempt = getattr(ctx, "attempt_id", "") or ""
        return bool(getattr(ctx, "extra", {}).get(_AUTH_KEY) == attempt and attempt)

    # helper for subclasses that decide to grant
    def _grant(self, ctx: Any) -> None:
        attempt = getattr(ctx, "attempt_id", "") or ""
        if attempt:
            ctx.extra[_AUTH_KEY] = attempt


class AutoSubmitPolicy(SubmissionPolicy):
    """Grants submission only when the run explicitly allows it (auto_submit) AND
    the policy itself is configured to allow. Everything else is withheld for human
    review — no autonomous or LLM path submits without passing here."""

    def __init__(self, allow: bool = True):
        self.allow = allow

    async def confirm_submit(self, ctx: Any, evidence: dict | None = None) -> bool:
        approved = bool(self.allow and getattr(ctx, "auto_submit", False))
        if approved:
            self._grant(ctx)
        return approved


class DenyAllPolicy(SubmissionPolicy):
    """Never authorizes a submit. Used for fill-only / dry runs."""

    async def confirm_submit(self, ctx: Any, evidence: dict | None = None) -> bool:
        return False
