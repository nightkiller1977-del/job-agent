"""scripts/run-scheduled.sh — the fail-closed branch guard for scheduled runs.

A scheduled discover/apply must never execute whatever branch a dev session
left checked out: with JOBAGENT_RUNTIME_BRANCH set, the launcher refuses
(exit 1) on a drifted checkout and proceeds only on the pinned branch.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run-scheduled.sh"


def _make_repo(tmp_path, branch: str) -> Path:
    """A throwaway git repo with scripts/run-scheduled.sh and a stub venv
    python that records it ran instead of launching the real agent."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / "src").mkdir()

    script = repo / "scripts" / "run-scheduled.sh"
    script.write_text(SCRIPT.read_text())
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    stub = repo / ".venv" / "bin" / "python3"
    stub.write_text("#!/usr/bin/env bash\necho \"RAN:$*\"\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
    return repo


def _run(repo: Path, cmd: str, pinned: str | None):
    env = {**os.environ}
    if pinned is None:
        env.pop("JOBAGENT_RUNTIME_BRANCH", None)
    else:
        env["JOBAGENT_RUNTIME_BRANCH"] = pinned
    return subprocess.run(
        [str(repo / "scripts" / "run-scheduled.sh"), cmd],
        capture_output=True, text=True, env=env,
    )


def test_script_is_executable_in_repo():
    assert os.access(SCRIPT, os.X_OK), "run-scheduled.sh must keep its exec bit (PR #101)"


def test_runs_on_the_pinned_branch(tmp_path):
    repo = _make_repo(tmp_path, "main-rewrite")
    res = _run(repo, "apply", pinned="main-rewrite")
    assert res.returncode == 0
    assert "RAN:src/main.py apply --auto-submit" in res.stdout


def test_refuses_on_a_drifted_branch(tmp_path):
    repo = _make_repo(tmp_path, "main-rewrite")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat/dev-experiment"],
                   check=True)
    res = _run(repo, "apply", pinned="main-rewrite")
    assert res.returncode == 1
    assert "REFUSING" in res.stderr
    assert "RAN:" not in res.stdout, "the agent must not launch on a drifted checkout"


def test_unset_pin_skips_the_guard(tmp_path):
    repo = _make_repo(tmp_path, "whatever-branch")
    res = _run(repo, "discover", pinned=None)
    assert res.returncode == 0
    assert "RAN:src/main.py discover --no-review" in res.stdout


def test_unknown_command_is_rejected(tmp_path):
    repo = _make_repo(tmp_path, "main-rewrite")
    res = _run(repo, "delete-everything", pinned="main-rewrite")
    assert res.returncode == 2
    assert "RAN:" not in res.stdout


def test_templates_route_through_the_guard():
    """Both platform templates must call run-scheduled.sh and carry the
    __RUNTIME_BRANCH__ placeholder the installers render."""
    for rel in ("systemd/jobagent-apply.service", "systemd/jobagent-discover.service",
                "launchd/com.jobagent.apply.plist", "launchd/com.jobagent.discover.plist"):
        text = (REPO_ROOT / rel).read_text()
        assert "run-scheduled.sh" in text, f"{rel} must launch via the guard script"
        assert "__RUNTIME_BRANCH__" in text, f"{rel} must pin the runtime branch"
