from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .state_manager import StateManager
from .notifier import _load_status, notify_info, notify_error
from .model_client import ModelClient

_log = logging.getLogger(__name__)
SESSIONS_DIR = Path(__file__).parent.parent / "state" / "sessions"
ALL_SOURCES = ["linkedin", "jobright", "indeed", "usajobs"]
CRED_KEYS = {
    "linkedin":  ("LINKEDIN_EMAIL",  "LINKEDIN_PASSWORD"),
    "jobright":  ("JOBRIGHT_EMAIL",  "JOBRIGHT_PASSWORD"),
    "indeed":    ("INDEED_EMAIL",    "INDEED_PASSWORD"),
    "usajobs":   ("USAJOBS_EMAIL", "USAJOBS_PASSWORD"),
}


class AgentCommander:
    def __init__(self, config: dict):
        self.config = config
        self._state = StateManager(config.get("state_db_path", "state/jobs.db"))
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._notify_phone = os.environ.get("NOTIFY_PHONE", "")
        self._model_client = ModelClient(anthropic_api_key=self._api_key)

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        status = _load_status()
        alerts = status.get("alerts", [])
        reauth_events = status.get("reauth_events", [])
        applied_total = status.get("applied", 0)
        failed_total = status.get("failed", 0)
        skipped_total = status.get("skipped", 0)
        last_run = status.get("last_run", "unknown")

        try:
            stats = self._state.get_stats()
        except Exception:
            stats = {}

        today_counts = stats.get("today", {})

        lines: list[str] = []

        # --- Overall stats ---
        lines.append("=== AGENT OVERALL STATS ===")
        lines.append(f"Applied (total): {applied_total}")
        lines.append(f"Failed (total):  {failed_total}")
        lines.append(f"Skipped (total): {skipped_total}")
        lines.append(f"Last run:        {last_run}")
        lines.append("")

        # --- Per-status job counts ---
        lines.append("=== JOB STATUS COUNTS (today) ===")
        for status_name in ("discovered", "approved", "applied", "failed", "skipped"):
            lines.append(f"  {status_name}: {today_counts.get(status_name, 0)}")
        lines.append("")

        # --- Last 20 alerts ---
        lines.append("=== RECENT ALERTS (last 20) ===")
        for alert in alerts[-20:]:
            ts = alert.get("ts", "")
            level = alert.get("level", "")
            title = alert.get("title", "")
            detail = alert.get("detail", "")
            lines.append(f"  [{ts}] [{level.upper()}] {title}: {detail}")
        if not alerts:
            lines.append("  (none)")
        lines.append("")

        # --- Last 20 reauth events ---
        lines.append("=== RECENT REAUTH EVENTS (last 20) ===")
        for ev in reauth_events[-20:]:
            ts = ev.get("ts", "")
            source = ev.get("source", "")
            mode = ev.get("mode", "")
            outcome = ev.get("outcome", "")
            detail = ev.get("detail", "")
            lines.append(f"  [{ts}] {source} mode={mode} outcome={outcome}: {detail}")
        if not reauth_events:
            lines.append("  (none)")
        lines.append("")

        # --- Last 10 applied jobs ---
        lines.append("=== LAST 10 APPLIED JOBS ===")
        try:
            applied_jobs = self._state.get_jobs_by_status("applied")
            applied_jobs_sorted = sorted(
                applied_jobs,
                key=lambda j: j.get("applied_at") or "",
                reverse=True,
            )[:10]
            for job in applied_jobs_sorted:
                lines.append(
                    f"  {job.get('title', '')} @ {job.get('company', '')} "
                    f"[{job.get('source', '')}] applied={job.get('applied_at', '')}"
                )
            if not applied_jobs_sorted:
                lines.append("  (none)")
        except Exception as exc:
            lines.append(f"  (error fetching applied jobs: {exc})")
        lines.append("")

        # --- Last 10 failed jobs ---
        lines.append("=== LAST 10 FAILED JOBS ===")
        try:
            failed_jobs = self._state.get_jobs_by_status("failed")
            failed_jobs_sorted = sorted(
                failed_jobs,
                key=lambda j: j.get("reviewed_at") or j.get("discovered_at") or "",
                reverse=True,
            )[:10]
            for job in failed_jobs_sorted:
                extra = ""
                raw_extra = job.get("extra_json")
                if raw_extra:
                    try:
                        extra_data = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
                        extra = str(extra_data.get("error", extra_data))[:120]
                    except Exception:
                        extra = str(raw_extra)[:120]
                lines.append(
                    f"  {job.get('title', '')} @ {job.get('company', '')} "
                    f"[{job.get('source', '')}] status={job.get('status', '')} detail={extra}"
                )
            if not failed_jobs_sorted:
                lines.append("  (none)")
        except Exception as exc:
            lines.append(f"  (error fetching failed jobs: {exc})")
        lines.append("")

        # --- Per-source info ---
        lines.append("=== PER-SOURCE INFO ===")
        for source in ALL_SOURCES:
            lines.append(f"  [{source}]")
            try:
                all_source_jobs = [
                    j for j in (self._state.get_jobs_by_status("discovered")
                                 + self._state.get_jobs_by_status("approved")
                                 + self._state.get_jobs_by_status("applied")
                                 + self._state.get_jobs_by_status("failed")
                                 + self._state.get_jobs_by_status("skipped"))
                    if j.get("source") == source
                ]
                job_count = len(all_source_jobs)
                last_discovered = max(
                    (j.get("discovered_at") or "" for j in all_source_jobs),
                    default=None,
                )
                # last outcome from reauth events
                source_reauths = [e for e in reauth_events if e.get("source") == source]
                last_outcome = source_reauths[-1].get("outcome", "unknown") if source_reauths else "unknown"
                lines.append(f"    job_count:   {job_count}")
                lines.append(f"    last_seen:   {last_discovered or 'never'}")
                lines.append(f"    last_outcome: {last_outcome}")
            except Exception as exc:
                lines.append(f"    (error: {exc})")
        lines.append("")

        # --- Credential check ---
        lines.append("=== CREDENTIAL CHECK ===")
        for source, (email_key, pass_key) in CRED_KEYS.items():
            email_set = "SET" if os.environ.get(email_key) else "MISSING"
            pass_set = "SET" if os.environ.get(pass_key) else "MISSING"
            lines.append(f"  {source}: {email_key}={email_set}, {pass_key}={pass_set}")
        lines.append("")

        # --- Session files ---
        lines.append("=== SESSION FILES ===")
        now = datetime.now(timezone.utc)
        for source in ALL_SOURCES:
            session_file = SESSIONS_DIR / f"{source}_chromium.json"
            if session_file.exists():
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
                age_hours = (now - mtime).total_seconds() / 3600
                lines.append(f"  {source}_chromium.json: EXISTS, age={age_hours:.1f}h")
            else:
                lines.append(f"  {source}_chromium.json: MISSING")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def query(self, question: str) -> str:
        """
        Answer a natural language question about the agent.
        Routes through Ollama first (via ModelClient); Claude is the fallback.

        async to avoid nested asyncio.run() crash when called from main_async().
        """
        context = self._build_context()
        system = (
            "You are an AI assistant integrated with a job application agent. "
            "You have access to the agent's complete state. "
            "Answer questions clearly and concisely. "
            "When something has failed, explain why based on the data. "
            "When something can be fixed automatically, say so explicitly."
        )
        messages = [
            {
                "role": "user",
                "content": f"Agent state:\n{context}\n\nQuestion: {question}",
            }
        ]
        return await self._model_client.complete(messages, system=system, task_type="reasoning")

    # ------------------------------------------------------------------
    # Diagnose
    # ------------------------------------------------------------------

    def diagnose(self, source: str | None = None) -> dict:
        status = _load_status()
        alerts = status.get("alerts", [])
        reauth_events = status.get("reauth_events", [])
        now = datetime.now(timezone.utc)

        sources_to_check = [source] if source else ALL_SOURCES
        source_results: dict[str, dict] = {}

        for src in sources_to_check:
            email_key, pass_key = CRED_KEYS.get(src, (f"{src.upper()}_EMAIL", f"{src.upper()}_PASSWORD"))
            has_email = bool(os.environ.get(email_key))
            has_password = bool(os.environ.get(pass_key))

            session_file = SESSIONS_DIR / f"{src}_chromium.json"
            session_exists = session_file.exists()
            session_age_hours: float | None = None
            if session_exists:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
                session_age_hours = (now - mtime).total_seconds() / 3600

            # Recent errors mentioning this source
            recent_errors = [
                a for a in alerts[-20:]
                if a.get("level") == "error"
                and src.lower() in (a.get("title", "") + a.get("detail", "")).lower()
            ]

            # Recent reauth outcome
            source_reauths = [e for e in reauth_events if e.get("source") == src]
            recent_reauth_outcome: str | None = source_reauths[-1].get("outcome") if source_reauths else None

            # Job count and last scrape
            try:
                all_jobs = []
                for st in ("discovered", "approved", "applied", "failed", "skipped"):
                    all_jobs.extend(j for j in self._state.get_jobs_by_status(st) if j.get("source") == src)
                job_count = len(all_jobs)
                last_scrape = max(
                    (j.get("discovered_at") or "" for j in all_jobs),
                    default=None,
                ) or None
            except Exception:
                job_count = 0
                last_scrape = None

            # Determine root cause and severity
            has_creds = has_email and has_password

            if not has_creds:
                root_cause = "missing credentials"
                severity = "critical"
                fixable = False
            elif not session_exists:
                root_cause = "stale session (no session file)"
                severity = "warning"
                fixable = True
            elif session_age_hours is not None and session_age_hours > 24:
                root_cause = f"stale session ({session_age_hours:.0f}h old)"
                severity = "warning"
                fixable = True
            elif recent_reauth_outcome in ("failed", "error"):
                root_cause = "recent auth failure"
                severity = "critical"
                fixable = False
            elif recent_errors:
                root_cause = "recent errors in logs"
                severity = "warning"
                fixable = False
            else:
                root_cause = "healthy"
                severity = "healthy"
                fixable = False

            source_results[src] = {
                "credentials": {"email": has_email, "password": has_password},
                "session_exists": session_exists,
                "session_age_hours": session_age_hours,
                "recent_errors": recent_errors,
                "recent_reauth_outcome": recent_reauth_outcome,
                "job_count": job_count,
                "last_scrape": last_scrape,
                "fixable": fixable,
                "root_cause": root_cause,
                "severity": severity,
            }

        # Build summary
        healthy = sum(1 for v in source_results.values() if v["severity"] == "healthy")
        total = len(source_results)
        summary_parts = [f"{healthy}/{total} sources healthy."]
        for src, data in source_results.items():
            if data["severity"] != "healthy":
                summary_parts.append(f"{src}: {data['root_cause']}.")
        summary = " ".join(summary_parts)

        has_fixable = any(v["fixable"] for v in source_results.values())

        return {
            "sources": source_results,
            "summary": summary,
            "has_fixable": has_fixable,
        }

    # ------------------------------------------------------------------
    # Attempt fix
    # ------------------------------------------------------------------

    async def attempt_fix(self, source: str, _reauth_cls=None) -> dict:
        if _reauth_cls is None:
            from .reauth import ReauthManager
            _reauth_cls = ReauthManager
        ReauthManager = _reauth_cls

        diagnosis = self.diagnose(source)
        source_data = diagnosis["sources"].get(source, {})

        if not source_data.get("fixable"):
            root_cause = source_data.get("root_cause", "unknown")
            return {"success": False, "reason": f"not fixable: {root_cause}"}

        root_cause = source_data["root_cause"]

        _AUTOMATED = {"jobright", "indeed", "linkedin"}
        strategy = "automated" if source in _AUTOMATED else "human"

        _log.info(
            "heal.start source=%s strategy=%s root_cause=%s",
            source, strategy, root_cause,
        )
        try:
            success = await ReauthManager(self.config).handle(
                source, root_cause, context="discover"
            )
            if success:
                _log.info(
                    "heal.success source=%s strategy=%s root_cause=%s",
                    source, strategy, root_cause,
                )
                notify_info(
                    title=f"Auto-fix succeeded: {source}",
                    detail=f"strategy={strategy} root_cause={root_cause}",
                )
            else:
                _log.warning(
                    "heal.failed source=%s strategy=%s root_cause=%s reason=reauth_returned_false",
                    source, strategy, root_cause,
                )
                notify_error(
                    title=f"Auto-fix failed: {source}",
                    detail=f"strategy={strategy} root_cause={root_cause}",
                )
            return {
                "success": bool(success),
                "source": source,
                "strategy": strategy,
                "detail": root_cause,
            }
        except Exception as exc:
            _log.error(
                "heal.exception source=%s strategy=%s error=%s",
                source, strategy, exc,
            )
            notify_error(
                title=f"Auto-fix exception: {source}",
                detail=str(exc),
            )
            return {
                "success": False,
                "source": source,
                "strategy": strategy,
                "detail": str(exc),
            }

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def get_report(self) -> dict:
        status = _load_status()
        alerts = status.get("alerts", [])
        reauth_events = status.get("reauth_events", [])

        try:
            stats = self._state.get_stats()
        except Exception:
            stats = {}
        today_counts = stats.get("today", {})

        # Recent applied jobs
        try:
            applied_jobs = self._state.get_jobs_by_status("applied")
            applied_sorted = sorted(
                applied_jobs,
                key=lambda j: j.get("applied_at") or "",
                reverse=True,
            )[:10]
            recent_applied = [
                {
                    "title": j.get("title", ""),
                    "company": j.get("company", ""),
                    "source": j.get("source", ""),
                    "applied_at": j.get("applied_at", ""),
                }
                for j in applied_sorted
            ]
        except Exception:
            recent_applied = []

        # Recent failed jobs
        try:
            failed_jobs = self._state.get_jobs_by_status("failed")
            failed_sorted = sorted(
                failed_jobs,
                key=lambda j: j.get("reviewed_at") or j.get("discovered_at") or "",
                reverse=True,
            )[:10]
            recent_failed = []
            for j in failed_sorted:
                detail = ""
                raw_extra = j.get("extra_json")
                if raw_extra:
                    try:
                        extra_data = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
                        detail = str(extra_data.get("error", extra_data))[:200]
                    except Exception:
                        detail = str(raw_extra)[:200]
                recent_failed.append(
                    {
                        "title": j.get("title", ""),
                        "company": j.get("company", ""),
                        "source": j.get("source", ""),
                        "detail": detail,
                    }
                )
        except Exception:
            recent_failed = []

        # Source health
        diagnosis = self.diagnose()
        source_health = {
            src: {
                "severity": data["severity"],
                "root_cause": data["root_cause"],
                "fixable": data["fixable"],
            }
            for src, data in diagnosis["sources"].items()
        }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {
                "applied": status.get("applied", 0),
                "failed": status.get("failed", 0),
                "skipped": status.get("skipped", 0),
                "last_run": status.get("last_run", ""),
            },
            "jobs_by_status": today_counts,
            "recent_applied": recent_applied,
            "recent_failed": recent_failed,
            "source_health": source_health,
            "recent_alerts": alerts[-20:],
            "reauth_events": reauth_events[-20:],
        }

    # ------------------------------------------------------------------
    # Format report
    # ------------------------------------------------------------------

    def format_report(self, report: dict) -> str:
        buf = io.StringIO()
        console = Console(file=buf, highlight=False)

        # --- Job stats table ---
        stats = report.get("stats", {})
        jobs_by_status = report.get("jobs_by_status", {})

        stats_table = Table(title="Job Stats", show_header=True, header_style="bold cyan")
        stats_table.add_column("Metric", style="bold")
        stats_table.add_column("Value")
        stats_table.add_row("Applied (total)", str(stats.get("applied", 0)))
        stats_table.add_row("Failed (total)", str(stats.get("failed", 0)))
        stats_table.add_row("Skipped (total)", str(stats.get("skipped", 0)))
        stats_table.add_row("Last Run", str(stats.get("last_run", "")))
        for status_name in ("discovered", "approved", "applied", "failed", "skipped"):
            stats_table.add_row(
                f"  Today / {status_name}",
                str(jobs_by_status.get(status_name, 0)),
            )
        console.print(stats_table)

        # --- Per-source health table ---
        source_health = report.get("source_health", {})
        health_table = Table(title="Source Health", show_header=True, header_style="bold magenta")
        health_table.add_column("Source", style="bold")
        health_table.add_column("Severity")
        health_table.add_column("Root Cause")
        health_table.add_column("Fixable")

        severity_styles = {
            "healthy": "green",
            "warning": "yellow",
            "critical": "red",
        }
        for src, health in source_health.items():
            sev = health.get("severity", "unknown")
            style = severity_styles.get(sev, "white")
            health_table.add_row(
                src,
                f"[{style}]{sev}[/{style}]",
                health.get("root_cause", ""),
                "Yes" if health.get("fixable") else "No",
            )
        console.print(health_table)

        # --- Recent alerts (last 10) ---
        recent_alerts = report.get("recent_alerts", [])[-10:]
        if recent_alerts:
            alerts_table = Table(title="Recent Alerts", show_header=True, header_style="bold yellow")
            alerts_table.add_column("Time")
            alerts_table.add_column("Level")
            alerts_table.add_column("Title")
            alerts_table.add_column("Detail")

            level_styles = {
                "error": "red",
                "warning": "yellow",
                "success": "green",
                "info": "blue",
            }
            for alert in recent_alerts:
                level = alert.get("level", "info")
                style = level_styles.get(level, "white")
                alerts_table.add_row(
                    alert.get("ts", ""),
                    f"[{style}]{level}[/{style}]",
                    alert.get("title", ""),
                    alert.get("detail", "")[:80],
                )
            console.print(alerts_table)

        return buf.getvalue()
