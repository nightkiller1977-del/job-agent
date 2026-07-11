#!/usr/bin/env python3
"""
job-agent CLI entry point.

Usage:
  python src/main.py setup                     # One-time: install Jobright Chrome extension (logins auto-handled from .env)
  python src/main.py discover                  # Scrape all sources, score, show review queue
  python src/main.py discover --source linkedin
  python src/main.py discover --source linkedin-saved
  python src/main.py discover --source usajobs
  python src/main.py discover --source jobright
  python src/main.py apply                     # Apply to all approved-but-not-yet-applied jobs
  python src/main.py apply --limit 1           # Apply only the first approved job
  python src/main.py preflight                 # Check approved queue readiness
  python src/main.py prepare-sessions          # Open blocked portals to refresh login/session cookies
  python src/main.py status                    # Show stats
  python src/main.py prune                     # Archive jobs older than 30 days (discovered/approved with no apply)
  python src/main.py prune --max-age-days 14   # Use a shorter staleness window
  python src/main.py prune --dry-run           # Preview what would be pruned without changing anything
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Ensure project root is in sys.path when running as `python src/main.py`
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from rich.console import Console

console = Console()


def load_env() -> None:
    """Load .env file from project root. Use override=True so .env wins over
    any empty shell env vars (Claude Code sets ANTHROPIC_API_KEY="" in env)."""
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(str(env_path), override=True)
    else:
        load_dotenv(override=True)


def check_api_key() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or key == "your_key_here":
        console.print(
            "[red]Error:[/red] ANTHROPIC_API_KEY not set.\n"
            "  Copy .env.example to .env and add your key:\n"
            "    cp .env.example .env\n"
            "    # edit .env and paste your Anthropic API key"
        )
        return False
    return True


# Credential pairs required by each source that uses a browser login.
_SOURCE_CREDS: dict[str, list[str]] = {
    "linkedin":  ["LINKEDIN_EMAIL",  "LINKEDIN_PASSWORD"],
    "jobright":  ["JOBRIGHT_EMAIL",  "JOBRIGHT_PASSWORD"],
    "indeed":    ["INDEED_EMAIL",    "INDEED_PASSWORD"],
    "usajobs":   ["USAJOBS_EMAIL", "USAJOBS_PASSWORD"],
}


def preflight_env_check(sources: list[str] | None) -> bool:
    """Validate that every credential required by *sources* is present in env.

    Returns True when all credentials are present, False (after printing errors)
    when any are missing.  Call this before launching any browser.

    Args:
        sources: list of source names that will actually run (e.g. ["linkedin"]).
                 Pass None to check all four browser-login sources.
    """
    if sources is None:
        sources = list(_SOURCE_CREDS.keys())

    missing_count = 0
    for src in sources:
        required = _SOURCE_CREDS.get(src)
        if not required:
            # source has no mandatory creds (e.g. "mcp", "linkedin-saved")
            continue
        for var in required:
            val = os.environ.get(var, "")
            if not val:
                print(
                    f"[PREFLIGHT FAIL] Missing credentials for {src}: "
                    f"{var} is not set. Set it in .env before running.",
                    file=sys.stderr,
                )
                missing_count += 1

    if missing_count:
        print(
            f"\n[PREFLIGHT FAIL] {missing_count} credential(s) missing. "
            "Fix them in .env and re-run.",
            file=sys.stderr,
        )
        return False
    return True


def _db_path_from_config() -> str:
    """Resolve the jobs DB path the same way the Orchestrator does."""
    import json
    for p in (Path("config.json"), Path(__file__).parent.parent / "config.json"):
        if p.exists():
            try:
                with open(p) as f:
                    return json.load(f).get("state_db_path", "state/jobs.db")
            except Exception:
                break
    return "state/jobs.db"


def _sources_in_apply_queue(company: str | None) -> list[str]:
    """Distinct credential-requiring sources in the approved-but-unapplied queue.

    Used by the 'apply' preflight so only sources with jobs actually queued get
    their credentials validated. Returns [] when the queue is empty or when no
    queued source needs credentials (e.g. legacy 'external' jobs).
    """
    try:
        from src.state_manager import StateManager
        state = StateManager(_db_path_from_config())
        jobs = state.get_approved_unapplied()
    except Exception:
        # If we can't read the queue, fall back to validating nothing here;
        # the apply flow will surface any real problem.
        return []
    if company:
        needle = company.lower()
        jobs = [j for j in jobs if needle in (j.get("company") or "").lower()]
    queued = {(j.get("source") or "").lower() for j in jobs if j.get("source")}
    return sorted(s for s in queued if s in _SOURCE_CREDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-agent",
        description="Automated job discovery and application agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    discover_parser = subparsers.add_parser(
        "discover",
        help="Scrape job sources, score them, and run review queue",
    )
    discover_parser.add_argument(
        "--source",
        choices=["linkedin", "linkedin-saved", "usajobs", "jobright", "indeed", "mcp"],
        default=None,
        help=(
            "Scrape only a specific source. "
            "'linkedin-saved' imports jobs you saved in LinkedIn. "
            "'mcp' scores jobs already scraped via Claude-in-Chrome. "
            "(default: all Playwright sources including Indeed)"
        ),
    )
    discover_parser.add_argument(
        "--no-review",
        action="store_true",
        help="Skip the terminal review queue (useful for background/cron syncs)",
    )

    # apply
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply to all jobs marked as approved",
    )
    apply_parser.add_argument(
        "--auto-submit",
        action="store_true",
        default=False,
        help="Automatically submit applications without manual confirmation (default: False)",
    )
    apply_parser.add_argument(
        "--no-auto-submit",
        action="store_false",
        dest="auto_submit",
        help="Pause for manual confirmation before each submission",
    )
    apply_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of approved jobs to attempt in this run",
    )
    apply_parser.add_argument(
        "--job-id",
        default=None,
        help="Only attempt a specific approved job_id",
    )
    apply_parser.add_argument(
        "--source",
        choices=["linkedin", "usajobs", "jobright", "indeed"],
        default=None,
        help="Only attempt approved jobs from one source",
    )
    apply_parser.add_argument(
        "--company",
        default=None,
        help="Only attempt approved jobs whose company contains this text",
    )

    # preflight
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Pull cloud approvals and report which jobs are ready or likely blocked before applying",
    )
    preflight_parser.add_argument("--source", choices=["linkedin", "usajobs", "jobright", "indeed"], default=None)
    preflight_parser.add_argument("--company", default=None)

    # prepare-sessions
    sessions_parser = subparsers.add_parser(
        "prepare-sessions",
        help="Open approved job portals that need login/session refresh in the persistent browser profile",
    )
    sessions_parser.add_argument("--source", choices=["linkedin", "usajobs", "jobright", "indeed"], default=None)
    sessions_parser.add_argument("--company", default=None)
    sessions_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of approved jobs to open for session preparation",
    )

    # setup
    subparsers.add_parser(
        "setup",
        help="One-time: install Jobright AI Chrome extension (logins auto-handled from .env credentials)",
    )

    # status
    subparsers.add_parser(
        "status",
        help="Show current job application stats",
    )

    # stats — apply funnel & success rate (P1 instrumentation)
    subparsers.add_parser(
        "stats",
        help="Show apply funnel and success rate (attempts, submitted, failure clusters, per-source)",
    )

    # ops-check
    ops_parser = subparsers.add_parser(
        "ops-check",
        help="Run the safe operational readiness flow: queue preflight plus mock apply-path tests",
    )
    ops_parser.add_argument("--source", choices=["linkedin", "usajobs", "jobright", "indeed"], default=None)
    ops_parser.add_argument("--company", default=None)
    ops_parser.add_argument(
        "--skip-functional",
        action="store_true",
        help="Only run approved-queue preflight; skip mock Playwright apply-path tests",
    )

    # hydrate
    subparsers.add_parser(
        "hydrate",
        help="Fetch and scrape unhydrated external job URLs",
    )

    # prune
    prune_parser = subparsers.add_parser(
        "prune",
        help="Archive jobs that have been sitting discovered/approved for too long (likely no longer available)",
    )
    prune_parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="Treat jobs older than this many days as stale (default: 30)",
    )
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pruned without making any changes",
    )

    # commander
    commander_parser = subparsers.add_parser(
        "commander",
        help="AI Commander — query, diagnose, and self-heal the agent",
    )
    commander_sub = commander_parser.add_subparsers(dest="subcommand", required=True)

    ask_p = commander_sub.add_parser("ask", help="Ask the model a natural language question about the agent")
    ask_p.add_argument("question", nargs="+", help="The question to ask")

    diag_p = commander_sub.add_parser("diagnose", help="Diagnose source health")
    diag_p.add_argument("--source", choices=["linkedin", "jobright", "indeed", "usajobs"], default=None)

    fix_p = commander_sub.add_parser("fix", help="Attempt automated fix for a source")
    fix_p.add_argument("--source", required=True, choices=["linkedin", "jobright", "indeed", "usajobs"])

    commander_sub.add_parser("report", help="Full agent health report")

    watch_p = commander_sub.add_parser("watch", help="Watch for failures and auto-heal")
    watch_p.add_argument("--interval", type=int, default=30)
    watch_p.add_argument("--no-auto-fix", action="store_true")

    # expand
    expand_parser = subparsers.add_parser(
        "expand",
        help="Enrich profile skills by analyzing public profiles (e.g. GitHub)",
    )
    expand_parser.add_argument(
        "--github",
        required=True,
        help="GitHub username to analyze",
    )

    # upskill
    subparsers.add_parser(
        "upskill",
        help="Analyze skill gaps against approved/applied jobs and generate learning roadmap",
    )

    # session-status
    subparsers.add_parser(
        "session-status",
        help="Show session health for all sources (healthy/stale/expired/missing)",
    )

    # heartbeat
    heartbeat_p = subparsers.add_parser(
        "heartbeat",
        help="Silently visit each source to extend cookie lifetime (run nightly via cron)",
    )
    heartbeat_p.add_argument(
        "--source",
        help="Limit heartbeat to one source (linkedin, indeed, jobright)",
    )

    return parser


async def main_async(args: argparse.Namespace) -> int:
    from src.orchestrator import Orchestrator

    # Config path relative to project root
    config_path = str(project_root / "config.json")
    orchestrator = Orchestrator(config_path=config_path)

    if args.command == "discover":
        await orchestrator.discover(source=args.source, no_review=args.no_review)

    elif args.command == "apply":
        await orchestrator.apply_approved(
            auto_submit=args.auto_submit,
            limit=args.limit,
            job_id=args.job_id,
            source=args.source,
            company=args.company,
        )

    elif args.command == "preflight":
        await orchestrator.preflight_approved(source=args.source, company=args.company)

    elif args.command == "prepare-sessions":
        await orchestrator.prepare_sessions(
            source=args.source,
            company=args.company,
            limit=args.limit,
        )

    elif args.command == "setup":
        await orchestrator.browser_setup()

    elif args.command == "status":
        orchestrator.show_status()

    elif args.command == "stats":
        orchestrator.show_apply_stats()

    elif args.command == "ops-check":
        console.rule("[bold green]Operational flow check[/bold green]")
        await orchestrator.preflight_approved(source=args.source, company=args.company)
        if not args.skip_functional:
            console.rule("[bold blue]Functional apply-path smoke tests[/bold blue]")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_apply_functional.py", "-q"],
                cwd=str(project_root),
            )
            if result.returncode != 0:
                return result.returncode
        console.print("[green]Operational flow check passed.[/green]")

    elif args.command == "hydrate":
        await orchestrator.hydrate_external_jobs()

    elif args.command == "prune":
        orchestrator.prune_stale_jobs(
            max_age_days=args.max_age_days,
            dry_run=args.dry_run,
        )

    elif args.command == "session-status":
        from src.session_watchdog import check_session_health, print_health_table
        results = check_session_health()
        print_health_table(results)
        blocked = [r for r in results if r.status in {"expired", "missing"}]
        if blocked:
            console.print(f"\n[red]{len(blocked)} source(s) need attention.[/red]")
            console.print("[cyan]Run:[/cyan] python src/main.py prepare-sessions")
            console.print("[cyan]Or:[/cyan]  bash scripts/install-jobagent-url-handler.sh  (one-tap repair from phone)")
        return 1 if blocked else 0

    elif args.command == "heartbeat":
        from src.session_watchdog import run_heartbeat
        sources = [args.source] if getattr(args, "source", None) else None
        results = await run_heartbeat(sources=sources, config=orchestrator.config)
        for src, ok in results.items():
            status = "[green]✓[/green]" if ok else "[red]✗[/red]"
            console.print(f"  {status} {src}")
        return 0

    elif args.command == "expand":
        from src.profile_enricher import ProfileEnricher
        enricher = ProfileEnricher()
        await enricher.enrich_from_github(args.github)

    elif args.command == "upskill":
        from src.gap_analyzer import GapAnalyzer
        analyzer = GapAnalyzer()
        await analyzer.run_analysis()

    elif args.command == "commander":
        import json
        from src.commander import AgentCommander
        from src.watcher import StatusWatcher

        config = json.loads((project_root / "config.json").read_text())
        commander = AgentCommander(config)

        if args.subcommand == "ask":
            question = " ".join(args.question)
            console.print(await commander.query(question))

        elif args.subcommand == "diagnose":
            report = commander.diagnose(args.source)
            console.print(report["summary"])
            for src, d in report["sources"].items():
                color = {"healthy": "green", "warning": "yellow", "critical": "red"}.get(d["severity"], "white")
                console.print(f"  [{color}]{src}[/{color}]: {d['root_cause']} | fixable={d['fixable']}")

        elif args.subcommand == "fix":
            result = await commander.attempt_fix(args.source)
            if result["success"]:
                console.print(f"[green]Fixed {args.source}[/green]: {result.get('detail', '')}")
            else:
                console.print(f"[red]Could not fix {args.source}[/red]: {result.get('reason', '')}")

        elif args.subcommand == "report":
            report = commander.get_report()
            console.print(commander.format_report(report))

        elif args.subcommand == "watch":
            watcher = StatusWatcher(config, poll_interval=args.interval, auto_fix=not args.no_auto_fix)
            console.print(f"[green]Watching for failures every {args.interval}s (auto_fix={not args.no_auto_fix})...[/green]")
            await watcher.watch()

    return 0


def main() -> None:
    load_env()
    # Telemetry is optional — never let a missing/broken telemetry dep (e.g. openlit
    # not installed) block core commands like `stats`/`apply`.
    try:
        from src.telemetry import setup as setup_telemetry
        setup_telemetry(agent="job-agent")
    except Exception as _tel_exc:
        import logging
        logging.getLogger(__name__).warning("telemetry setup skipped: %s", _tel_exc)
    parser = build_parser()
    args = parser.parse_args()

    # All commands except 'status' need the API key
    if args.command in ("discover", "hydrate", "expand", "upskill") and not check_api_key():
        sys.exit(1)
    if args.command == "commander" and args.subcommand in ("ask", "report", "watch") and not check_api_key():
        sys.exit(1)

    # Pre-flight: ensure browser-login credentials are present before any
    # browser is launched.  For 'discover', only check the source(s) that
    # will actually run; for 'apply', check the single --source (or all).
    if args.command == "discover":
        # mcp and linkedin-saved don't use browser login creds
        src = getattr(args, "source", None)
        if src in ("mcp", "linkedin-saved"):
            sources_to_check: list[str] | None = []   # nothing to validate
        elif src is not None:
            sources_to_check = [src]
        else:
            sources_to_check = None  # all four
        if sources_to_check != [] and not preflight_env_check(sources_to_check):
            sys.exit(1)

    if args.command == "apply":
        src = getattr(args, "source", None)
        if src:
            sources_to_check = [src]
        elif os.environ.get("DASHBOARD_URL"):
            # apply_approved() pulls cloud-approved jobs into the local queue
            # AFTER this preflight, so the local queue can't tell us which
            # sources those jobs use yet. Validate all sources to preserve the
            # fail-fast guarantee for the cloud-approval workflow.
            sources_to_check = None
        else:
            # Local-only: validate creds just for sources actually represented
            # in the approved queue — a missing USAJOBS_PASSWORD shouldn't block
            # an apply run whose queue is all LinkedIn jobs. An empty queue means
            # nothing to apply, so nothing to validate.
            sources_to_check = _sources_in_apply_queue(getattr(args, "company", None))
        if sources_to_check != [] and not preflight_env_check(sources_to_check):
            sys.exit(1)

    try:
        exit_code = asyncio.run(main_async(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
