"""
Orchestrator — coordinates scraping, scoring, review, and application flow.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from .state_manager import StateManager
from .scorer import JobScorer
from .review_queue import run_review_queue, show_summary_table
from .sources.jobright import JobrightScraper
from .sources.linkedin import LinkedInScraper
from .sources.usajobs import USAJobsScraper
from .sources.indeed import IndeedScraper

import logging

from .sources.base import AuthFailedError, JobExpiredError
from .notifier import notify_error, notify_info, notify_warning, record_run_stats
from .reauth import ReauthManager

console = Console()
_log = logging.getLogger(__name__)


SOURCE_MAP = {
    "jobright": JobrightScraper,
    "linkedin": LinkedInScraper,
    "usajobs": USAJobsScraper,
    "indeed":   IndeedScraper,
    "external": JobrightScraper,   # legacy fallback for manually-pasted non-source URLs
}

# Path to the file written by the Claude-in-Chrome MCP scraper
MCP_SCRAPED_FILE = Path(__file__).parent.parent / "state" / "mcp_scraped.json"


class Orchestrator:
    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.state = StateManager(self.config.get("state_db_path", "state/jobs.db"))
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.scorer = JobScorer(config=self.config, api_key=api_key)

    def _load_config(self, path: str) -> dict:
        p = Path(path).expanduser()
        if p.exists():
            with open(p) as f:
                return json.load(f)
        # Try relative to project root
        project_root = Path(__file__).parent.parent
        p2 = project_root / path
        if p2.exists():
            with open(p2) as f:
                return json.load(f)
        console.print(f"[yellow]Warning: config not found at {path}, using defaults.[/yellow]")
        return {}

    # ------------------------------------------------------------------
    # discover command
    # ------------------------------------------------------------------

    async def discover(self, source: Optional[str] = None, no_review: bool = False) -> None:
        """Scrape jobs, score them, save to DB, and run review queue.

        source='mcp' is a special mode: reads from state/mcp_scraped.json
        (written by the Claude-in-Chrome MCP scraper in the Claude Code session).
        All other sources use Playwright scrapers.
        """
        await self.load_credentials_from_dashboard()

        # Hydrate any manual external jobs first
        await self.hydrate_external_jobs()

        # ------ MCP file-based import mode ------
        if source == "mcp":
            await self._discover_from_mcp_file(no_review=no_review)
            return
        if source == "linkedin-saved":
            await self._discover_linkedin_saved(no_review=no_review)
            return

        sources_to_run = [source] if source else list(SOURCE_MAP.keys())

        # Validate source
        for s in sources_to_run:
            if s not in SOURCE_MAP:
                console.print(f"[red]Unknown source: {s}. Valid: {list(SOURCE_MAP.keys())} mcp[/red]")
                return

        all_new_jobs: list[dict] = []

        for src_name in sources_to_run:
            console.rule(f"[bold]Scraping {src_name}[/bold]")
            _log.info("scrape.start source=%s", src_name)
            scraper_cls = SOURCE_MAP[src_name]
            scraper = scraper_cls(self.config)
            t_scrape = time.perf_counter()
            try:
                jobs = await scraper.scrape()
                duration_s = round(time.perf_counter() - t_scrape)
                console.print(f"  Scraped {len(jobs)} raw jobs from {src_name}")
                _log.info("scrape.complete source=%s jobs=%d duration_s=%d status=success", src_name, len(jobs), duration_s)
            except AuthFailedError as auth_exc:
                duration_s = round(time.perf_counter() - t_scrape)
                console.print(f"[yellow]{src_name}: session expired — attempting reauth…[/yellow]")
                _log.warning("scrape.auth_failed source=%s detail=%s duration_s=%d status=auth_failed", src_name, auth_exc.detail, duration_s)
                reauth_mgr = ReauthManager(self.config)
                refreshed = await reauth_mgr.handle(src_name, auth_exc.detail, context="discover")
                if refreshed:
                    t_scrape2 = time.perf_counter()
                    try:
                        scraper2 = SOURCE_MAP[src_name](self.config)
                        jobs = await scraper2.scrape()
                        duration_s2 = round(time.perf_counter() - t_scrape2)
                        console.print(f"  Scraped {len(jobs)} raw jobs from {src_name} (after reauth)")
                        _log.info("scrape.complete source=%s jobs=%d duration_s=%d status=success reauth=true", src_name, len(jobs), duration_s2)
                    except Exception as retry_exc:
                        duration_s2 = round(time.perf_counter() - t_scrape2)
                        console.print(f"[red]{src_name} scrape failed after reauth:[/red] {retry_exc}")
                        _log.error("scrape.failed source=%s error=%s duration_s=%d status=failed", src_name, retry_exc, duration_s2)
                        jobs = []
                else:
                    console.print(f"[red]{src_name} reauth failed — skipping this run.[/red]")
                    _log.error("scrape.reauth_failed source=%s duration_s=%d status=reauth_failed", src_name, duration_s)
                    jobs = []
            except Exception as exc:
                duration_s = round(time.perf_counter() - t_scrape)
                console.print(f"[red]{src_name} scrape failed:[/red] {exc}")
                _log.error("scrape.failed source=%s error=%s duration_s=%d status=failed", src_name, exc, duration_s)
                jobs = []

            # Filter already-seen jobs
            new_jobs = [j for j in jobs if not self.state.already_seen(j["job_id"])]
            console.print(f"  {len(new_jobs)} new (unseen) jobs to score")

            if not new_jobs:
                continue

            # Score jobs
            console.print(f"  Scoring {len(new_jobs)} jobs with Ollama → Claude cascade…")
            _log.info("scoring.start source=%s count=%d", src_name, len(new_jobs))
            t_score = time.perf_counter()
            scored = await self._score_jobs_with_progress(new_jobs)
            score_duration = round(time.perf_counter() - t_score, 1)
            n_approved = sum(1 for j in scored if j.get("status") == "approved")
            n_skipped = sum(1 for j in scored if j.get("status") == "skipped")
            _log.info(
                "scoring.complete source=%s scored=%d approved=%d skipped=%d duration_s=%.1f",
                src_name, len(scored), n_approved, n_skipped, score_duration,
            )

            # Save to DB
            saved = 0
            for job in scored:
                is_new = self.state.upsert_job(job)
                if is_new:
                    saved += 1
            console.print(f"  Saved {saved} jobs to database")
            all_new_jobs.extend(scored)

        if not all_new_jobs:
            console.print("\n[yellow]No new jobs found this run.[/yellow]")
        else:
            console.print(f"\n[green]Total new jobs this run: {len(all_new_jobs)}[/green]")

        # Sync to cloud dashboard (non-fatal)
        await self._sync_to_cloud(all_new_jobs)

        # Run review queue for all pending jobs (includes older unreviewed ones)
        import sys
        if no_review or not (sys.stdin and sys.stdin.isatty()):
            console.print("\n[yellow]Skipping terminal review queue (no-review flag or non-interactive terminal).[/yellow]")
            return

        pending = self.state.get_pending_review()
        if not pending:
            console.print("[yellow]No jobs pending review.[/yellow]")
            return

        console.print(f"\n[bold]Review queue: {len(pending)} jobs[/bold]")
        summary = run_review_queue(pending, self.state)
        console.print(
            f"\n[green]Review complete:[/green] "
            f"{summary['applied']} approved, "
            f"{summary['skipped']} skipped, "
            f"{summary['bookmarked']} bookmarked"
        )

    async def _discover_linkedin_saved(self, no_review: bool = False) -> None:
        """Import LinkedIn saved jobs as approved apply targets."""
        console.rule("[bold blue]Importing LinkedIn saved jobs[/bold blue]")
        scraper = LinkedInScraper(self.config)
        try:
            saved_jobs = await scraper.scrape_saved()
        except Exception as exc:
            console.print(f"[red]LinkedIn saved-job import failed:[/red] {exc}")
            saved_jobs = []

        if not saved_jobs:
            console.print("[yellow]No LinkedIn saved jobs found.[/yellow]")
            return

        inserted = 0
        approved = 0
        for job in saved_jobs:
            job["status"] = "approved"
            job["score"] = max(int(job.get("score") or 0), 100)
            job["score_reason"] = job.get("score_reason") or "User saved this job in LinkedIn; approved regardless of score."
            if self.state.upsert_job(job):
                inserted += 1
            else:
                existing = self.state.get_job(job["job_id"]) or {}
                score = max(int(existing.get("score") or 0), int(job.get("score") or 0), 100)
                self.state.update_job_details(job["job_id"], job)
                self.state.update_score(job["job_id"], score, job["score_reason"], job.get("flags", "linkedin_saved"))
            self.state.set_status(job["job_id"], "approved")
            approved += 1

        console.print(f"[green]LinkedIn saved import complete:[/green] {inserted} new, {approved} approved for apply.")
        await self._sync_to_cloud(saved_jobs)

        if no_review:
            console.print("\n[yellow]Skipping terminal review queue (no-review flag).[/yellow]")
            return

    async def _discover_from_mcp_file(self, no_review: bool = False) -> None:
        """Load jobs scraped by the Claude-in-Chrome MCP tools, score, and review."""
        import hashlib
        console.rule("[bold magenta]Loading MCP-scraped jobs[/bold magenta]")
        if not MCP_SCRAPED_FILE.exists():
            console.print(f"[red]No MCP scraped file found at {MCP_SCRAPED_FILE}[/red]")
            return

        with open(MCP_SCRAPED_FILE) as f:
            raw_jobs = json.load(f)

        if not raw_jobs:
            console.print("[yellow]MCP scraped file is empty.[/yellow]")
            return

        console.print(f"  Loaded {len(raw_jobs)} jobs from MCP scrape")

        # Add job_id if missing
        for job in raw_jobs:
            if "job_id" not in job:
                job["job_id"] = hashlib.md5(job.get("url", job.get("title","")).encode()).hexdigest()[:16]

        new_jobs = [j for j in raw_jobs if not self.state.already_seen(j["job_id"])]
        console.print(f"  {len(new_jobs)} new (unseen) jobs to score")

        if not new_jobs:
            console.print("[yellow]All MCP jobs already seen.[/yellow]")
        else:
            console.print(f"  Scoring {len(new_jobs)} jobs with Claude…")
            scored = await self._score_jobs_with_progress(new_jobs)
            saved = 0
            for job in scored:
                if self.state.upsert_job(job):
                    saved += 1
            console.print(f"  Saved {saved} jobs to database")

        # Review queue
        import sys
        if no_review or not (sys.stdin and sys.stdin.isatty()):
            console.print("\n[yellow]Skipping terminal review queue (no-review flag or non-interactive terminal).[/yellow]")
            return

        pending = self.state.get_pending_review()
        if not pending:
            console.print("[yellow]No jobs pending review.[/yellow]")
            return

        console.print(f"\n[bold]Review queue: {len(pending)} jobs[/bold]")
        summary = run_review_queue(pending, self.state)
        console.print(
            f"\n[green]Review complete:[/green] "
            f"{summary['applied']} approved, "
            f"{summary['skipped']} skipped, "
            f"{summary['bookmarked']} bookmarked"
        )

    async def _score_jobs_with_progress(self, jobs: list[dict]) -> list[dict]:
        """Score jobs with a progress bar."""
        scored = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Scoring…", total=len(jobs))
            for job in jobs:
                scored_job = await self._score_one(job)
                scored.append(scored_job)
                progress.advance(task_id)
        return scored

    async def _score_one(self, job: dict) -> dict:
        score, reason, flags, action = await self.scorer.score(job)
        job["score"] = score
        job["score_reason"] = reason
        job["flags"] = flags
        job["recommended_action"] = action

        # Auto-classify job status based on recommended action for full automation
        if action == "apply":
            job["status"] = "approved"
            console.print(f"  [green]→ Auto-approved: {job.get('title')} @ {job.get('company')} (Score: {score})[/green]")
        elif action == "skip":
            job["status"] = "skipped"
            console.print(f"  [dim]→ Auto-skipped: {job.get('title')} @ {job.get('company')} (Score: {score})[/dim]")
        else:
            job["status"] = "discovered"

        return job

    # ------------------------------------------------------------------
    # apply command
    # ------------------------------------------------------------------

    def _filter_jobs(
        self,
        jobs: list[dict],
        *,
        job_id: Optional[str] = None,
        source: Optional[str] = None,
        company: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        filtered = jobs
        if job_id:
            filtered = [j for j in filtered if j.get("job_id") == job_id]
        if source:
            filtered = [j for j in filtered if j.get("source") == source]
        if company:
            needle = company.lower()
            filtered = [j for j in filtered if needle in (j.get("company") or "").lower()]
        if limit is not None:
            filtered = filtered[: max(0, limit)]
        return filtered

    def _classify_apply_readiness(self, job: dict) -> tuple[str, str]:
        import json as _json
        source = (job.get("source") or "").lower()
        company = (job.get("company") or "").lower()
        url = (job.get("url") or "").lower()
        # Also check any previously-extracted ATS URL stored in extra_json
        try:
            extra = job.get("extra_json") or {}
            if isinstance(extra, str):
                extra = _json.loads(extra)
            ats_url = (extra.get("ats_url") or "").lower()
        except Exception:
            ats_url = ""
        all_urls = url + " " + ats_url

        if source == "usajobs":
            return ("needs-session", "USAJobs requires a signed-in USAJobs/Login.gov browser session and a saved resume.")
        if "https://www./" in all_urls or url in {"", "https://", "http://"}:
            return ("needs-hydration", "Job URL is missing or invalid; hydrate/refresh the job before applying.")

        last_status = ""
        last_detail = ""
        try:
            extra = job.get("extra_json") or {}
            if isinstance(extra, str):
                extra = _json.loads(extra)
            last_status = str(extra.get("apply_last_status") or "")
            last_detail = str(extra.get("apply_last_detail") or "")
        except Exception:
            pass

        # Only block on STATUS CODES THAT SCRAPERS ACTUALLY PRODUCE.
        # "needs-session" and "needs-portal-login" were emitted by old preflight
        # classification code (now removed) and never by scrapers themselves.
        # Blocking on them permanently locked every Workday/BrassRing job after the
        # first preflight run.  They are intentionally excluded here.
        session_statuses = {
            "workday_session_expired",
            "brassring_login_required",
            "microsoft_login_required",
            "linkedin_authwall",
            "linkedin_login_required",
        }
        if last_status in session_statuses:
            return ("needs-session", last_detail or "Portal session or credentials must be refreshed before applying.")
        # "needs-review" was also set by old preflight code; "workday_form_not_detected"
        # means the portal page loaded but looked like a job listing — worth retrying.
        # Only block on "form_not_reached" outcomes that indicate a hard wall.
        hard_blocks = {
            "workday_account_required",
            "brassring_registration_required",
        }
        if last_status in hard_blocks:
            return ("needs-review", last_detail or "This application requires manual account setup before auto-submit.")
        if last_status in {"linkedin_stuck_on_required_field", "required_field_unanswered"}:
            return ("needs-answer", last_detail or "The apply form has a required field the agent cannot answer yet.")
        if last_status == "error" and (
            "err_name_not_resolved" in last_detail.lower()
            or "https://www./" in last_detail.lower()
        ):
            return ("needs-hydration", last_detail or "The application URL could not be resolved.")

        # All other sources: attempt to apply. Each scraper handles auth failures
        # inline and records a specific block reason for the dashboard.
        return ("ready", "")

    async def preflight_approved(self, source: Optional[str] = None, company: Optional[str] = None) -> None:
        """Pull cloud-approved jobs and print production-readiness blockers."""
        await self.load_credentials_from_dashboard()
        await self._pull_approved_from_cloud()
        approved = self._filter_jobs(
            self.state.get_approved_unapplied(),
            source=source,
            company=company,
        )
        if not approved:
            console.print("[yellow]No approved jobs pending application.[/yellow]")
            return

        console.print(f"\n[bold]Approved apply preflight: {len(approved)} job(s)[/bold]")
        blockers: dict[str, int] = {}
        for job in approved:
            readiness, detail = self._classify_apply_readiness(job)
            blockers[readiness] = blockers.get(readiness, 0) + 1
            console.print(
                f"  • {readiness}: {job.get('title')} @ {job.get('company')} "
                f"({job.get('source')}, score={job.get('score')})"
            )
            console.print(f"    [dim]{detail}[/dim]")
        console.print("\n[bold]Preflight summary[/bold]")
        for key, count in sorted(blockers.items()):
            console.print(f"  {key}: {count}")

    async def browser_setup(self) -> None:
        """Install the Jobright Chrome extension into the job-agent profile.

        LinkedIn, Jobright, and Indeed all auto-login from .env credentials
        at run-time — no manual sign-in needed. The only thing that requires
        a one-time browser action is installing the Jobright AI autofill
        extension from the Chrome Web Store. Run this once, install the
        extension, close the window, and every future run picks it up.
        """
        console.print("\n[bold cyan]Job-Agent Browser Setup[/bold cyan]")
        console.print("[dim]LinkedIn/Jobright/Indeed log in automatically from .env — no manual sign-in needed.[/dim]")
        console.print("[dim]Opening Chrome to install the Jobright AI extension from the Web Store...[/dim]\n")

        scraper = JobrightScraper(self.config)
        page = await scraper._start_browser(load_extensions=True)
        ctx = scraper._context

        # Go straight to the Jobright extension on the Chrome Web Store
        await page.goto(
            "https://chromewebstore.google.com/search/jobright%20ai",
            wait_until="domcontentloaded",
        )

        console.print("[green]Browser open.[/green] Click [bold]'Add to Chrome'[/bold] on the Jobright AI extension, then close the window.")

        # Wait until the user closes the browser window
        try:
            while ctx.pages:
                await asyncio.sleep(2)
        except Exception:
            pass

        console.print("[green]Setup complete.[/green] Extension installed — all future apply runs will use it.\n")

    async def prepare_sessions(
        self,
        source: Optional[str] = None,
        company: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        """Open approved jobs that need authenticated ATS sessions.

        This is intentionally separate from apply: the user can refresh Workday,
        Microsoft, or BrassRing cookies once in the persistent browser profile,
        then run apply afterward without each job failing independently.
        """
        await self.load_credentials_from_dashboard()
        await self._pull_approved_from_cloud()
        approved = self._filter_jobs(
            self.state.get_approved_unapplied(),
            source=source,
            company=company,
        )
        session_jobs = []
        for job in approved:
            readiness, _detail = self._classify_apply_readiness(job)
            if readiness in {"needs-session", "needs-portal-login", "needs-review"}:
                session_jobs.append(job)
        if limit is not None:
            session_jobs = session_jobs[: max(0, limit)]

        if not session_jobs:
            if source in SOURCE_MAP:
                scraper = SOURCE_MAP[source](self.config)
                prepare = getattr(scraper, "prepare_session", None)
                if prepare:
                    console.print(f"[yellow]No approved {source} jobs need session preparation; opening the source session instead.[/yellow]")
                    await prepare(None)
                    return
            console.print("[green]No approved jobs need session preparation.[/green]")
            return

        console.print(f"\n[bold]Preparing sessions for {len(session_jobs)} approved job(s)[/bold]")
        console.print("[dim]Sign in or complete portal account prompts in each browser window, then return here.[/dim]")

        for job in session_jobs:
            scraper_cls = SOURCE_MAP.get(job.get("source", ""))
            if not scraper_cls:
                console.print(f"[yellow]Skipping unknown source: {job.get('source')}[/yellow]")
                continue
            scraper = scraper_cls(self.config)
            prepare = getattr(scraper, "prepare_session", None)
            if not prepare:
                console.print(f"[yellow]{job.get('source')} does not support session preparation.[/yellow]")
                continue
            console.rule(f"[bold]{job.get('title')} @ {job.get('company')}[/bold]")
            await prepare(job)

    async def apply_approved(
        self,
        auto_submit: bool = False,
        limit: Optional[int] = None,
        job_id: Optional[str] = None,
        source: Optional[str] = None,
        company: Optional[str] = None,
    ) -> None:
        """Apply to all jobs in 'approved' status.

        Pre-flight: classifies each approved job by portal readiness.
        Session-blocked jobs are skipped in non-interactive (scheduled) runs —
        the block reason is recorded in extra_json and shown in the dashboard.
        Run 'python src/main.py prepare-sessions' to refresh blocked portals,
        then re-run apply.
        """
        import sys as _sys
        is_interactive = bool(_sys.stdin and _sys.stdin.isatty())

        await self.load_credentials_from_dashboard()
        # Pull cloud-approved jobs into local SQLite first
        await self._pull_approved_from_cloud()

        all_approved = self._filter_jobs(
            self.state.get_approved_unapplied(),
            job_id=job_id,
            source=source,
            company=company,
            limit=limit,
        )
        if not all_approved:
            console.print("[yellow]No approved jobs pending application.[/yellow]")
            return

        # ── Pre-flight classification ──────────────────────────────────────────
        # Classify each job so session-blocked jobs don't waste a browser launch.
        BLOCKED_READINESS = {
            "needs-session",
            "needs-portal-login",
            "needs-review",
            "needs-hydration",
            "needs-answer",
        }
        ready: list[dict]   = []
        blocked: list[tuple] = []  # (job, readiness, reason)
        for j in all_approved:
            readiness, reason = self._classify_apply_readiness(j)
            if readiness in BLOCKED_READINESS:
                blocked.append((j, readiness, reason))
            else:
                ready.append(j)

        console.print(f"\n[bold]Apply pre-flight: {len(all_approved)} approved job(s)[/bold]")
        console.print(f"  ✅ Will attempt  : {len(ready)}")
        console.print(f"  \U0001f512 Session needed: {len(blocked)}")

        if blocked:
            console.print("\n[yellow]Session-blocked (skipping in this run):[/yellow]")
            for bj, readiness, reason in blocked:
                console.print(
                    f"  • {readiness}: {bj.get('title','?')[:50]} @ {bj.get('company','?')}"
                )
                console.print(f"    [dim]{reason}[/dim]")
                # Persist so dashboard and future runs can surface the reason
                self.state.record_apply_attempt(bj["job_id"], readiness, reason)
                await self._push_apply_attempt_to_cloud(bj["job_id"])
            if not is_interactive:
                console.print(
                    "\n[cyan]To fix:[/cyan] Run  python src/main.py prepare-sessions\n"
                    "         then re-run apply to process the session-blocked jobs."
                )

        if not ready:
            console.print("\n[yellow]All approved jobs require session prep — nothing to attempt.[/yellow]")
            return

        console.print(f"\n[bold]Applying to {len(ready)} job(s)[/bold]")
        notify_info(
            "Apply run started",
            f"{len(ready)} job(s) ready, {len(blocked)} session-blocked, "
            f"auto_submit={auto_submit}, source={source or 'all'}",
        )

        applied_count  = 0
        failed_count   = 0
        skipped_count  = 0
        outcomes: list[dict] = []

        for job in ready:
            console.rule(f"[bold]{job.get('title')} @ {job.get('company')}[/bold]")
            src = job.get("source", "")

            if src not in SOURCE_MAP:
                console.print(f"[red]Unknown source '{src}' — skipping.[/red]")
                skipped_count += 1
                outcomes.append({"job": job, "status": "skipped", "reason": f"unknown source {src}"})
                self.state.record_apply_attempt(job["job_id"], "unknown_source", f"source '{src}' not in SOURCE_MAP")
                await self._push_apply_attempt_to_cloud(job["job_id"])
                continue

            scraper = SOURCE_MAP[src](self.config)
            try:
                result = await scraper.apply(job, auto_submit=auto_submit)
                if result:
                    self.state.set_status(job["job_id"], "applied")
                    self.state.record_apply_attempt(job["job_id"], "applied", "Application submitted successfully.")
                    # Persist any analytics the scraper collected (atsScore, resumeVersion, etc.)
                    _analytics = getattr(scraper, "_apply_analytics", None)
                    if _analytics:
                        self.state.record_application_analytics(job["job_id"], _analytics)
                    applied_count += 1
                    outcomes.append({"job": job, "status": "applied", "reason": "submitted"})
                    console.print("[green]Applied! Status updated.[/green]")
                    await self._push_status_to_cloud(job["job_id"], "applied")
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                else:
                    reason = getattr(scraper, "last_apply_detail", "") or "not submitted"
                    code   = getattr(scraper, "last_apply_status",  "") or "blocked"
                    console.print(f"[yellow]Application not submitted ({code}) — status unchanged.[/yellow]")
                    if reason:
                        console.print(f"[dim]{reason}[/dim]")
                    # Persist the specific block reason
                    self.state.record_apply_attempt(job["job_id"], code, reason)
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({"job": job, "status": code, "reason": reason})
            except AuthFailedError as auth_exc:
                console.print(f"[yellow]{src} apply: session expired — attempting reauth…[/yellow]")
                reauth_mgr = ReauthManager(self.config)
                refreshed = await reauth_mgr.handle(src, auth_exc.detail, context="apply")
                if refreshed:
                    try:
                        scraper2 = SOURCE_MAP[src](self.config)
                        result = await scraper2.apply(job, auto_submit=auto_submit)
                        if result:
                            self.state.set_status(job["job_id"], "applied")
                            self.state.record_apply_attempt(job["job_id"], "applied", "Submitted after reauth.")
                            applied_count += 1
                            outcomes.append({"job": job, "status": "applied", "reason": "submitted after reauth"})
                            console.print("[green]Applied after reauth! Status updated.[/green]")
                            await self._push_status_to_cloud(job["job_id"], "applied")
                            await self._push_apply_attempt_to_cloud(job["job_id"])
                        else:
                            reason = getattr(scraper2, "last_apply_detail", "") or "not submitted"
                            code   = getattr(scraper2, "last_apply_status",  "") or "blocked"
                            self.state.record_apply_attempt(job["job_id"], code, reason)
                            await self._push_apply_attempt_to_cloud(job["job_id"])
                            skipped_count += 1
                            outcomes.append({"job": job, "status": code, "reason": reason})
                    except Exception as retry_exc:
                        console.print(f"[red]{src} apply failed after reauth:[/red] {retry_exc}")
                        self.state.record_apply_attempt(job["job_id"], "reauth_retry_error", str(retry_exc)[:400])
                        await self._push_apply_attempt_to_cloud(job["job_id"])
                        failed_count += 1
                        outcomes.append({"job": job, "status": "reauth_retry_error", "reason": str(retry_exc)})
                else:
                    self.state.record_apply_attempt(job["job_id"], "reauth_failed", auth_exc.detail[:400])
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({"job": job, "status": "reauth_failed", "reason": auth_exc.detail})
            except JobExpiredError as exc:
                self.state.set_status(job["job_id"], "expired")
                console.print("[red]Job no longer active (expired). Removed from database.[/red]")
                outcomes.append({"job": job, "status": "expired", "reason": str(exc)})
                await self._push_status_to_cloud(job["job_id"], "expired")
            except Exception as exc:
                console.print(f"[red]Apply error for {job.get('title')}:[/red] {exc}")
                self.state.record_apply_attempt(job["job_id"], "error", str(exc)[:400])
                await self._push_apply_attempt_to_cloud(job["job_id"])
                failed_count += 1
                outcomes.append({"job": job, "status": "error", "reason": str(exc)})

        record_run_stats(applied_count, failed_count, skipped_count)
        _log.info(
            "apply.complete applied=%d failed=%d skipped=%d session_blocked=%d",
            applied_count, failed_count, skipped_count, len(blocked),
        )
        console.print(
            f"\n[bold]Apply run complete:[/bold] "
            f"{applied_count} applied, {failed_count} failed, "
            f"{skipped_count} blocked/not submitted, "
            f"{len(blocked)} session-blocked (skipped pre-flight)"
        )
        if outcomes:
            console.print("\n[bold]Outcome details[/bold]")
            for item in outcomes:
                j = item["job"]
                console.print(f"  • {item['status']}: {j.get('title')} @ {j.get('company')}")
                if item.get("reason"):
                    console.print(f"    [dim]{item['reason']}[/dim]")
        if applied_count == 0 and (skipped_count or blocked):
            notify_warning(
                "Apply run: nothing submitted",
                f"0 submitted, {skipped_count} blocked, {len(blocked)} need session prep. "
                f"Run: python src/main.py prepare-sessions",
            )

    def prune_stale_jobs(self, max_age_days: int = 30, dry_run: bool = False) -> dict:
        """Archive jobs that have been sitting in discovered/approved status
        for longer than max_age_days without ever being applied to.

        Most job listings close within 30 days, so age is a reliable proxy for
        unavailability without requiring an expensive browser visit per job.

        Returns a summary dict: { "pruned": [...], "total": N }
        """
        stale = self.state.get_stale_jobs(max_age_days=max_age_days)
        if not stale:
            console.print(
                f"[green]No stale jobs found (none older than {max_age_days} days in discovered/approved).[/green]"
            )
            return {"pruned": [], "total": 0}

        console.print(
            f"\n[bold]Stale job cleanup:[/bold] {len(stale)} job(s) older than {max_age_days} days"
        )
        if dry_run:
            console.print("[yellow]Dry-run — no changes will be made.[/yellow]")

        pruned = []
        for job in stale:
            age_days = 0
            try:
                disc = datetime.fromisoformat(job["discovered_at"].rstrip("Z"))
                age_days = (datetime.utcnow() - disc).days
            except Exception:
                pass
            console.print(
                f"  {'[dim]would archive[/dim]' if dry_run else '[red]archiving[/red]'}: "
                f"{job.get('title')} @ {job.get('company')} "
                f"({job.get('source')}, {age_days}d old, status={job.get('status')})"
            )
            if not dry_run:
                self.state.archive_job(job["job_id"], reason=f"stale>{max_age_days}d")
            pruned.append(job)

        if not dry_run:
            console.print(f"[green]Archived {len(pruned)} stale job(s).[/green]")
        else:
            console.print(f"[yellow]Would archive {len(pruned)} job(s). Run without --dry-run to apply.[/yellow]")

        return {"pruned": pruned, "total": len(pruned)}

    async def _pull_approved_from_cloud(self) -> None:
        """Fetch jobs marked 'approved' on the cloud dashboard and upsert them
        into the local SQLite DB so the apply command can act on them."""
        dashboard_url = os.environ.get("DASHBOARD_URL", "")
        sync_secret = os.environ.get("SYNC_SECRET", "")
        if not dashboard_url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{dashboard_url}/api/jobs/approved",
                    headers={"X-Sync-Secret": sync_secret} if sync_secret else {},
                )
                if r.status_code != 200:
                    console.print(f"[yellow]Cloud pull returned {r.status_code} — skipping.[/yellow]")
                    return
                jobs = r.json()
                if not jobs:
                    return
                pulled = 0
                for job in jobs:
                    # Insert if new, then always force status to "approved"
                    # (upsert_job skips existing rows, so set_status does the update)
                    job["status"] = "approved"
                    self.state.upsert_job(job)
                    self.state.set_status(job["job_id"], "approved")
                    pulled += 1
                console.print(f"[cyan]☁ Pulled {pulled} approved job(s) from cloud dashboard.[/cyan]")
        except Exception as e:
            console.print(f"[dim]Cloud pull failed (non-fatal): {e}[/dim]")
            _log.warning("cloud_sync.pull_failed error=%s", e)
            notify_error("Cloud sync failed: _pull_approved_from_cloud", str(e)[:200])

    async def _push_status_to_cloud(self, job_id: str, status: str) -> None:
        """POST a status update back to the cloud dashboard (non-fatal)."""
        dashboard_url = os.environ.get("DASHBOARD_URL", "")
        sync_secret = os.environ.get("SYNC_SECRET", "")
        if not dashboard_url:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{dashboard_url}/api/action",
                    json={"job_id": job_id, "action": status},
                    headers={"X-Sync-Secret": sync_secret} if sync_secret else {},
                )
                if r.status_code != 200:
                    console.print(f"[dim]Cloud status push returned {r.status_code}[/dim]")
        except Exception as e:
            console.print(f"[dim]Cloud status push failed (non-fatal): {e}[/dim]")
            _log.warning("cloud_sync.push_status_failed error=%s", e)
            notify_error("Cloud sync failed: _push_status_to_cloud", str(e)[:200])

    async def _push_apply_attempt_to_cloud(self, job_id: str) -> None:
        """Sync the latest local apply attempt fields to the dashboard.

        /api/action only changes the job status. The dashboard's approved queue
        displays apply_last_* fields from extra_json, so blocked and failed
        attempts need a normal sync after record_apply_attempt().
        """
        job = self.state.get_job(job_id)
        if not job:
            return
        await self._sync_to_cloud([job])

    # ------------------------------------------------------------------
    # status command
    # ------------------------------------------------------------------

    async def _sync_to_cloud(self, jobs: list[dict]) -> None:
        """POST discovered jobs to the Render dashboard. Non-fatal on any error."""
        dashboard_url = os.environ.get("DASHBOARD_URL", "")
        sync_secret = os.environ.get("SYNC_SECRET", "")
        if not dashboard_url or not jobs:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"{dashboard_url}/api/sync",
                    json=jobs,
                    headers={"X-Sync-Secret": sync_secret},
                )
                if r.status_code == 200:
                    console.print(f"[green]☁ Synced {len(jobs)} jobs to dashboard.[/green]")
                else:
                    console.print(f"[yellow]Dashboard sync returned {r.status_code}[/yellow]")
        except Exception as e:
            console.print(f"[dim]Dashboard sync failed (non-fatal): {e}[/dim]")

    def show_status(self) -> None:
        """Display stats table."""
        stats = self.state.get_stats()
        show_summary_table(stats)

        # Also show today's bookmarks
        bookmarked = self.state.get_jobs_by_status("bookmarked")
        if bookmarked:
            console.print(f"\n[cyan]Bookmarked jobs ({len(bookmarked)}):[/cyan]")
            for j in bookmarked[:10]:
                console.print(f"  • {j['title']} @ {j['company']} — {j['url']}")

    async def load_credentials_from_dashboard(self) -> None:
        """Fetch credentials from the cloud dashboard and populate os.environ.
        Falls back to local env variables if not found or on error.
        """
        dashboard_url = os.environ.get("DASHBOARD_URL", "")
        sync_secret = os.environ.get("SYNC_SECRET", "")
        if not dashboard_url:
            return

        console.print("[cyan]☁ Fetching platform credentials from cloud...[/cyan]")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{dashboard_url}/api/credentials",
                    headers={"X-Sync-Secret": sync_secret} if sync_secret else {},
                )
                if r.status_code == 200:
                    creds = r.json()
                    loaded = []
                    for item in creds:
                        platform = item.get("platform")
                        email = item.get("email")
                        password = item.get("password")
                        if not email or not password:
                            continue
                        if platform == "indeed":
                            os.environ["INDEED_EMAIL"] = email
                            os.environ["INDEED_PASSWORD"] = password
                            loaded.append("Indeed")
                        elif platform == "linkedin":
                            os.environ["LINKEDIN_EMAIL"] = email
                            os.environ["LINKEDIN_PASSWORD"] = password
                            loaded.append("LinkedIn")
                        elif platform == "jobright":
                            os.environ["JOBRIGHT_EMAIL"] = email
                            os.environ["JOBRIGHT_PASSWORD"] = password
                            loaded.append("Jobright")
                        elif platform == "company_portal":
                            os.environ["COMPANY_EMAIL"] = email
                            os.environ["COMPANY_PASSWORD"] = password
                            loaded.append("Company ATS")
                    if loaded:
                        console.print(f"[green]☁ Platform credentials loaded from cloud: {', '.join(loaded)}[/green]")
                    else:
                        console.print("[yellow]☁ No platform credentials configured on cloud.[/yellow]")
                else:
                    console.print(f"[yellow]☁ Cloud credentials pull returned {r.status_code} — using local env fallbacks.[/yellow]")
        except Exception as e:
            console.print(f"[dim]Failed to load credentials from cloud (non-fatal): {e}[/dim]")

    async def hydrate_external_jobs(self) -> None:
        """Fetch unhydrated placeholders from the cloud dashboard, scrape them locally,
        score them with Claude, and sync the results back to the dashboard."""
        await self.load_credentials_from_dashboard()
        dashboard_url = os.environ.get("DASHBOARD_URL", "")
        sync_secret = os.environ.get("SYNC_SECRET", "")
        if not dashboard_url:
            return

        console.print("[cyan]☁ Checking for unhydrated external jobs from cloud...[/cyan]")
        import httpx
        unhydrated = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{dashboard_url}/api/jobs/unhydrated",
                    headers={"X-Sync-Secret": sync_secret} if sync_secret else {},
                )
                if r.status_code == 200:
                    unhydrated = r.json()
        except Exception as e:
            console.print(f"[dim]Failed to pull unhydrated jobs: {e}[/dim]")
            return

        if not unhydrated:
            console.print("[cyan]No unhydrated jobs found.[/cyan]")
            return

        console.print(f"[cyan]Found {len(unhydrated)} unhydrated job(s). Starting local scraper...[/cyan]")

        from .sources.linkedin import LinkedInScraper, _infer_remote_type  # noqa: F401
        from .sources.jobright import JobrightScraper
        from .sources.indeed import IndeedScraper

        jobright_scraper = JobrightScraper(self.config)
        linkedin_scraper = LinkedInScraper(self.config)
        indeed_scraper   = IndeedScraper(self.config)

        hydrated_jobs = []

        try:
            for placeholder in unhydrated:
                job_id = placeholder["job_id"]
                url = placeholder["url"]
                source = placeholder["source"]

                console.print(f"\n[bold cyan]Hydrating {source} job: {url}[/bold cyan]")
                job = {
                    "job_id": job_id,
                    "url": url,
                    "source": source,
                }

                # Route to the right scraper + browser flags per source
                if source == "linkedin":
                    scraper_for_page = linkedin_scraper
                    load_ext = False
                elif source == "indeed":
                    scraper_for_page = indeed_scraper
                    load_ext = False
                else:
                    scraper_for_page = jobright_scraper
                    load_ext = (source != "linkedin")

                page = None
                try:
                    page = await scraper_for_page._start_browser(load_extensions=load_ext)
                    if source == "linkedin":
                        await linkedin_scraper._hydrate_job_detail(page, job)
                    elif source == "indeed":
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await indeed_scraper._delay(2, 3)
                        await indeed_scraper._hydrate_job_detail(page, job)
                    elif source == "jobright":
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await jobright_scraper._delay(2, 3)
                        title = await page.title()
                        if " | Jobright" in title:
                            title = title.replace(" | Jobright", "")
                        job["title"] = title

                        extracted = await page.evaluate("""
                        () => {
                            try {
                                const nd = JSON.parse(document.getElementById('__NEXT_DATA__')?.textContent || '{}');
                                const job = nd?.props?.pageProps?.job || nd?.props?.pageProps?.jobDetail || {};
                                return {
                                    title: job.title || '',
                                    company: job.companyName || job.company || '',
                                    location: job.location || '',
                                    salary: job.salary || '',
                                    description: job.description || ''
                                };
                            } catch(e) { return null; }
                        }
                        """)
                        if extracted and extracted.get("title"):
                            job["title"] = extracted["title"]
                            job["company"] = extracted["company"]
                            job["location"] = extracted["location"]
                            job["salary_raw"] = extracted["salary"]
                            job["description"] = extracted["description"]
                        else:
                            comp_elem = await page.query_selector("[class*='company'], [class*='employer']")
                            if comp_elem:
                                job["company"] = (await comp_elem.inner_text()).strip()
                            desc_elem = await page.query_selector("[class*='description'], [class*='job-detail']")
                            if desc_elem:
                                job["description"] = (await desc_elem.inner_text()).strip()
                    else:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await jobright_scraper._delay(2, 3)
                        job["title"] = await page.title()

                        title_lower = job["title"].lower()
                        for suffix in [" - indeed.com", " | indeed", " - jobs", " jobs at ", " careers at "]:
                            if suffix in title_lower:
                                idx = title_lower.index(suffix)
                                job["title"] = job["title"][:idx].strip()
                                break

                        body_text = await page.evaluate("document.body.innerText")
                        job["description"] = body_text[:3000]

                    # Ensure essential fields
                    if not job.get("title") or job["title"] == "Importing...":
                        job["title"] = f"Job listing ({job_id})"
                    if not job.get("company") or job["company"] == "Pending local agent sync":
                        job["company"] = "Unknown Company"
                    if not job.get("description"):
                        job["description"] = "No description available."

                    # Score via Ollama → Claude cascade
                    console.print(f"Scoring job...")
                    score, reason, flags, action = await self.scorer.score(job)
                    job["score"] = score
                    job["score_reason"] = reason
                    job["flags"] = (flags or "").replace("needs_hydration", "").strip(",")
                    job["recommended_action"] = action
                    job["status"] = "discovered"

                    # Save to local SQLite
                    self.state.upsert_job(job)
                    self.state.set_status(job_id, "discovered")
                    self.state.update_score(job_id, score, reason, job["flags"])
                    self.state.update_job_details(job_id, job)

                    hydrated_jobs.append(job)
                    console.print(f"[green]Successfully hydrated: {job['title']} @ {job['company']} (Score: {score})[/green]")

                except Exception as inner_e:
                    console.print(f"[red]Failed to hydrate job {job_id}: {inner_e}[/red]")
                finally:
                    if page:
                        await scraper_for_page._close_browser()

        finally:
            pass

        if hydrated_jobs:
            console.print(f"[cyan]Syncing {len(hydrated_jobs)} hydrated job(s) back to cloud dashboard...[/cyan]")
            await self._sync_to_cloud(hydrated_jobs)
