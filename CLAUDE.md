# job-agent — Claude Code instructions

@AGENTS.md

`AGENTS.md` is the shared engineering source of truth. The additional detail below is retained for Claude Code and must not weaken it.

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

When a subagent, tool, or external report cites a `file:line` claim about this repo, verify it against the actual source before presenting or acting on it. Read the referenced file, run the referenced grep, or execute the referenced test yourself. If verification is skipped due to scope, say so explicitly.

Applies equally to subagent investigation reports, GitHub Copilot Code Review, AI Commander Code Review Agent, and OpenHands reviews, static-analysis output, and AI-authored suggestions that would land in a PR.

## Claude-specific side-effect rule

Ask before irreversible/shared actions unless the user explicitly authorized that exact action in the current task: merging, force-pushing/history rewrite, production deploy/config changes, secret-store writes, live employer submissions/messages, destructive data changes, or permission/budget changes. Branch commits, tests, and opening a PR do not require an extra confirmation.

## Ecosystem and tool boundary

This repository is `job-agent`: Python/Playwright job discovery, scoring, tailoring, ATS automation, receipt verification, and duplicate-submission-safe reconciliation. Before cross-repository work, use the complete repository/ownership and infrastructure map in `AGENTS.md`.

- Resolve managed credentials only through the authorized `aicc-secrets` SOPS+age flow; never reveal plaintext or reuse another service's scoped MongoDB credential.
- Treat `aicc-secrets/render-sync-map.json` as the Render distribution allowlist, not as a catalog granting every service access. Its Job Agent mapping translates the store key `MONGODB_URI_JOB_AGENT_DASHBOARD` to the runtime name `MONGODB_URI`.
- Treat Tailscale as private transport, not authorization. Current operational uses are Job Agent noVNC re-auth and supported Code Review Agent Ollama tunnels; the wider Coordinator/worker mesh is not complete until verified.
- Reuse Desktop/Coordinator/OpenRouter/Brain Memory/Model Intelligence/metrics authorities rather than recreating them in this repo.
- Verify implemented versus planned behavior from current source, tests, deployment configuration, and runtime evidence.
- Start delegated fix sessions (review-finding fixes, CI fixes, merging the base branch in) on Sonnet. Use Opus only for initial design/implementation or when the user asks for it.
