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

## Source verification before quoting

When a subagent, tool, or external report cites a `file:line` claim about this repo, verify it against the actual source before presenting or acting on it. Read the referenced file, run the referenced grep, or execute the referenced test yourself. Quoting a claim you have not verified — especially regex behavior, control-flow assertions, or "this pattern already matches X" — is exactly how false-confidence proposals ("just tighten the regex") get past review. If verification is skipped due to time or scope, say so explicitly.

Applies equally to: subagent investigation reports (Explore, general-purpose), Codex/OpenHands reviews, static-analysis output, and any AI-authored suggestion that would land in a PR. Regression tests derived from a real observed failure are the only durable check; adopted-without-verification claims are not.
