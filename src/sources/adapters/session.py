"""7a: ExternalApplySession — owns browser lifecycle for external ATS applies.

Real implementation (live-verify on the Mac — no browser in the CI sandbox). Not
yet wired into the live apply path: LinkedIn/Indeed/`external` still call
JobrightScraper.apply_external_ats_job until P4 flips the call sites over, so this
can be exercised in isolation first.

Layering (see ATS_ADAPTER_PLAN.md → Three-Layer Decomposition):
    Site Scraper → ExternalApplySession → AtsAdapterRegistry.pick(ctx).apply(ctx)

Verified corrections honored here:
  1. Extension load is a PRE-LAUNCH decision keyed off the external URL
     (jobright.py:258-268 sets load_extensions = not is_teamtailor before launch).
  2. Replicates the direct-nav-with-extension path (jobright.py:237), NOT the
     Jobright-card autofill handoff (jobright.py:1534/1602).
  3. Binds the "jobright" profile — shared with JobrightScraper — so callers MUST
     avoid a concurrent second Chrome on that profile (ProcessSingleton lock, see
     CLAUDE.md). Reuse a live context or serialize; _clear_profile_locks only
     clears STALE locks.
"""
from __future__ import annotations

import time
import urllib.parse
import uuid

from rich.console import Console

from ..base import BaseScraper
from ...events import RunLog
from .context import AtsApplyContext, AtsApplyResult
from .registry import AtsAdapterRegistry
from .generic import GenericAtsAdapter, detect_vendor
from .attempt import AttemptPhase
from .policy import AutoSubmitPolicy, SubmissionPolicy
from .idempotency import SubmissionLedger, canonical_key
from .profile_lock import ProfileLock, ProfileLockError
from .auth_routing import directive_for

console = Console()


def _host(url: str) -> str:
    # hostname (NOT netloc) so embedded credentials like user:pass@host never leak
    # into the audit stream.
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _phase_for(res: AtsApplyResult) -> AttemptPhase:
    if res.verified:
        return AttemptPhase.RECEIPT_VERIFIED
    if res.status == "submission_unverified":
        return AttemptPhase.UNKNOWN          # clicked but unconfirmed — never success
    return AttemptPhase.FAILED


def _detect_pre_launch_vendor(external_url: str) -> dict:
    """Cheap URL-only pre-launch hints (before the browser exists).

    Returns hints the session needs to configure the browser — notably whether
    to load the Jobright extension. Full vendor selection happens AFTER navigation
    via the registry (which can read the live DOM); this is only what must be
    known pre-launch.
    """
    url = (external_url or "").lower()
    is_teamtailor = "teamtailor.com" in url
    return {
        "is_teamtailor": is_teamtailor,
        # Teamtailor is handled by a dedicated form-filler; the extension is not
        # loaded for it (mirrors jobright.py:268).
        "load_extensions": not is_teamtailor,
    }


