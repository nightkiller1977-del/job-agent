from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_UNANSWERED_FILE = Path(__file__).parent.parent.parent / "state" / "unanswered_questions.json"


class UnansweredQuestionsTracker:
    """Captures screening questions the agent cannot answer during ATS application flows."""

    def __init__(self, file_path: Path | str | None = None):
        if file_path is None:
            self.file_path = DEFAULT_UNANSWERED_FILE
        else:
            self.file_path = Path(file_path)

    def _load(self) -> list[dict]:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save(self, questions: list[dict]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(questions, f, indent=2)

    def record_unanswered(
        self,
        question_text: str,
        field_type: str = "text",
        job: Optional[Dict[str, Any]] = None,
        options: Optional[List[str]] = None,
    ) -> None:
        """Record a question that could not be answered."""
        if not question_text or len(question_text.strip()) < 3:
            return

        cleaned_q = question_text.strip()
        questions = self._load()

        # Check if already present
        for item in questions:
            if item.get("question", "").lower() == cleaned_q.lower():
                item["last_seen"] = datetime.now(timezone.utc).isoformat()
                item["occurrence_count"] = item.get("occurrence_count", 1) + 1
                if job:
                    item["last_job"] = f"{job.get('title', '')} @ {job.get('company', '')}"
                self._save(questions)
                return

        new_entry = {
            "question": cleaned_q,
            "field_type": field_type,
            "options": options or [],
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "occurrence_count": 1,
            "resolved": False,
            "answer": None,
            "last_job": f"{job.get('title', '')} @ {job.get('company', '')}" if job else "",
            "source": job.get("source", "") if job else "",
        }
        questions.append(new_entry)
        self._save(questions)
        console.print(f"[yellow]⚠️ Logged unanswered screening question:[/yellow] {cleaned_q[:80]}")

    def list_unanswered(self, unresolved_only: bool = True) -> list[dict]:
        """Return list of captured questions."""
        questions = self._load()
        if unresolved_only:
            return [q for q in questions if not q.get("resolved")]
        return questions

    def display_table(self) -> None:
        """Render a formatted table in terminal of pending questions."""
        unresolved = self.list_unanswered(unresolved_only=True)
        if not unresolved:
            console.print("[green]No unanswered screening questions pending! All clear.[/green]")
            return

        table = Table(title=f"Pending Screening Questions ({len(unresolved)})")
        table.add_column("Count", justify="right", style="cyan", no_wrap=True)
        table.add_column("Question", style="white")
        table.add_column("Type", style="yellow")
        table.add_column("Last Role / Company", style="dim")

        for q in unresolved:
            table.add_row(
                str(q.get("occurrence_count", 1)),
                q.get("question", ""),
                q.get("field_type", "text"),
                q.get("last_job", ""),
            )

        console.print(table)


# Global singleton instance
tracker = UnansweredQuestionsTracker()
