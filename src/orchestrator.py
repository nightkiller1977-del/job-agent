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
from .sources.jobspy_scraper import JobSpyScraper
from .sources.themuse import TheMuseScraper
from .sources.builtin import BuiltInScraper

import logging

from .sources.base import AuthFailedError, JobExpiredError
from .notifier import notify_error, notify_info, notify_warning, record_run_stats, record_reauth_event
from .reauth import ReauthManager, AUTOMATED_SOURCES
from .resume_helper import ATSReadabilityError, KeywordCoverageError, PDFTextLayerError
from .blocker_classifier import should_attempt, classify, needs_preflight_reauth, preflight_reauth_viable
from .session_watchdog import preflight_session_check

console = Console()
_log = logging.getLogger(__name__)


SOURCE_MAP = {
    "jobright": JobrightScraper,
    "linkedin": LinkedInScraper,
    "usajobs": USAJobsScraper,
    "indeed":   IndeedScraper,
    "external": JobrightScraper,   # legacy fallback for manually-pasted non-source URLs
    "jobspy":   JobSpyScraper,
    "glassdoor": JobrightScraper,  # routes external apply
    "ziprecruiter": JobrightScraper,
    "google":   JobrightScraper,
    "themuse":  TheMuseScraper,
    "builtin":  BuiltInScraper,
}

# Sources fanned out by default discovery (no --source given). Deliberately
# excludes "external", which resolves to JobrightScraper and is only meant for
# explicitly hydrating manually-pasted non-source URLs — including it here would
# scrape Jobright twice per run.
DEFAULT_DISCOVERY_SOURCES = ["jobright", "linkedin", "usajobs", "indeed", "jobspy", "themuse", "builtin"]

# Path to the file written by the Claude-in-Chrome MCP scraper
MCP_SCRAPED_FILE = Path(__file__).parent.parent / "state" / "mcp_scraped.json"


