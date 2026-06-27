# job-agent — Claude Code instructions

## CRITICAL: Chrome profile isolation

**Never change `_profile_dir` in `src/sources/base.py` to point at the main Chrome profile.**

The correct implementation is:
```python
@property
def _profile_dir(self) -> Path:
    d = SESSIONS_DIR / f"{self.name}_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

Each scraper gets an isolated profile under `state/sessions/<name>_profile/`.

**Do NOT:**
- Return `Path("/Users/<you>/Library/Application Support/Google/Chrome")`
- Add `--profile-directory=Default` to Chrome args
- Add any guard in `_clear_profile_locks()` that skips lock removal for the main profile

**Why:** Chrome holds an exclusive lock on the main profile while it's running. Pointing Playwright at it causes `ProcessSingleton` / `database is locked` failures every time.

## Security — never commit these files
- `settings-v3.json`
- `.env`
- `state/jobs.db`
- `state/profile.json`
- `state/sessions/`
- `state/tailored_resumes/`

## Branch protection
Never push directly to `main`. Always use a feature branch.
Never use `--no-verify` to skip pre-commit hooks.
