"""
ATS API discovery source — Greenhouse / Lever / Ashby public job-board APIs.

Source-first routing: these vendors publish public JSON APIs *for* applicants,
so this source needs no browser, no login session, and hits no bot walls. It
wraps the fetchers in src/discovery/ats_api.py (normalization, canonical URLs,
salary extraction, per-board dedup) as a standard discovery source the
orchestrator can fan out alongside the browser scrapers.

Configured via the "ats_boards" section of config.json:

    "ats_boards": {
      "title_include": ["manager", "director", "vp", "head of"],
      "greenhouse": ["stripe", {"token": "reddit", "company": "Reddit"}],
      "lever":      ["netflix"],
      "ashby":      [{"token": "openai", "company": "OpenAI"}]
    }

Board entries are either a bare token string or {"token": ..., "company": ...}.
"title_include" is an optional case-insensitive substring pre-filter applied
before scoring — whole boards can carry hundreds of postings, and pre-filtering
keeps LLM scoring spend proportional to relevant jobs. Leave it out to score
everything. Jobs keep their vendor as `source` ("greenhouse"/"lever"/"ashby"),
which SOURCE_MAP routes through the external-ATS apply flow — the same path
that owns the dedicated Greenhouse/Lever/Ashby apply adapters.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.console import Console

from src.discovery.ats_api import (
    fetch_greenhouse_jobs,
    fetch_lever_jobs,
    fetch_ashby_jobs,
)
from .base import BaseScraper

console = Console()

# Vendor → fetcher, resolved through the module namespace at call time so a
# monkeypatched fetch_*_jobs is honored (import-time references would not be).
_VENDORS = ("greenhouse", "lever", "ashby")


def _fetcher_for(vendor: str):
    import sys
    return getattr(sys.modules[__name__], f"fetch_{vendor}_jobs")


def _parse_board_entry(entry) -> tuple[str, str | None]:
    """Accept "token" or {"token": ..., "company": ...}; returns (token, company)."""
    if isinstance(entry, str):
        return entry.strip(), None
    if isinstance(entry, dict):
        return str(entry.get("token", "")).strip(), entry.get("company") or None
    return "", None


class AtsApiScraper(BaseScraper):
    name = "ats"

    async def scrape(self) -> list[dict]:
        boards_cfg = self.config.get("ats_boards", {}) or {}
        title_include = [
            t.strip().lower()
            for t in boards_cfg.get("title_include", [])
            if isinstance(t, str) and t.strip()
        ]

        tasks = []
        labels = []
        for vendor in _VENDORS:
            for entry in boards_cfg.get(vendor, []) or []:
                token, company = _parse_board_entry(entry)
                if not token:
                    continue
                tasks.append(_fetcher_for(vendor)(token, company))
                labels.append(f"{vendor}:{token}")

        if not tasks:
            console.print(
                "[yellow]ATS API: no boards configured — add an \"ats_boards\" "
                "section to config.json to enable this source.[/yellow]"
            )
            return []

        console.print(f"[blue]ATS API:[/blue] fetching {len(tasks)} board(s): {', '.join(labels)}")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        now = datetime.utcnow().isoformat()
        jobs: list[dict] = []
        seen_ids: set[str] = set()
        raw_count = 0
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                # Fetchers already log + return [] on HTTP errors; this catches
                # anything unexpected so one board never sinks the batch.
                console.print(f"[red]ATS API: {label} failed: {result}[/red]")
                continue
            raw_count += len(result)
            for job in result:
                if job["job_id"] in seen_ids:
                    continue  # same posting listed on two configured boards
                seen_ids.add(job["job_id"])
                job.setdefault("discovered_at", now)
                jobs.append(job)

        if title_include:
            kept = [
                j for j in jobs
                if any(term in (j.get("title") or "").lower() for term in title_include)
            ]
            dropped = len(jobs) - len(kept)
            if dropped:
                console.print(
                    f"  [dim]title_include filter dropped {dropped} of {len(jobs)} "
                    f"postings before scoring[/dim]"
                )
            jobs = kept

        console.print(f"  ATS API: {len(jobs)} postings from {raw_count} raw across {len(tasks)} board(s)")
        return jobs

    async def apply(self, job: dict, auto_submit: bool = False) -> bool:
        """ATS jobs apply through the external-ATS flow (adapter registry).

        Normally unused: these jobs carry source "greenhouse"/"lever"/"ashby",
        which SOURCE_MAP routes to the external apply flow directly. This
        delegate keeps behavior identical if a job ever carries source "ats".
        """
        from .jobright import JobrightScraper

        scraper = JobrightScraper(self.config)
        submitted = await scraper.apply(job, auto_submit=auto_submit)
        self.last_apply_status = scraper.last_apply_status
        self.last_apply_detail = scraper.last_apply_detail
        return submitted
