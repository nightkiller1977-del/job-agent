"""Regression test for scripts/hooks/notify-push-blocked.sh.

A failing pre-push run passes the current git branch name into this script
as the notification detail. Git branch names can legally contain '"' and
AppleScript operators (e.g. `review" & do shell script "..." & "`), so the
detail must reach osascript only as a process argument (argv) — never
interpolated into the AppleScript source text, or a malicious/local branch
name could run arbitrary AppleScript as the developer.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "hooks" / "notify-push-blocked.sh"

MALICIOUS_DETAIL = 'review" & do shell script "touch /tmp/pwned-by-regression-test" & "'

EXPECTED_APPLESCRIPT = (
    "on run argv\n"
    '  display notification (item 1 of argv) with title "Job Agent" subtitle "push blocked"\n'
    "end run\n"
)


def test_malicious_branch_name_reaches_osascript_only_as_argv(tmp_path):
    bash = shutil.which("bash")
    cat = shutil.which("cat")
    assert bash and cat, "bash and cat must be available to run this test"

    record_argv = tmp_path / "osascript.argv"
    record_stdin = tmp_path / "osascript.stdin"

    # Fake `osascript` that just records what it was called with — an
    # absolute-path shebang and absolute `cat` so it needs no PATH lookups
    # of its own (the real PATH below is deliberately restricted).
    fake_osascript = tmp_path / "osascript"
    fake_osascript.write_text(
        f"#!{bash}\n"
        "shift || true\n"  # drop the leading '-' (stdin-script marker)
        f'printf \'%s\\n\' "$@" > "{record_argv}"\n'
        f'{cat} > "{record_stdin}"\n'
    )
    fake_osascript.chmod(0o755)

    env = dict(os.environ)
    # Only the fake osascript resolves here — no real notify-send, so the
    # hook is forced down the osascript branch under test.
    env["PATH"] = str(tmp_path)

    result = subprocess.run(
        [bash, str(HOOK), MALICIOUS_DETAIL],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert record_argv.exists(), f"fake osascript was never invoked; stderr={result.stderr!r}"

    argv_lines = record_argv.read_text().splitlines()
    stdin_content = record_stdin.read_text()

    # The untrusted branch text must show up ONLY as a positional argument...
    assert argv_lines == [MALICIOUS_DETAIL]
    # ...and the AppleScript program handed to osascript must be the fixed,
    # constant source — completely unaffected by the malicious value.
    assert stdin_content == EXPECTED_APPLESCRIPT
    assert MALICIOUS_DETAIL not in stdin_content
