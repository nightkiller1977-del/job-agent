"""
Orchestrator — coordinates scraping, scoring, review, and application flow.
"""
from __future__ import annotations

import asyncio
import json
import os
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

from .sources.base import JobExpiredError
from .notifier import notify_info, notify_warning, record_run_stats

console = Console()


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
        self.scorer = JobScorer(api_key=api_key)

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
            scraper_cls = SOURCE_MAP[src_name]
            scraper = scraper_cls(self.config)
            try:
                jobs = await scraper.scrape()
                console.print(f"  Scraped {len(jobs)} raw jobs from {src_name}")
            except Exception as exc:
                console.print(f"[red]{src_name} scrape failed:[/red] {exc}")
                jobs = []

            # Filter already-seen jobs
            new_jobs = [j for j in jobs if not self.state.already_seen(j["job_id"])]
            console.print(f"  {len(new_jobs)} new (unseen) jobs to score")

            if not new_jobs:
                continue

            # Score jobs
            console.print(f"  Scoring {len(new_jobs)} jobs with Claude…")
            scored = await self._score_jobs_with_progress(new_jobs)

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
                # Run scoring in executor to avoid blocking event loop
                loop = asyncio.get_event_loop()
                scored_job = await loop.run_in_executor(None, self._score_one, job)
                scored.append(scored_job)
                progress.advance(task_id)
        return scored

    def _score_one(self, job: dict) -> dict:
        score, reason, flags, action = self.scorer.score(job)
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
        source = (job.get("source") or "").lower()
        company = (job.get("company") or "").lower()
        url = (job.get("url") or "").lower()
        if source == "linkedin":
            return ("needs-session", "LinkedIn Easy Apply requires a fresh authenticated LinkedIn browser session.")
        if source == "usajobs":
            return ("needs-session", "USAJobs requires a signed-in USAJobs/Login.gov browser session and a saved resume.")
        if "myworkdayjobs.com" in url:
            return ("needs-session", "Workday jobs require a fresh authenticated browser profile before apply can complete.")
        if "brassring.com" in url or "lockheed" in company:
            return ("needs-portal-login", "BrassRing often requires company portal login before submit is reachable.")
        if "microsoft.com" in url or "microsoft" in company:
            return ("needs-review", "Microsoft portal may require manual account/session review before final submit.")
        if any(name in company for name in ["cvs", "citi"]):
            return ("needs-session", "Likely Workday-backed company; verify session before apply.")
        return ("unknown", "No known blocker detected, but ATS still needs runtime verification.")

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
                f"  • [{readiness}] {job.get('title')} @ {job.get('company')} "
                f"({job.get('source')}, score={job.get('score')})"
            )
            console.print(f"    [dim]{detail}[/dim]")
        console.print("\n[bold]Preflight summary[/bold]")
        for key, count in sorted(blockers.items()):
            console.print(f"  {key}: {count}")

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
        BLOCKED_READINESS = {"needs-session", "needs-portal-login", "needs-review"}
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
                    f"  • [{readiness}] {bj.get('title','?')[:50]} @ {bj.get('company','?')}"
                )
                console.print(f"    [dim]{reason}[/dim]")
                # Persist so dashboard and future runs can surface the reason
                self.state.record_apply_attempt(bj["job_id"], readiness, reason)
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
                continue

            scraper = SOURCE_MAP[src](self.config)
            try:
                result = await scraper.apply(job, auto_submit=auto_submit)
                if result:
                    self.state.set_status(job["job_id"], "applied")
                    self.state.record_apply_attempt(job["job_id"], "applied", "Application submitted successfully.")
                    applied_count += 1
                    outcomes.append({"job": job, "status": "applied", "reason": "submitted"})
                    console.print("[green]Applied! Status updated.[/green]")
                    await self._push_status_to_cloud(job["job_id"], "applied")
                else:
                    reason = getattr(scraper, "last_apply_detail", "") or "not submitted"
                    code   = getattr(scraper, "last_apply_status",  "") or "blocked"
                    console.print(f"[yellow]Application not submitted ({code}) — status unchanged.[/yellow]")
                    if reason:
                        console.print(f"[dim]{reason}[/dim]")
                    # Persist the specific block reason
                    self.state.record_apply_attempt(job["job_id"], code, reason)
                    skipped_count += 1
                    outcomes.append({"job": job, "status": code, "reason": reason})
            except JobExpiredError as exc:
                self.state.set_status(job["job_id"], "expired")
                self.state.record_apply_attempt(job["job_id"], "expired", str(exc))
                console.print("[red]Job no longer active (expired). Status updated to expired.[/red]")
                outcomes.append({"job": job, "status": "expired", "reason": str(exc)})
                await self._push_status_to_cloud(job["job_id"], "expired")
            except Exception as exc:
                console.print(f"[red]Apply error for {job.get('title')}:[/red] {exc}")
                self.state.record_apply_attempt(job["job_id"], "error", str(exc)[:400])
                failed_count += 1
                outcomes.append({"job": job, "status": "error", "reason": str(exc)})

        record_run_stats(applied_count, failed_count, skipped_count)
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

        from .sources.linkedin import LinkedInScraper, _infer_remote_type
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

                    # Run Claude scorer
                    console.print(f"Scoring with Claude...")
                    score, reason, flags, action = self.scorer.score(job)
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
