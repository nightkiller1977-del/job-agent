import json
import os
import sqlite3
from pathlib import Path
from rich.console import Console
from rich.markdown import Markdown

console = Console()

class GapAnalyzer:
    def __init__(self, db_path: str = "state/jobs.db", profile_path: str = "state/profile.json"):
        self.db_path = Path(db_path).expanduser()
        self.profile_path = Path(profile_path).expanduser()
        self.profile = {}
        self.load_profile()

    def load_profile(self) -> None:
        if self.profile_path.exists():
            try:
                with open(self.profile_path, "r") as f:
                    self.profile = json.load(f)
            except Exception as e:
                console.print(f"[red]Error loading profile:[/red] {e}")

    def _get_target_jobs(self) -> list[dict]:
        """Fetch all approved or applied jobs from SQLite."""
        if not self.db_path.exists():
            return []
        
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Fetch jobs that were approved or applied
            cursor.execute(
                "SELECT title, company, description, score, flags FROM jobs WHERE status IN ('approved', 'applied') OR score >= 70"
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            console.print(f"[red]Failed to query jobs database:[/red] {e}")
            return []

    async def run_analysis(self) -> str:
        """Run AI gap analysis comparing profile skills against approved/applied job requirements."""
        jobs = self._get_target_jobs()
        if not jobs:
            console.print("[yellow]No approved, applied, or high-scoring jobs found in the database. Run discover/review first.[/yellow]")
            return ""

        console.print(f"[cyan]Analyzing skill gaps across {len(jobs)} target job(s)...[/cyan]")

        # Prepare existing skills
        existing_skills = self.profile.get("skills", [])
        
        # Prepare job requirements text summary
        job_summaries = []
        for i, j in enumerate(jobs[:15]): # limit to top 15 to fit within context length safely
            job_summaries.append(
                f"Job #{i+1}: {j.get('title')} @ {j.get('company')}\n"
                f"Description excerpt:\n{j.get('description', '')[:1000]}"
            )
        jobs_text = "\n\n".join(job_summaries)

        from src.model_client import ModelClient
        model_client = ModelClient()

        prompt = (
            "You are an executive talent assessor and career strategist.\n"
            "Analyze the candidate's existing skills against the requirements of their target job openings.\n"
            "Identify the gaps and produce a structured, actionable upskilling roadmap.\n\n"
            f"CANDIDATE EXISTING SKILLS:\n{', '.join(existing_skills)}\n\n"
            f"TARGET JOB POSTINGS:\n{jobs_text}\n\n"
            "Produce a comprehensive Markdown report. Include the following sections:\n"
            "1. # Upskilling & Skill Gap Analysis\n"
            "2. ## Target Roles Overview (brief description of trends in target roles)\n"
            "3. ## Key Missing Competencies (bullet points listing technologies, tools, or concepts requested but missing from their skills list)\n"
            "4. ## Recommended Learning Action Plan (specific courses, certifications, or self-directed project ideas to close these gaps)\n"
            "Do not include any prose before or after the markdown report. Output only valid markdown."
        )

        try:
            report_md = await model_client.complete(
                messages=[{"role": "user", "content": prompt}],
                task_type="reasoning",
                max_tokens=1500
            )
            
            # Clean think blocks if present
            import re
            report_md = re.sub(r"<think>.*?</think>\s*", "", report_md, flags=re.DOTALL)
            
            # Save report locally
            report_path = Path("state/upskilling_report.md")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report_md, encoding="utf-8")
            
            console.print("\n" + "="*40)
            console.print(Markdown(report_md))
            console.print("="*40 + "\n")
            console.print(f"[green]Upskilling report saved to {report_path} ✓[/green]")
            return report_md
        except Exception as e:
            console.print(f"[red]Failed to run gap analysis model:[/red] {e}")
            return ""