class ExternalApplySession(BaseScraper):
    """Launches Chrome on the jobright profile, navigates to the ATS page, then
    delegates form-filling to the registry-selected adapter."""

    name = "jobright"  # forces state/sessions/jobright_profile (extension lives there)

    def __init__(self, config, registry: AtsAdapterRegistry | None = None,
                 policy: SubmissionPolicy | None = None,
                 ledger: SubmissionLedger | None = None,
                 run_log: RunLog | None = None,
                 dispatcher=None,
                 reauth_router=None):
        super().__init__(config)
        self.registry = registry or self._create_default_registry()
        # Optional policy override; when None a per-call AutoSubmitPolicy is used so
        # the gate tracks the run's auto_submit flag.
        self._policy_override = policy
        self.ledger = ledger if ledger is not None else SubmissionLedger()
        # One run_id spans all applies on this session instance (Phase 2a).
        self.run_log = run_log or RunLog(agent="external_apply")
        # Optional Phase 2b notification dispatcher; None = no notifications.
        self.dispatcher = dispatcher
        # Optional Phase 3 re-auth router; None = auth outcomes are surfaced but not acted on.
        self.reauth_router = reauth_router

    async def _route_auth(self, res: AtsApplyResult, job: dict, attempt_id: str) -> None:
        """If an adapter reported an auth wall, surface it and hand to re-auth routing."""
        directive = directive_for(res.status, job)
        if directive is None:
            return
        self.run_log.emit(
            "auth_required", attempt_id=attempt_id, status=res.status,
            vendor=directive.vendor, source=directive.source, action=directive.action,
        )
        res.analytics["reauth_directive"] = directive.to_dict()
        # human-action notification is emitted by the attempt_finished _maybe_notify path
        if self.reauth_router is not None:
            try:
                refreshed = await self.reauth_router.route(directive)
            except Exception:
                refreshed = False
            if refreshed:
                # ReauthManager.handle() returning True means the session was refreshed
                # and the caller should retry. Preserve that signal instead of dropping
                # it, so the orchestrator re-queues the job this run rather than next.
                res.analytics["reauth_refreshed"] = True
                self.run_log.emit(
                    "reauth_refreshed", attempt_id=attempt_id,
                    source=directive.source, vendor=directive.vendor,
                )

    def _maybe_notify(self, event: str, outcome: str, title: str, message: str = "",
                      key: str | None = None) -> None:
        if self.dispatcher is None:
            return
        try:  # fail-open at the call site too
            self.dispatcher.dispatch_event(event, outcome, title, message, key=key)
        except Exception:
            pass

    def _create_default_registry(self) -> AtsAdapterRegistry:
        from .greenhouse import GreenhouseAdapter
        from .lever import LeverAdapter
        from .ashby import AshbyAdapter
        from .workday import WorkdayAdapter
        from .vendor_cta import (
            MicrosoftAdapter, BrassRingAdapter, SmartRecruitersAdapter, TeamtailorAdapter,
        )

        reg = AtsAdapterRegistry(fallback=GenericAtsAdapter())
        reg.register(GreenhouseAdapter())
        reg.register(LeverAdapter())
        reg.register(AshbyAdapter())
        reg.register(WorkdayAdapter())
        reg.register(MicrosoftAdapter())
        reg.register(BrassRingAdapter())
        reg.register(SmartRecruitersAdapter())
        reg.register(TeamtailorAdapter())
        return reg


    async def scrape(self, *args, **kwargs):  # BaseScraper abstractmethod; not used here
        raise NotImplementedError("ExternalApplySession does not scrape; it applies.")

    async def apply(self, job: dict, auto_submit: bool = False) -> AtsApplyResult:
        """Parity target for the three migrated call sites (linkedin:1265,
        indeed:397, SOURCE_MAP['external']). Live-verify on the Mac before
        flipping those call sites off JobrightScraper.

        Reliability substrate (Phase 0.1-0.4):
          - 0.2 idempotency: a canonical (vendor,url) key gates against duplicate
            applies; a "submit in progress" marker survives a mid-submit crash.
          - 0.3 policy: the session owns the gate and rejects any `submitted`
            result that was not authorized through it.
          - 0.4 lifecycle: an exclusive profile lock (no 2nd Chrome on the shared
            jobright profile) and a guaranteed browser close in `finally`.
        """
        external_url = job.get("url") or job.get("external_url") or ""
        attempt_id = uuid.uuid4().hex
        key = canonical_key(job)
        vendor = detect_vendor(external_url)
        job_id = str(job.get("job_id") or "")
        started = time.time()

        def _event(event: str, phase: AttemptPhase, **extra):
            # host only (never the full URL — it can carry tokens); no PII fields.
            self.run_log.emit(
                event, attempt_id=attempt_id, job_id=job_id, vendor=vendor,
                host=_host(external_url), phase=phase.value,
                duration_ms=int((time.time() - started) * 1000), **extra,
            )

        _event("attempt_started", AttemptPhase.STARTED, auto_submit=auto_submit)

        # --- 0.2 pre-flight duplicate/interrupted checks (before launching Chrome) ---
        if key and self.ledger.already_applied(key):
            _event("duplicate_prevented", AttemptPhase.UNKNOWN, outcome="duplicate_application_prevented")
            return AtsApplyResult.blocked(
                "duplicate_application_prevented",
                f"already applied to {key} — not resubmitting", attempt_id=attempt_id,
            )
        if key and self.ledger.in_progress(key):
            stale = self.ledger.is_stale_in_progress(key)
            _event("submit_in_progress_blocked", AttemptPhase.UNKNOWN,
                   outcome="submit_in_progress", stale=stale)
            self._maybe_notify("submit_in_progress_blocked", "submit_in_progress",
                               f"{vendor}: apply needs attention",
                               "A prior submit is unresolved — review before retrying.",
                               key=f"{key or attempt_id}:submit_in_progress")
            return AtsApplyResult.blocked(
                "submit_in_progress",
                f"a prior submit for {key} is unresolved "
                f"({'stale/crashed' if stale else 'in flight'}) — not resubmitting blindly",
                attempt_id=attempt_id,
            )
        if key and self.ledger.needs_reconciliation(key):
            # A prior attempt clicked submit but no receipt was confirmed — resubmitting
            # blindly risks a duplicate. Hold until reconciled (human/receipt re-check).
            _event("submit_unverified_blocked", AttemptPhase.UNKNOWN,
                   outcome="submit_unverified_unresolved")
            return AtsApplyResult.blocked(
                "submit_unverified_unresolved",
                f"a prior submit for {key} was unconfirmed — reconcile before resubmitting",
                attempt_id=attempt_id,
            )

        policy: SubmissionPolicy = self._policy_override or AutoSubmitPolicy(allow=auto_submit)
        hints = _detect_pre_launch_vendor(external_url)

        # --- 0.4 exclusive profile lock: never a 2nd Chrome on the jobright profile ---
        try:
            lock = await ProfileLock(self._profile_dir).acquire_async()
        except ProfileLockError as e:
            _event("profile_locked", AttemptPhase.FAILED, outcome="profile_locked")
            self._maybe_notify("profile_locked", "profile_locked",
                               f"{vendor}: apply blocked",
                               "Another Chrome holds the profile — close it and retry.",
                               key=f"{key or attempt_id}:profile_locked")
            return AtsApplyResult.blocked(
                "profile_locked", str(e), attempt_id=attempt_id,
            )

        marked = False
        try:
            page = await self._start_browser(load_extensions=hints["load_extensions"])
            await page.goto(external_url, wait_until="domcontentloaded", timeout=45000)
            # a company/external URL may redirect to the real ATS — re-derive the vendor
            # from the navigated URL so post-nav events aren't mislabeled 'generic'.
            vendor = detect_vendor(getattr(page, "url", "") or external_url) or vendor
            _event("form_reached", AttemptPhase.FORM_REACHED)

            ctx = AtsApplyContext(
                page=page,
                job=job,
                profile=getattr(self, "profile", None),
                resume_path=job.get("resume_path"),
                cover_letter_path=job.get("cover_letter_path"),
                auto_submit=auto_submit,
                policy=policy,
                url=page.url,
                attempt_id=attempt_id,
            )

            # A submit may happen -> write the in-progress marker BEFORE it (crash-safe).
            if key and auto_submit:
                self.ledger.begin(key, attempt_id)
                marked = True

            adapter = await self.registry.pick(ctx)
            # selecting an adapter does NOT mean fields were filled — keep the phase at
            # FORM_REACHED so failed attempts don't overstate how far they progressed.
            _event("adapter_selected", AttemptPhase.FORM_REACHED, adapter=adapter.name)
            console.print(f"[cyan]ExternalApplySession:[/cyan] adapter={adapter.name} url={page.url[:80]}")
            res = self._enforce_authorization(await adapter.apply(ctx), ctx, policy)

            # Recovery only on form-completion failure — NOT on 'submission_unverified'
            # (re-driving that risks a duplicate submit).
            if not res.submitted and res.status in ("submit_not_found", "form_not_reached", "blocked"):
                from .recovery_browseruse import BrowserUseRecovery
                _event("recovery_triggered", AttemptPhase.FIELDS_FILLED, after=res.status)
                console.print("[yellow]Triggering Browser Use self-healing recovery...[/yellow]")
                recovery_res = self._enforce_authorization(
                    await BrowserUseRecovery().apply(ctx), ctx, policy
                )
                if recovery_res.submitted:
                    res = recovery_res

            res.attempt_id = attempt_id
            if marked:
                # Only record ambiguity when a submit was ACTUALLY clicked. A pre-submit
                # blocker (login_required, captcha, submit_not_found, review_ready, ...)
                # never clicked submit, so it must release the marker rather than be
                # frozen as unverified and block the job forever.
                if res.verified:
                    self.ledger.complete(key, attempt_id, verified=True)
                elif res.status == "submission_unverified":
                    self.ledger.complete(key, attempt_id, verified=False)
                else:
                    self.ledger.clear(key)
            _event("attempt_finished", _phase_for(res), outcome=res.status, verified=res.verified)
            self._maybe_notify("attempt_finished", res.status,
                               f"{vendor}: {res.status}", res.detail,
                               key=f"{key or attempt_id}:{res.status}")
            await self._route_auth(res, job, attempt_id)
            return res
        except Exception as exc:
            # a browser/adapter crash must still close the attempt in the audit stream,
            # or per-run failure metrics silently undercount real crashes.
            _event("attempt_finished", AttemptPhase.FAILED, outcome="error",
                   error=type(exc).__name__)
            raise
        finally:
            try:
                await self._close_browser()
            except Exception:
                pass
            lock.release()

    @staticmethod
    def _enforce_authorization(res: AtsApplyResult, ctx: AtsApplyContext,
                               policy: SubmissionPolicy) -> AtsApplyResult:
        """Non-bypassable gate: a `submitted` result is only honored if the policy
        authorized submission for this attempt. Any route that submits without
        passing the gate (e.g. an LLM path) is downgraded to unverified."""
        if res.submitted and not policy.authorized(ctx):
            return AtsApplyResult.unverified(
                f"submit reported without policy authorization — downgraded "
                f"(was status={res.status!r})",
                **(res.analytics or {}),
            )
        return res

