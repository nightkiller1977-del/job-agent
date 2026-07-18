"""Phase 3 — route adapter auth-wall outcomes to re-auth / session-prep.

The Workday and CTA adapters report an auth outcome instead of burning a submit
(`workday_session_expired`, `<vendor>_login_required`). This module turns such an
outcome into a machine-actionable `ReauthDirective` and provides a pluggable router.

The actual pipeline seam:
  - These are *portal* sessions (the shared jobright profile), so the remediation is
    usually `prepare-sessions` + a human sign-in — a Workday/Microsoft SSO wall cannot
    be refreshed non-interactively (the legacy path aborts to prepare-sessions too).
  - When the underlying issue is a *scraper* source session (jobright/indeed/linkedin),
    `ManagerReauthRouter` hands off to the existing `reauth.ReauthManager`.

`ExternalApplySession` takes an optional `reauth_router`; the orchestrator wires a
`ManagerReauthRouter(config)` to close the loop. Kept import-light (reauth is imported
lazily) so this module loads anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def is_auth_required(status: str) -> bool:
    s = (status or "").lower()
    return (
        s.endswith("_login_required")
        or s.endswith("_session_expired")
        or s in ("session_expired", "needs_session_prep", "credentials_missing")
    )


def _vendor_of(status: str) -> str:
    s = (status or "").lower()
    if s.endswith("_login_required"):
        return s[: -len("_login_required")]
    if s.endswith("_session_expired"):
        return s[: -len("_session_expired")]
    return ""


@dataclass
class ReauthDirective:
    status: str
    vendor: str          # ATS vendor (workday/microsoft/...) or ""
    source: str          # job source / profile owner (jobright/linkedin/...)
    action: str          # "prepare_sessions" | "reauth"
    reason: str
    remediation: str     # human-runnable command

    def to_dict(self) -> dict:
        return {
            "status": self.status, "vendor": self.vendor, "source": self.source,
            "action": self.action, "reason": self.reason, "remediation": self.remediation,
        }


def directive_for(status: str, job: dict | None = None) -> ReauthDirective | None:
    """Build a re-auth directive for an auth-required status, else None."""
    if not is_auth_required(status):
        return None
    job = job or {}
    source = str(job.get("source") or "jobright")
    vendor = _vendor_of(status)
    # ATS-portal auth walls are refreshed by prepare-sessions (interactive login in the
    # persistent profile); a bare session_expired on a scraper source can try reauth.
    portal = bool(vendor) or status == "needs_session_prep"
    action = "prepare_sessions" if portal else "reauth"
    who = vendor or source
    remediation = (
        f"python src/main.py prepare-sessions --source {source}"
        if action == "prepare_sessions"
        else "add credentials to .env / re-run to refresh the session"
    )
    return ReauthDirective(
        status=status, vendor=vendor, source=source, action=action,
        reason=f"{who} requires an authenticated session before applying",
        remediation=remediation,
    )


class ReauthRouter:
    """Base router — records directives; override `route` to act."""

    def __init__(self):
        self.routed: list[ReauthDirective] = []

    async def route(self, directive: ReauthDirective) -> bool:
        self.routed.append(directive)
        return False


class ManagerReauthRouter(ReauthRouter):
    """Wires scraper-source session issues to the existing ReauthManager; portal
    (prepare_sessions) directives are surfaced for a human (can't be auto-refreshed)."""

    def __init__(self, config):
        super().__init__()
        self.config = config

    async def route(self, directive: ReauthDirective) -> bool:
        await super().route(directive)
        if directive.action == "reauth":
            try:
                from src.reauth import ReauthManager, AUTOMATED_SOURCES
                if directive.source in AUTOMATED_SOURCES:
                    return await ReauthManager(self.config).handle(
                        directive.source, directive.reason, context="apply"
                    )
            except Exception:
                return False
        # prepare_sessions: interactive portal login required — leave for the human
        # (the session already emitted an auth_required event + human-action notice).
        return False
