"""
Terminal review queue — presents each scored job to the user for action.
Uses Rich for a clean, readable terminal UI.
"""
from __future__ import annotations

import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()


def _score_color(score: int) -> str:
    if score >= 80:
        return "bold green"
    elif score >= 60:
        return "bold yellow"
    elif score >= 40:
        return "yellow"
    else:
        return "red"


def _remote_badge(remote_type: str) -> Text:
    rt = (remote_type or "").upper()
    if "REMOTE" in rt:
        return Text("REMOTE", style="bold green")
    elif "HYBRID" in rt:
        return Text("HYBRID", style="bold yellow")
    elif "ONSITE" in rt or "ON-SITE" in rt or "ON SITE" in rt:
        return Text("ON-SITE", style="bold red")
    else:
        return Text(rt or "UNKNOWN", style="dim")


def _source_badge(source: str) -> Text:
    s = (source or "").lower()
    if "linkedin" in s:
        return Text("LinkedIn", style="bold blue")
    elif "usajobs" in s or "usa" in s:
        return Text("USAJobs", style="bold cyan")
    elif "jobright" in s:
        return Text("Jobright", style="bold magenta")
    else:
        return Text(source or "Unknown", style="dim")


def _flag_badges(flags: str) -> list[Text]:
    badges = []
    flag_list = [f.strip() for f in (flags or "").split(",") if f.strip()]
    flag_styles = {
        "FEDERAL_ROLE": ("FEDERAL", "bold white on blue"),
        "CLEARED_ROLE": ("TS CLEARED", "bold white on dark_red"),
        "ALWAYS_APPLY": ("AUTO-APPLY", "bold white on green"),
        "FLAG_FOR_REVIEW": ("REVIEW", "bold black on yellow"),
        "SALARY_MISSING": ("SALARY?", "bold yellow"),
        "BELOW_THRESHOLD": ("BELOW $", "bold red"),
        "LOCATION_MISMATCH": ("LOCATION!", "bold red"),
        "IC_ROLE": ("IC ROLE", "bold white on red"),
        "SKIP": ("SKIP", "bold white on red"),
    }
    for flag in flag_list:
        if flag in flag_styles:
            label, style = flag_styles[flag]
            badges.append(Text(f" {label} ", style=style))
    return badges


def render_job_card(job: dict, index: int, total: int) -> Panel:
    """Render a single job as a Rich Panel."""
    score = job.get("score") or 0
    score_color = _score_color(score)

    # Header line
    title_text = Text()
    title_text.append(f"[{index}/{total}] ", style="dim")
    title_text.append(job.get("title", "Unknown Title"), style="bold white")
    title_text.append(" @ ", style="dim")
    title_text.append(job.get("company", "Unknown Company"), style="bold cyan")

    # Details table
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column("Field", style="dim", width=12)
    table.add_column("Value")

    table.add_row("Location", job.get("location", "—"))
    table.add_row("Salary", job.get("salary_raw", "Not listed") or "Not listed")

    # Remote/source row
    remote_text = _remote_badge(job.get("remote_type", ""))
    source_text = _source_badge(job.get("source", ""))
    combo = Text()
    combo.append_text(remote_text)
    combo.append("  ")
    combo.append_text(source_text)
    table.add_row("Type/Source", combo)

    # Score row
    score_text = Text()
    score_text.append(f"{score}/100", style=score_color)
    reason = job.get("score_reason", "")
    if reason:
        score_text.append(f"  {reason[:120]}", style="dim")
    table.add_row("Score", score_text)

    # Flags
    flag_badges = _flag_badges(job.get("flags", ""))
    if flag_badges:
        flags_text = Text()
        for badge in flag_badges:
            flags_text.append_text(badge)
            flags_text.append(" ")
        table.add_row("Flags", flags_text)

    # URL
    url = job.get("url", "")
    if url:
        table.add_row("URL", Text(url[:80], style="link " + url if url.startswith("http") else "dim"))

    # Description snippet
    desc = (job.get("description") or "").strip()
    if desc:
        snippet = desc[:300].replace("\n", " ")
        if len(desc) > 300:
            snippet += "…"
        table.add_row("Snippet", Text(snippet, style="italic dim"))

    from rich.columns import Columns
    from rich.align import Align

    content_lines = [title_text, "", table]

    from rich.console import Group
    content = Group(title_text, Text(""), table)

    border_style = _score_color(score)
    return Panel(
        content,
        border_style=border_style,
        padding=(1, 2),
    )


def prompt_user_action() -> str:
    """
    Show the action prompt and return the chosen action.
    Returns: 'apply' | 'skip' | 'bookmark' | 'quit'
    """
    console.print(
        "\n  [bold][A][/bold]pply  "
        "[bold][S][/bold]kip  "
        "[bold][B][/bold]ookmark  "
        "[bold][Q][/bold]uit\n",
        highlight=False,
    )
    while True:
        try:
            raw = input("  Action > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted. Quitting review queue.[/yellow]")
            return "quit"
        if raw in ("a", "apply"):
            return "apply"
        elif raw in ("s", "skip"):
            return "skip"
        elif raw in ("b", "bookmark"):
            return "bookmark"
        elif raw in ("q", "quit"):
            return "quit"
        else:
            console.print("  [red]Unknown key. Use A / S / B / Q.[/red]")


def run_review_queue(jobs: list[dict], state_manager) -> dict:
    """
    Present jobs one by one for review.
    Updates state_manager for each decision.
    Returns summary dict.
    """
    if not jobs:
        console.print("[yellow]No jobs in the review queue.[/yellow]")
        return {"applied": 0, "skipped": 0, "bookmarked": 0, "quit": False}

    console.rule("[bold blue]Job Review Queue[/bold blue]")
    console.print(f"  [dim]{len(jobs)} jobs to review. Sorted by score (highest first).[/dim]\n")

    summary = {"applied": 0, "skipped": 0, "bookmarked": 0, "quit": False}

    for i, job in enumerate(jobs, start=1):
        console.clear()
        console.print(render_job_card(job, i, len(jobs)))
        action = prompt_user_action()

        if action == "quit":
            summary["quit"] = True
            console.print("[yellow]Quitting review queue. Progress saved.[/yellow]")
            break
        elif action == "apply":
            state_manager.set_status(job["job_id"], "approved")
            summary["applied"] += 1
            console.print("[green]  Marked for application.[/green]")
        elif action == "skip":
            state_manager.set_status(job["job_id"], "skipped")
            summary["skipped"] += 1
            console.print("[dim]  Skipped.[/dim]")
        elif action == "bookmark":
            state_manager.set_status(job["job_id"], "bookmarked")
            summary["bookmarked"] += 1
            console.print("[cyan]  Bookmarked.[/cyan]")

    return summary


def show_summary_table(stats: dict) -> None:
    """Display a summary stats table."""
    console.rule("[bold blue]Job Agent Stats[/bold blue]")
    table = Table(title="Job Status Summary", box=box.ROUNDED)
    table.add_column("Status", style="bold")
    table.add_column("Total", justify="right")
    table.add_column("Today", justify="right", style="dim")

    today = stats.get("today", {})
    for status in ["discovered", "approved", "applied", "skipped", "bookmarked", "expired"]:
        total = stats.get(status, 0)
        today_count = today.get(status, 0)
        style = ""
        if status == "applied":
            style = "bold green"
        elif status == "approved":
            style = "bold yellow"
        elif status == "skipped":
            style = "dim"
        elif status == "expired":
            style = "bold red"
        table.add_row(status.capitalize(), str(total), str(today_count), style=style)

    console.print(table)

