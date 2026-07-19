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
}

# Sources fanned out by default discovery (no --source given). Deliberately
# excludes "external", which resolves to JobrightScraper and is only meant for
# explicitly hydrating manually-pasted non-source URLs — including it here would
# scrape Jobright twice per run.
DEFAULT_DISCOVERY_SOURCES = ["jobright", "linkedin", "usajobs", "indeed"]

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
                    await prepare(None)
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
            if needs_external_portal_prep(readiness, job):
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