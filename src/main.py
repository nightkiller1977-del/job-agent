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

    elif args.command == "commander":
        import json
        from src.commander import AgentCommander
        from src.watcher import StatusWatcher

        config = json.loads((project_root / "config.json").read_text())
        commander = AgentCommander(config)

        if args.subcommand == "ask":
            question = " ".join(args.question)
            console.print(commander.query(question))

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
    from src.telemetry import setup as setup_telemetry
    setup_telemetry(agent="job-agent")
    parser = build_parser()
    args = parser.parse_args()

    # All commands except 'status' need the API key
    if args.command in ("discover", "hydrate") and not check_api_key():
        sys.exit(1)
    if args.command == "commander" and args.subcommand in ("ask", "report", "watch") and not check_api_key():
        sys.exit(1)

    try:
        exit_code = asyncio.run(main_async(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    main()
