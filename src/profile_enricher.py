import json
import os
import re
from pathlib import Path
import httpx
from rich.console import Console
from rich.table import Table

console = Console()

class ProfileEnricher:
    def __init__(self, profile_path: str = "state/profile.json"):
        self.profile_path = Path(profile_path)
        self.profile = {}
        self.load_profile()

    def load_profile(self) -> None:
        if self.profile_path.exists():
            try:
                with open(self.profile_path, "r") as f:
                    self.profile = json.load(f)
            except Exception as e:
                console.print(f"[red]Error loading profile:[/red] {e}")
        else:
            self.profile = {"personal_info": {}, "social_links": {}, "skills": [], "work_history": [], "education": []}

    def save_profile(self) -> None:
        try:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.profile_path, "w") as f:
                json.dump(self.profile, f, indent=2)
            console.print(f"[green]Profile successfully saved to {self.profile_path} ✓[/green]")
        except Exception as e:
            console.print(f"[red]Error saving profile:[/red] {e}")

    async def enrich_from_github(self, github_username: str) -> None:
        """Fetches public repo info from GitHub for github_username and extracts new skills."""
        console.print(f"[cyan]Fetching GitHub profile and repos for '{github_username}'...[/cyan]")
        
        headers = {"Accept": "application/vnd.github.v3+json"}
        token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                # 1. Fetch User Bio
                user_res = await client.get(f"https://api.github.com/users/{github_username}", headers=headers)
                user_res.raise_for_status()
                user_data = user_res.json()
                bio = user_data.get("bio") or ""
                
                # 2. Fetch Repos
                repos_res = await client.get(f"https://api.github.com/users/{github_username}/repos?per_page=50&sort=updated", headers=headers)
                repos_res.raise_for_status()
                repos = repos_res.json()
            except Exception as e:
                console.print(f"[red]Failed to fetch data from GitHub API:[/red] {e}")
                return

        if not repos:
            console.print("[yellow]No repositories found for this GitHub user.[/yellow]")
            return

        # Prepare summary of repos
        repo_summaries = []
        for r in repos:
            name = r.get("name", "")
            desc = r.get("description") or ""
            lang = r.get("language") or ""
            topics = ", ".join(r.get("topics", []))
            repo_summaries.append(f"- Repo: {name}\n  Description: {desc}\n  Primary Language: {lang}\n  Topics: {topics}")

        repos_text = "\n".join(repo_summaries)
        
        # 3. Call AI Model Client to extract skills
        console.print("[cyan]Analyzing repository contents via Claude...[/cyan]")
        
        from src.model_client import ModelClient
        model_client = ModelClient()
        
        prompt = (
            "You are a technical profile auditor.\n"
            "Analyze the candidate's GitHub user bio and repository summaries to extract a list of professional skills, frameworks, tools, and programming languages they possess.\n\n"
            f"GITHUB USER BIO:\n{bio}\n\n"
            f"REPOSITORIES:\n{repos_text[:4000]}\n\n"
            "Output ONLY a valid JSON list of strings, representing the identified skills (e.g. [\"Python\", \"FastAPI\", \"Docker\"]). No markdown formatting, no other text."
        )

        try:
            raw_response = await model_client.complete(
                messages=[{"role": "user", "content": prompt}],
                task_type="reasoning",
                max_tokens=500
            )
            
            # Clean response
            raw_response = re.sub(r"<think>.*?</think>\s*", "", raw_response, flags=re.DOTALL)
            raw_response = re.sub(r"^```[a-z]*\n?", "", raw_response.strip())
            raw_response = re.sub(r"\n?```$", "", raw_response)
            m = re.search(r"\[.*\]", raw_response, re.DOTALL)
            if m:
                raw_response = m.group()
                
            extracted_skills = json.loads(raw_response)
        except Exception as e:
            console.print(f"[red]Failed to extract skills using AI model:[/red] {e}")
            return

        if not isinstance(extracted_skills, list):
            console.print("[red]Invalid response format from AI model (expected a list of strings).[/red]")
            return

        # Clean and filter extracted skills
        extracted_skills = [str(s).strip() for s in extracted_skills if s and str(s).strip()]
        
        # Merge into existing skills
        existing_skills = {s.lower(): s for s in self.profile.get("skills", [])}
        new_skills_added = []
        for s in extracted_skills:
            if s.lower() not in existing_skills:
                new_skills_added.append(s)
                self.profile.setdefault("skills", []).append(s)

        # Print visual summary
        if new_skills_added:
            table = Table(title="Profile Skills Expanded")
            table.add_column("Added Skill", style="green")
            for skill in new_skills_added:
                table.add_row(skill)
            console.print(table)
            
            # Save updated profile
            self.save_profile()
        else:
            console.print("[yellow]No new skills identified. Profile is already up-to-date ✓[/yellow]")
