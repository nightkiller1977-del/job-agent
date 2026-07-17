import subprocess
import sys
from pathlib import Path

def test_launchd_parity_no_secrets_shadowing():
    """
    Simulate launchd executing `python src/main.py` where `src/` is first in sys.path.
    If `src/secrets.py` exists, it will shadow the standard library `secrets` module,
    breaking `pyotp` and failing this test.
    """
    repo_root = Path(__file__).parent.parent
    former_secrets_path = str((repo_root / "src" / "secrets.py").resolve())
    
    # 1. Test that the actual entry point doesn't crash with an ImportError.
    # We use '--help' because the user requested retaining it to verify the complete launchd CLI path.
    res_main = subprocess.run(
        [sys.executable, "src/main.py", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15
    )
    assert res_main.returncode == 0, f"src/main.py --help failed: {res_main.stderr}"
    assert "ImportError" not in res_main.stderr
    assert "AttributeError" not in res_main.stderr
    
    # 2. Run an explicit check recreating the exact launch path conditions
    check_script = (
        "import sys\n"
        "import secrets\n"
        "assert hasattr(secrets, 'token_hex'), 'Standard library secrets shadowed'\n"
        "file_path = getattr(secrets, '__file__', '')\n"
        f"assert file_path != {repr(former_secrets_path)}, 'src/secrets.py is shadowing'\n"
    )
    
    res_explicit = subprocess.run(
        [sys.executable, "-c", check_script],
        cwd=str(repo_root / "src"),  # Execute with src/ as the current working directory so sys.path[0] is src/
        capture_output=True,
        text=True,
        timeout=10
    )
    
    if res_explicit.returncode != 0:
        print("STDOUT:", res_explicit.stdout)
        print("STDERR:", res_explicit.stderr)
        
    assert res_explicit.returncode == 0
