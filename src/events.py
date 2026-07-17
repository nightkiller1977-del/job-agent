"""Phase 2a — structured, per-run event log.

One run (e.g. an apply batch) gets a `run_id` and an append-only JSONL stream at
`state/runs/{run_id}.jsonl`. Events also flow to the existing telemetry logging
seam (`telemetry.setup()` attaches a Loki handler to the root logger), so a
structured `logging` record ships to Loki without a second transport.

Design rules honoured (from the plan / review):
  - SQLite stays the source of truth for application state; this is an *audit*
    stream, not a store.
  - Every event carries: schema_version, run_id, ts, agent, event, and (when
    relevant) attempt_id, job_id, vendor, phase, outcome, duration_ms.
  - Redaction is mandatory: resumes, phone numbers, emails, screening answers,
    cookies, secrets, and raw model prompts never enter an event.
  - emit() never raises — logging must not change an application result.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RUNS_DIR = Path(__file__).resolve().parent.parent / "state" / "runs"

_log = logging.getLogger("job_agent.events")

# Field-name fragments whose values must never be logged. Matched case-insensitively
# as substrings so `resume_path`, `applicant_email`, `raw_prompt`, etc. are all caught.
_SENSITIVE = (
    "resume", "cover_letter", "cover-letter", "phone", "email", "answer",
    "cookie", "prompt", "password", "secret", "token", "profile", "ssn",
    "address", "dob", "birth",
)
_MAX_STR = 300


def _sanitize(fields: dict) -> dict:
    clean: dict[str, Any] = {}
    for k, v in fields.items():
        if any(s in k.lower() for s in _SENSITIVE):
            continue  # drop sensitive fields entirely
        if isinstance(v, str) and len(v) > _MAX_STR:
            v = v[:_MAX_STR] + "…"
        clean[k] = v
    return clean


class RunLog:
    """One run's structured event stream. Create once per pipeline invocation and
    pass it down; all attempts in the run share the run_id."""

    def __init__(self, agent: str = "job-agent", run_id: str | None = None,
                 runs_dir: Path | str | None = None):
        self.agent = agent
        self.run_id = run_id or uuid.uuid4().hex[:16]
        self.dir = Path(runs_dir) if runs_dir else RUNS_DIR
        self.path = self.dir / f"{self.run_id}.jsonl"

    def emit(self, event: str, **fields) -> dict:
        """Append one structured event. Returns the record (handy for tests).
        Never raises."""
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "ts": time.time(),
            "agent": self.agent,
            "event": event,
        }
        record.update(_sanitize(fields))
        self._write(record)
        self._ship(event, record)
        return record

    # ---- sinks (each isolated so one failing never breaks the flow) ----------
    def _write(self, record: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=str)
            # append is atomic for small lines on POSIX; open in append mode.
            with open(self.path, "a") as f:
                f.write(line + "\n")
        except Exception:  # audit logging must never break an apply
            pass

    def _ship(self, event: str, record: dict) -> None:
        try:
            # tags mirror telemetry.model_span so Loki queries are consistent
            _log.info(event, extra={"tags": {k: record[k] for k in (
                "run_id", "event", "agent") if k in record}})
        except Exception:
            pass


def read_run(run_id: str, runs_dir: Path | str | None = None) -> list[dict]:
    """Read back a run's events (newest sink for stats/tests)."""
    path = (Path(runs_dir) if runs_dir else RUNS_DIR) / f"{run_id}.jsonl"
    out: list[dict] = []
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
    except FileNotFoundError:
        pass
    return out
