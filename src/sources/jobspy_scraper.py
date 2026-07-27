import asyncio
import json
import sys
import os
from datetime import datetime
from rich.console import Console
from .base import BaseScraper

console = Console()

class JobSpyScraper(BaseScraper):
    name = "jobspy"

    async def scrape(self) -> list[dict]:
        search = self.config.get("search_settings", {})
        keywords = search.get("keywords", "")
        location = search.get("location", "Remote")
        
        if not keywords:
            console.print("[yellow]JobSpy: No keywords configured. Skipping.[/yellow]")
            return []

        console.print(f"[blue]JobSpy:[/blue] Scraping Glassdoor, ZipRecruiter, Google for '{keywords}' in '{location}'…")

        # Run bridge script under the host python3 (which is python 3.12 with jobspy installed)
        python_bin = "/usr/local/bin/python3"
        bridge_path = os.path.join(os.path.dirname(__file__), "jobspy_bridge.py")

        cmd = [
            python_bin,
            bridge_path,
            "--keywords", keywords,
            "--location", location,
            "--sites", "glassdoor,zip_recruiter,google",
            "--limit", "30"
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
        except Exception as e:
            console.print(f"[red]JobSpy: Failed to spawn bridge subprocess: {e}[/red]")
            return []

        if process.returncode != 0:
            err_msg = stderr.decode().strip()
            console.print(f"[red]JobSpy: Bridge failed with code {process.returncode}: {err_msg}[/red]")
            return []

        try:
            raw_jobs = json.loads(stdout.decode())
        except Exception as e:
            console.print(f"[red]JobSpy: Failed to parse bridge output: {e}[/red]")
            return []

        now = datetime.utcnow().isoformat()
        jobs = []
        for item in raw_jobs:
            url = item.get("url", "")
            if not url:
                continue
            
            # Map site name to standardized source names
            site = item.get("site", "google")
            if site == "zip_recruiter":
                source_name = "ziprecruiter"
            else:
                source_name = site # glassdoor, google

            jobs.append({
                "job_id": self._make_job_id(url),
                "source": source_name,
                "title": item.get("title", ""),
                "company": item.get("company", ""),
                "location": item.get("location", ""),
                "salary_raw": str(item.get("salary", "")),
                "remote_type": "remote" if "remote" in item.get("location", "").lower() else "unknown",
                "url": url,
                "description": item.get("description", "") or f"{item.get('title')} at {item.get('company')}",
                "discovered_at": now,
            })

        return jobs