class Orchestrator:
    def __init__(self, config_path: str = "config.json"):
        # Single-source secrets resolution (SECRETS.md). Defensively load the project
        # .env — with override=False so it never stomps intentionally-set shell/launchd
        # vars; main.py stays the authoritative override=True load — then let the central
        # AI Commander store fill anything still missing. This path matters because
        # scheduled/launchd runs construct the Orchestrator directly without going through
        # main.load_env(); that gap is what broke reauth before.
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).parent.parent / ".env", override=False)
            from src.secret_store import fill_missing
            fill_missing()
        except Exception:
            pass
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

        sources_to_run = [source] if source else list(DEFAULT_DISCOVERY_SOURCES)

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
        """Score jobs concurrently (via JobScorer.batch_score) with a progress bar."""
        # Coerce to a positive int: a config value of 0/false/None/garbage would
        # otherwise become asyncio.Semaphore(0) inside batch_score and hang the
        # whole discover run. Fall back to the default of 5.
        raw_concurrency = self.config.get("search_settings", {}).get("score_concurrency", 5)
        try:
            concurrency = int(raw_concurrency)
        except (TypeError, ValueError):
            concurrency = 5
        if concurrency < 1:
            concurrency = 5
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Scoring…", total=len(jobs))
            scored = await self.scorer.batch_score(
                jobs,
                concurrency=concurrency,
                on_result=lambda: progress.advance(task_id),
            )
            # Guard: ensure the bar shows complete even if a job settled without
            # firing the callback.
            progress.update(task_id, completed=len(jobs))

        # Classify status after scoring so the per-job log lines don't interleave
        # with the live progress bar.
        for job in scored:
            self._classify_status(job)
        return scored

    def _classify_status(self, job: dict) -> dict:
        """Map a scored job's recommended_action onto its lifecycle status."""
        action = job.get("recommended_action")
        score = job.get("score")
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
        # Also check any previously-extracted ATS URL stored in extra_json, plus the
        # one-shot session-prepared marker that prepare_sessions() stamps (see
        # clear_session_block).
        session_prepared_at = ""
        try:
            extra = job.get("extra_json") or {}
            if isinstance(extra, str):
                extra = _json.loads(extra)
            ats_url = (extra.get("ats_url") or "").lower()
            session_prepared_at = str(extra.get("session_prepared_at") or "")
        except Exception:
            ats_url = ""
        all_urls = url + " " + ats_url

        # A missing/malformed job URL is a precondition no session prep can fix, so
        # check it before the session-prepared marker and the source blocks — a
        # prepared job with a broken URL must still route to hydration rather than
        # launch the scraper against an invalid URL (Codex #57). The marker overrides
        # only the session/portal blocks, not URL validity.
        if "https://www./" in all_urls or url in {"", "https://", "http://"}:
            return ("needs-hydration", "Job URL is missing or invalid; hydrate/refresh the job before applying.")

        # Honor the session-prepared marker before the source-specific session blocks
        # below — including USAJobs, which otherwise returns needs-session
        # unconditionally and could never be made retryable by prepare-sessions
        # (Codex #57). Preparation confirmed the portal session; retry without
        # touching apply_last_status/detail (get_apply_funnel() needs them intact).
        # One-shot: record_apply_attempt() drops the marker on the next attempt, so a
        # still-broken session re-blocks normally.
        if session_prepared_at:
            return ("ready", "Session prepared via prepare-sessions; retrying.")

        if source == "usajobs":
            return ("needs-session", "USAJobs requires a signed-in USAJobs/Login.gov browser session and a saved resume.")

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
            "smartrecruiters_login_required",
            "teamtailor_login_required",
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

    def _mark_confirmation_submitted(self, job_id: str) -> None:
        """Stamp confirmation_status from the real apply-success signal this run just
        observed, so it reflects actual evidence rather than being backfilled later
        from an inferred email match (see EmailConfirmationTracker)."""
        try:
            self.state.transition_confirmation(job_id, "submitting")
            self.state.transition_confirmation(job_id, "submitted")
        except Exception as exc:
            console.print(f"[dim]confirmation_status bookkeeping skipped for {job_id}: {exc}[/dim]")

    def _apply_validation_metadata(self, scraper=None, exc: Exception | None = None) -> dict:
        metrics = getattr(scraper, "_apply_validation_metrics", None) if scraper else None
        if not metrics and exc is not None:
            result = getattr(exc, "result", None)
            if result:
                metrics = {
                    "passed": result.passed,
                    "coverage": result.coverage,
                    "matched_keywords": result.matched_keywords,
                    "unmatched_keywords": result.unmatched_keywords,
                    "failure_type": result.failure_type or "",
                    "detail": result.detail or str(exc),
                }
        md = {"apply_validation_metrics": metrics} if metrics else {}
        # Persist the external ATS portal URL discovered during the attempt so
        # prepare-sessions can open the portal directly for LinkedIn/Indeed-origin
        # jobs (needs_external_portal_prep reads extra_json.ats_url).
        ats_url = getattr(scraper, "last_apply_ats_url", "") if scraper else ""
        if ats_url:
            md["ats_url"] = ats_url
        return md

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
        session_jobs = []  # (job, readiness)
        for job in approved:
            readiness, _detail = self._classify_apply_readiness(job)
            if readiness in {"needs-session", "needs-portal-login", "needs-review"}:
                session_jobs.append((job, readiness))
        if limit is not None:
            session_jobs = session_jobs[: max(0, limit)]

        if not session_jobs:
            if source in SOURCE_MAP:
                scraper = SOURCE_MAP[source](self.config)
                prepare = getattr(scraper, "prepare_session", None)
                if prepare:
                    console.print(f"[yellow]No approved {source} jobs need session preparation; opening the source session instead.[/yellow]")
                    prepared = await prepare(None)
                    if prepared and source in AUTOMATED_SOURCES:
                        record_reauth_event(source, "human", "success", "manually prepared via prepare-sessions")
                    return
            console.print("[green]No approved jobs need session preparation.[/green]")
            return

        console.print(f"\n[bold]Preparing sessions for {len(session_jobs)} approved job(s)[/bold]")
        console.print("[dim]Sign in or complete portal account prompts in each browser window, then return here.[/dim]")

        from .sources.adapters.auth_routing import needs_external_portal_prep

        for job, readiness in session_jobs:
            # Portal-blocked jobs (needs-session/needs-portal-login with a known
            # external ATS URL) must open the ATS portal in the shared
            # external-apply profile regardless of discovery source: LinkedIn's
            # and Indeed's prepare_session only refresh their own site session,
            # which never clears a Workday/Microsoft/BrassRing auth wall.
            routed_to_external_portal = needs_external_portal_prep(readiness, job)
            if routed_to_external_portal:
                scraper_cls = SOURCE_MAP["jobright"]
                console.print(
                    f"[cyan]Routing {job.get('source')} job to external ATS portal prep.[/cyan]"
                )
            else:
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
            prepared = await prepare(job)
            # Mark the job retryable — otherwise apply_approved() keeps classifying
            # it as needs-session/needs-portal-login/needs-review forever (Codex #56
            # P1). prepare_session() returns True only when the session is confirmed
            # ready to clear: verified without human input (auto-login / already
            # authenticated — safe even under launchd, Codex #57 P2b) or an
            # interactive human completed the login. It returns False when it bailed
            # early (no ATS URL, Codex #57 P2a) or a non-TTY run left a login wall
            # unresolved (Codex #57 P1) — so this single check covers all of them.
            if job.get("job_id") and prepared:
                self.state.clear_session_block(job["job_id"])
                await self._push_apply_attempt_to_cloud(job["job_id"])
                job_source = job.get("source", "")
                # Only reset job_source's own reauth circuit breaker when its
                # own login session is what got prepared.
                #
                # routed_to_external_portal covers LinkedIn/Indeed-origin jobs
                # explicitly routed to Jobright's external-portal flow —
                # job_source's own session was never touched there.
                #
                # job_source == "jobright" needs its own exclusion:
                # needs_external_portal_prep() returns False for jobright-origin
                # jobs (it treats them as "already on that flow" rather than
                # routing them), but JobrightScraper.prepare_session(job) — when
                # given a job, as opposed to the job=None base-session-refresh
                # path — ALWAYS opens the external ATS portal (Workday/Microsoft/
                # etc.) for that job, never verifies Jobright's own login. Every
                # status that lands a job in this loop (workday_session_expired,
                # brassring_login_required, microsoft_login_required, ...) is an
                # external-ATS-wall status, not a Jobright-login status, so this
                # is not a hypothetical case. (Codex review, PR #81.)
                own_source_login_prepared = not routed_to_external_portal and job_source != "jobright"
                if job_source in AUTOMATED_SOURCES and own_source_login_prepared:
                    record_reauth_event(job_source, "human", "success", "manually prepared via prepare-sessions")

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
        self._log_credential_presence()
        # Pull cloud-approved jobs into local SQLite first
        await self._pull_approved_from_cloud()

        all_approved = self._filter_jobs(
            self.state.get_approved_unapplied(),
            job_id=job_id,
            source=source,
            company=company,
            limit=limit,
        )
        min_apply_score = int(self.config.get("search_settings", {}).get("min_apply_score", 0))
        if min_apply_score > 0:
            valid_approved = []
            for j in all_approved:
                s = j.get("score")
                if s is not None and isinstance(s, (int, float)) and s < min_apply_score:
                    console.print(f"[dim]Auto-skipping low-score job ({s} < {min_apply_score}): {j.get('title')} @ {j.get('company')}[/dim]")
                    self.state.set_status(j["job_id"], "skipped")
                    try:
                        await self._push_status_to_cloud(j["job_id"], "skipped")
                    except Exception:
                        pass
                else:
                    valid_approved.append(j)
            all_approved = valid_approved

        if not all_approved:
            console.print("[yellow]No approved jobs meeting minimum score pending application.[/yellow]")
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
            blocked_sources = set()
            for bj, readiness, reason in blocked:
                console.print(
                    f"  • {readiness}: {bj.get('title','?')[:50]} @ {bj.get('company','?')}"
                )
                console.print(f"    [dim]{reason}[/dim]")
                blocked_sources.add(bj.get("source", ""))
                # A preflight block is not an application attempt. Preserve any
                # concrete portal status (for example workday_session_expired) so
                # prepare-sessions can still select and route this job on the next
                # run; replacing it with the generic readiness label strands the
                # recovery flow before a browser is opened.
                if readiness in {"needs-session", "needs-portal-login", "needs-review"}:
                    self.state.record_preflight_block(bj["job_id"], readiness, reason)
                else:
                    self.state.record_apply_attempt(bj["job_id"], readiness, reason)
                await self._push_apply_attempt_to_cloud(bj["job_id"])
            # Emit deep-link notifications for each blocked source
            if not is_interactive and blocked_sources:
                preflight_session_check(list(blocked_sources))
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
        reauthed_this_run: set[str] = set()  # P3: reauth each source at most once per run
        apply_reauth_mgr = ReauthManager(self.config)

        for job in ready:
            console.rule(f"[bold]{job.get('title')} @ {job.get('company')}[/bold]")

            # P2 circuit breaker: stop burning attempts on jobs that have exhausted
            # their retry budget for their blocker class (baseline: 245 wasted retries,
            # some jobs attempted 17×). Reads the prior outcome; does not run apply.
            try:
                _extra = json.loads(job.get("extra_json") or "{}")
            except Exception:
                _extra = {}
            _last = _extra.get("apply_last_status")
            _attempts = int(_extra.get("apply_attempt_count", 0) or 0)
            # clear_session_block() stamped this after a human signed in to the job's
            # portal via prepare-sessions. It's a one-shot bypass of the downstream
            # auth gates below (Codex #57 P1): record_apply_attempt() drops the flag
            # on the very next attempt, so a still-broken session falls straight back
            # under normal gating and can't loop. apply_last_status/count are left
            # intact for telemetry, which is exactly why these gates can't see the
            # prep on their own.
            _session_prepared = bool(_extra.get("session_prepared_at"))
            _ok, _skip_reason = should_attempt(_last, _attempts)
            if not _ok and not _session_prepared:
                _cls = classify(_last).value
                console.print(f"[dim]⛔ Circuit breaker: skipping — {_skip_reason}[/dim]")
                self.state.flag_circuit_break(job["job_id"], _cls, _skip_reason)
                skipped_count += 1
                outcomes.append({"job": job, "status": "circuit_open", "reason": _skip_reason})
                continue
            if not _ok and _session_prepared:
                console.print(
                    "[cyan]Retrying past the circuit breaker — session prepared via prepare-sessions.[/cyan]"
                )

            src = job.get("source", "")

            # P3 session/auth preflight: if this job last failed on an auth blocker,
            # refresh the source session BEFORE attempting (once per source per run),
            # so we don't burn another attempt hitting the same expired session.
            # Skip when the session was just prepared: the human already refreshed the
            # (possibly external ATS) session, and reauthing the discovery source here
            # could clobber it / record credentials_missing without running the scraper.
            if src in SOURCE_MAP and not _session_prepared and needs_preflight_reauth(_last, src, reauthed_this_run):
                reauthed_this_run.add(src)
                # Guard: don't trigger a doomed reauth (human-login sources block on a
                # timeout; automated sources with missing creds just error). Skip cleanly
                # with an actionable status instead of a 10-min block / scary notification.
                _viable, _why = preflight_reauth_viable(src)
                if not _viable:
                    console.print(
                        f"[yellow]P3 preflight: skipping {src} — {_why} "
                        f"({'run prepare-sessions' if _why == 'needs_session_prep' else 'add credentials to .env'}).[/yellow]"
                    )
                    self.state.record_apply_attempt(job["job_id"], _why, f"P3 preflight: {_why}")
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({"job": job, "status": _why, "reason": f"P3 preflight: {_why}"})
                    continue

                console.print(f"[cyan]P3 preflight:[/cyan] prior auth blocker ({_last}) — refreshing {src} session…")
                try:
                    _refreshed = await apply_reauth_mgr.handle(src, _last or "", context="apply")
                except Exception as _re:
                    _refreshed = False
                    console.print(f"[yellow]P3 preflight reauth error for {src}:[/yellow] {_re}")
                if not _refreshed:
                    console.print(f"[yellow]P3 preflight: {src} session not refreshed — skipping this job.[/yellow]")
                    self.state.record_apply_attempt(job["job_id"], "reauth_failed", "P3 preflight reauth failed")
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({"job": job, "status": "reauth_failed", "reason": "preflight reauth failed"})
                    continue

            if src not in SOURCE_MAP:
                console.print(f"[red]Unknown source '{src}' — skipping.[/red]")
                skipped_count += 1
                outcomes.append({"job": job, "status": "skipped", "reason": f"unknown source {src}"})
                self.state.record_apply_attempt(job["job_id"], "unknown_source", f"source '{src}' not in SOURCE_MAP")
                await self._push_apply_attempt_to_cloud(job["job_id"])
                continue

            scraper = SOURCE_MAP[src](self.config)
            try:
                # One-shot same-run retry: when the adapter path reports it refreshed a
                # session mid-attempt (analytics["reauth_refreshed"], set by
                # ExternalApplySession._route_auth), the blocked attempt is retried
                # immediately instead of dead-ending until the next scheduled run.
                for _reauth_pass in range(2):
                    result = await scraper.apply(job, auto_submit=auto_submit)
                    if result:
                        break
                    _an = getattr(scraper, "_apply_analytics", None) or {}
                    if _reauth_pass == 0 and _an.get("reauth_refreshed"):
                        console.print(
                            "[cyan]Session refreshed by re-auth — retrying this job in the same run.[/cyan]"
                        )
                        continue
                    break
                if result:
                    self.state.set_status(job["job_id"], "applied")
                    self._mark_confirmation_submitted(job["job_id"])
                    self.state.record_apply_attempt(
                        job["job_id"],
                        "applied",
                        "Application submitted successfully.",
                        metadata=self._apply_validation_metadata(scraper),
                    )
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
                    self.state.record_apply_attempt(
                        job["job_id"],
                        code,
                        reason,
                        metadata=self._apply_validation_metadata(scraper),
                    )
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({"job": job, "status": code, "reason": reason})
            except AuthFailedError as auth_exc:
                console.print(f"[yellow]{src} apply: session expired — attempting reauth…[/yellow]")
                if src in reauthed_this_run:
                    console.print(
                        f"[yellow]{src} reauth already attempted this run — skipping duplicate notification.[/yellow]"
                    )
                    self.state.record_apply_attempt(
                        job["job_id"],
                        "reauth_failed",
                        f"Reauth already attempted for {src} this run; skipping duplicate notification.",
                    )
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    skipped_count += 1
                    outcomes.append({
                        "job": job,
                        "status": "reauth_failed",
                        "reason": f"reauth already attempted for {src} this run",
                    })
                    continue
                reauthed_this_run.add(src)
                refreshed = await apply_reauth_mgr.handle(src, auth_exc.detail, context="apply")
                if refreshed:
                    try:
                        scraper2 = SOURCE_MAP[src](self.config)
                        result = await scraper2.apply(job, auto_submit=auto_submit)
                        if result:
                            self.state.set_status(job["job_id"], "applied")
                            self._mark_confirmation_submitted(job["job_id"])
                            self.state.record_apply_attempt(
                                job["job_id"],
                                "applied",
                                "Submitted after reauth.",
                                metadata=self._apply_validation_metadata(scraper2),
                            )
                            applied_count += 1
                            outcomes.append({"job": job, "status": "applied", "reason": "submitted after reauth"})
                            console.print("[green]Applied after reauth! Status updated.[/green]")
                            await self._push_status_to_cloud(job["job_id"], "applied")
                            await self._push_apply_attempt_to_cloud(job["job_id"])
                        else:
                            reason = getattr(scraper2, "last_apply_detail", "") or "not submitted"
                            code   = getattr(scraper2, "last_apply_status",  "") or "blocked"
                            self.state.record_apply_attempt(
                                job["job_id"],
                                code,
                                reason,
                                metadata=self._apply_validation_metadata(scraper2),
                            )
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
            except ATSReadabilityError as exc:
                if isinstance(exc, KeywordCoverageError):
                    reason = f"Keyword coverage ({exc.result.coverage*100:.1f}%) is below 65% threshold. Doesn't meet criteria."
                    self.state.set_status(job["job_id"], "skipped")
                    self.state.update_score(job["job_id"], job.get("score", 0), reason, job.get("flags", ""))
                    self.state.record_apply_attempt(
                        job["job_id"],
                        "skipped",
                        reason[:400],
                        metadata=self._apply_validation_metadata(scraper, exc),
                    )
                    await self._push_status_to_cloud(job["job_id"], "skipped")
                    await self._push_apply_attempt_to_cloud(job["job_id"])
                    console.print(f"[red]Job marked as skipped because keyword coverage is below 65% (Doesn't meet criteria):[/red] {job.get('title')}")
                    skipped_count += 1
                    outcomes.append({"job": job, "status": "skipped", "reason": reason})
                    continue

                _is_unreadable = isinstance(exc, PDFTextLayerError) or "no extractable text layer" in str(exc).lower() or "failed to parse" in str(exc).lower()
                status = "pdf_text_layer_failed" if isinstance(exc, PDFTextLayerError) else "ats_failure"
                console.print(f"[red]ATS Readability Failure for {job.get('title')} ({status}):[/red] {exc}")
                self.state.record_apply_attempt(
                    job["job_id"],
                    status,
                    str(exc)[:400],
                    metadata=self._apply_validation_metadata(scraper, exc),
                )
                await self._push_apply_attempt_to_cloud(job["job_id"])
                notify_error("ATS Readability Failure", f"Job ID {job.get('job_id')} failed ATS check ({status}): {exc}")
                failed_count += 1
                outcomes.append({"job": job, "status": status, "reason": str(exc)})
                if _is_unreadable:
                    # Unreadable PDF (image-only or corrupt) — pause the whole loop; self-healing required.
                    console.print("[bold yellow]Pausing application loop: PDF is unreadable. Self-healing/repair required.[/bold yellow]")
                    break
                # General ATS mismatch — skip this job only; continue applying to others.
                console.print("[yellow]Skipping this job due to ATS failure; continuing with remaining jobs.[/yellow]")
                continue
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

    def reset_failures(self, reason: str, dry_run: bool = False) -> None:
        """Reset selected failed approved jobs so the apply circuit can retry them."""
        if reason != "keyword-validation":
            console.print(f"[red]Unsupported reset reason:[/red] {reason}")
            return
        stats = self.state.reset_failed_keyword_jobs(dry_run=dry_run)
        mode = "would reset" if dry_run else "reset"
        console.print(
            f"[green]Failure reset complete:[/green] matched={stats['matched']} "
            f"{mode}={stats['reset'] if not dry_run else stats['matched']} "
            f"unmatched={stats['unmatched']}"
        )

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
        """Display stats table, reconciling ledger states for active jobs."""
        try:
            self.state.reconcile_active_jobs_from_ledger()
        except Exception:
            pass
        stats = self.state.get_stats()
        show_summary_table(stats)

        # Also show today's bookmarks
        bookmarked = self.state.get_jobs_by_status("bookmarked")
        if bookmarked:
            console.print(f"\n[cyan]Bookmarked jobs ({len(bookmarked)}):[/cyan]")
            for j in bookmarked[:10]:
                console.print(f"  • {j['title']} @ {j['company']} — {j['url']}")

    def show_apply_stats(self) -> dict:
        """P1: print the apply funnel + success rate from persisted data.

        Works on existing data — no new run required. Returns the funnel dict
        so callers (tests, cloud push) can consume it too.
        """
        from rich.table import Table

        try:
            self.state.reconcile_active_jobs_from_ledger()
        except Exception:
            pass

        f = self.state.get_apply_funnel()
        rate_pct = f["attempt_success_rate"] * 100

        console.rule("[bold]Apply Success Report[/bold]")
        console.print(
            f"Discovered: [bold]{f['total_jobs']}[/bold]   "
            f"Attempts: [bold]{f['attempts']}[/bold]   "
            f"Submitted: [bold]{f['submitted']}[/bold]   "
            f"Success rate: [bold]{rate_pct:.1f}%[/bold]   "
            f"Wasted retries: [bold]{f['wasted_retries']}[/bold]"
        )

        sc = f["status_counts"]
        if sc:
            console.print(
                "[dim]Funnel: "
                + " → ".join(
                    f"{k}={v}"
                    for k, v in sorted(sc.items(), key=lambda x: -x[1])
                )
                + "[/dim]"
            )

        if f["failure_clusters"]:
            ct = Table(title="Failure clusters", show_edge=False)
            ct.add_column("cluster")
            ct.add_column("count", justify="right")
            for k, v in f["failure_clusters"].items():
                ct.add_row(k, str(v))
            console.print(ct)

        if f["failure_histogram"]:
            ft = Table(title="Failure detail", show_edge=False)
            ft.add_column("status")
            ft.add_column("count", justify="right")
            for k, v in f["failure_histogram"].items():
                ft.add_row(k, str(v))
            console.print(ft)

        if f["per_source"]:
            st = Table(title="Per source", show_edge=False)
            st.add_column("source")
            st.add_column("attempts", justify="right")
            st.add_column("submitted", justify="right")
            st.add_column("rate", justify="right")
            for src, d in sorted(f["per_source"].items(), key=lambda x: -x[1]["attempts"]):
                st.add_row(src, str(d["attempts"]), str(d["submitted"]), f"{d['rate']*100:.0f}%")
            console.print(st)

        return f

    def _log_credential_presence(self) -> None:
        """BAND-AID / TODO(secrets): log which source credentials are present in
        os.environ (names + SET/MISSING, never values) at the start of a run. Turns
        'is JOBRIGHT_EMAIL actually loaded in the scheduled process?' from a guess into
        a fact in the log. Remove once the single-source secrets store lands (roadmap).
        """
        pairs = {
            "jobright": ("JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD"),
            "linkedin": ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"),
            "indeed":   ("INDEED_EMAIL", "INDEED_PASSWORD"),
            "usajobs":  ("USAJOBS_EMAIL", "USAJOBS_PASSWORD"),
            "anthropic": ("ANTHROPIC_API_KEY", None),
        }
        parts = []
        for name, (ek, pk) in pairs.items():
            ok = bool(os.environ.get(ek)) and (pk is None or bool(os.environ.get(pk)))
            parts.append(f"{name}={'SET' if ok else 'MISSING'}")
        console.print(f"[dim]🔑 Credential presence: {'  '.join(parts)}[/dim]")

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
                    kept_local = []
                    # BAND-AID / TODO(secrets): .env is the authoritative source. The cloud
                    # dashboard may only FILL MISSING creds — never overwrite a value already
                    # present locally. A stale/empty cloud value previously clobbered good
                    # .env creds and broke reauth (jobright/usajobs). Replace this whole
                    # multi-source scheme with the single-source secrets store
                    # (SOPS + age + Azure Key Vault) — see roadmap.
                    _platform_keys = {
                        "indeed":        ("INDEED_EMAIL",   "INDEED_PASSWORD",   "Indeed"),
                        "linkedin":      ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "LinkedIn"),
                        "jobright":      ("JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD", "Jobright"),
                        "company_portal":("COMPANY_EMAIL",  "COMPANY_PASSWORD",  "Company ATS"),
                    }
                    for item in creds:
                        email = item.get("email")
                        password = item.get("password")
                        keys = _platform_keys.get(item.get("platform"))
                        if not email or not password or not keys:
                            continue
                        email_key, pw_key, label = keys
                        # .env wins: only fill when BOTH local values are empty/absent
                        if os.environ.get(email_key) or os.environ.get(pw_key):
                            kept_local.append(label)
                            continue
                        os.environ[email_key] = email
                        os.environ[pw_key] = password
                        loaded.append(label)
                    if loaded:
                        console.print(f"[green]☁ Cloud filled missing credentials: {', '.join(loaded)}[/green]")
                    if kept_local:
                        console.print(f"[dim]☁ Kept local .env credentials (cloud not applied): {', '.join(kept_local)}[/dim]")
                    if not loaded and not kept_local:
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
