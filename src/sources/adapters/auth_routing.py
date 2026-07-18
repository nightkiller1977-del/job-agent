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

import json
import shlex
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
    if action == "prepare_sessions":
        # prepare-sessions filters approved jobs by the job's discovery source
        # (job["source"]) and company, then routes portal-blocked jobs to the
        # external-portal prep flow (needs_external_portal_prep) regardless of
        # source — so this origin-source target does open the ATS portal even for
        # LinkedIn/Indeed-origin jobs. Company names come from scraped listings:
        # quote them so the hint is safe to paste into a shell.
        company = str(job.get("company") or "")
        target = f"--source {shlex.quote(source)}"
        if company:
            target += f" --company {shlex.quote(company)}"
        remediation = (
            f"sign in to the {who} portal for this job: "
            f"python src/main.py prepare-sessions {target}"
        )
    else:
        remediation = "add credentials to .env / re-run to refresh the session"
    return ReauthDirective(
        status=status, vendor=vendor, source=source, action=action,
        reason=f"{who} requires an authenticated session before applying",
        remediation=remediation,
    )


# Readiness classes whose remediation is an interactive ATS-portal login
# (orchestrator._classify_apply_readiness / prepare_sessions vocabulary).
PORTAL_PREP_READINESS = {"needs-session", "needs-portal-login", "needs-review"}


def _job_extra(job: dict | None) -> dict:
    extra = (job or {}).get("extra_json") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    return extra if isinstance(extra, dict) else {}


def external_ats_url(job: dict | None) -> str:
    """Best-known external ATS/portal URL for a job, '' if none was recorded.

    Checks the top-level key first (callers may stamp it), then the persisted
    ``extra_json.ats_url`` that record_apply_attempt stores after an external
    apply attempt discovers the portal URL.
    """
    job = job or {}
    url = str(job.get("ats_url") or "").strip()
    if not url:
        url = str(_job_extra(job).get("ats_url") or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def needs_external_portal_prep(readiness: str, job: dict | None) -> bool:
    """True when session prep for this job must open its external ATS portal
    (in the shared external-apply profile) rather than the discovery source.

    Dispatching prepare-sessions purely on job["source"] only refreshes the ATS
    portal for jobright-origin jobs — LinkedIn/Indeed prepare_session just opens
    LinkedIn/Indeed, so a Workday/Microsoft/etc. auth wall on a job discovered
    there is never refreshed. Route those to the external prep path instead,
    when the block is a portal wall (not the source's own session) and the
    portal URL is known.
    """
    if readiness not in PORTAL_PREP_READINESS:
        return False
    job = job or {}
    source = str(job.get("source") or "").lower()
    if source in ("jobright", "external"):
        return False  # already dispatched to the external-portal prep flow
    last_status = str(_job_extra(job).get("apply_last_status") or "").lower()
    # Statuses named after the discovery source (linkedin_authwall,
    # linkedin_login_required, indeed_*) mean the SOURCE session is the blocker
    # — the source's own prepare_session is the right remediation there.
    if not last_status or (source and last_status.startswith(f"{source}_")):
        return False
    return bool(external_ats_url(job))


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
