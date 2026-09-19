"""Regression coverage for pre-push hooks run from linked worktrees."""

import os
import shutil
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "hooks" / "pre-push"
INSTALLER = REPO_ROOT / "scripts" / "install-hooks.sh"


def _clean_git_env(env: dict) -> dict:
    local_env_vars = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {key: value for key, value in env.items() if key not in set(local_env_vars)}


def _make_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    env = _clean_git_env({**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(primary), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env=env,
    )
    subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "feature", str(linked)], check=True, env=env)
    return primary, linked


def test_hook_uses_primary_worktree_venv_when_linked_worktree_has_none(tmp_path):
    bash = shutil.which("bash")
    assert bash
    primary, linked = _make_repo_with_worktree(tmp_path)
    pytest = primary / ".venv" / "bin" / "pytest"
    pytest.parent.mkdir(parents=True)
    pytest.write_text(f"#!{bash}\nprintf 'PRIMARY_VENV\\n'\n")
    pytest.chmod(pytest.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [bash, str(HOOK)],
        cwd=linked,
        env=_clean_git_env({**os.environ}),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert "PRIMARY_VENV" in result.stdout


def test_installer_uses_git_hook_path_for_linked_worktrees(tmp_path):
    bash = shutil.which("bash")
    assert bash
    primary, linked = _make_repo_with_worktree(tmp_path)
    (linked / "scripts" / "hooks").mkdir(parents=True)
    destination = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--git-path", "hooks/pre-push"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_git_env({**os.environ}),
    ).stdout.strip()
    (linked / "scripts" / "hooks" / "pre-push").write_text("#!/usr/bin/env bash\nexit 0\n")
    (linked / "scripts" / "install-hooks.sh").write_text(INSTALLER.read_text())
    (linked / "scripts" / "install-hooks.sh").chmod(0o755)

    result = subprocess.run(
        [bash, str(linked / "scripts" / "install-hooks.sh")],
        cwd=linked,
        env=_clean_git_env({**os.environ}),
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert Path(destination).read_text() == "#!/usr/bin/env bash\nexit 0\n"
