import subprocess
import sys
from pathlib import Path


def test_launchd_parity_no_secrets_shadowing():
    """Simulate launchd executing `python src/main.py`, where `src/` is first on
    sys.path. A module named `src/secrets.py` would shadow the standard library
    `secrets` module and break `pyotp` (used for USAJOBS TOTP), silently failing
    auto-login on every scheduled run. This guards against that regression.
    """
    repo_root = Path(__file__).parent.parent
    former_secrets_path = str((repo_root / "src" / "secrets.py").resolve())

    # 1. The real entry point must import cleanly (src/ is sys.path[0] here).
    res_main = subprocess.run(
        [sys.executable, "src/main.py", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_main.returncode == 0, f"src/main.py --help failed: {res_main.stderr}"
    assert "ImportError" not in res_main.stderr
    assert "AttributeError" not in res_main.stderr

    # 2. With src/ as the working dir (sys.path[0]), `import secrets` must resolve
    #    to the standard library, not a repo module.
    check_script = (
        "import secrets\n"
        "assert hasattr(secrets, 'token_hex'), 'stdlib secrets shadowed'\n"
        f"assert getattr(secrets, '__file__', '') != {former_secrets_path!r}, 'src/secrets.py is shadowing'\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", check_script],
        cwd=str(repo_root / "src"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, f"stdlib secrets resolution failed: {res.stdout}\n{res.stderr}"
