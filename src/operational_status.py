"""Read-only, timestamped host readiness information for operators."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True,
            text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _scheduler_last_result() -> str:
    """Read, never mutate, the scheduler's most recent result when available."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3)
            return "loaded" if result.returncode == 0 and "com.jobagent.apply" in result.stdout else "not_loaded"
        except Exception:
            return "unavailable"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "jobagent-apply.service", "--property=Result", "--value"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or "unknown" if result.returncode == 0 else "not_loaded"
    except Exception:
        return "unavailable"


def collect_operational_status(root: Path | None = None) -> dict[str, object]:
    root = root or Path(__file__).resolve().parent.parent
    pinned = os.environ.get("JOBAGENT_RUNTIME_BRANCH", "").strip() or _git(root, "config", "--get", "jobagent.runtimeBranch")
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "runtime_branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "pinned_branch": pinned,
        "venv_python_present": (root / ".venv" / "bin" / "python3").is_file(),
        "config_present": (root / "config.json").is_file(),
        "profile_present": (root / "state" / "profile.json").is_file(),
        "scheduler_last_result": _scheduler_last_result(),
    }


def show_operational_status(root: Path | None = None) -> None:
    print(json.dumps(collect_operational_status(root), sort_keys=True))
