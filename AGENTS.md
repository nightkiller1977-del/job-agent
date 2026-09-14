# AI Commander job-agent instructions

## Engineering principles

1. Follow existing architecture, naming, contracts, and patterns before introducing a new pattern.
2. Reuse existing services, helpers, adapters, schemas, configuration, tests, and contracts before creating duplicate functionality.
3. Prefer the simplest correct solution; add abstraction only for a demonstrated need.
4. Keep changes focused and avoid unrelated refactors.
5. Security/privacy first: least privilege, validated inputs, protected secrets, explicit trust boundaries, fail-closed authorization/policy behavior.
6. Fix root causes, not symptoms. Reproduce failures and change the component that owns the behavior.
7. Preserve approval, policy, budget, audit, permission, and protected-path boundaries.
8. Make side effects restart-safe and idempotent; reconcile ambiguous outcomes before retrying.
9. Treat model output as a proposal, not proof. Verify with source, tests, receipts, APIs, or other deterministic evidence.
10. Add regression coverage when practical. Run targeted tests, then the relevant broader suite. Never bypass hooks/tests/security checks merely to get green.
11. Prefer existing dependencies and infrastructure.
12. Do not claim completion without validation evidence; state remaining risk.
13. Review priority: correctness/security, reuse, simplicity, validation evidence.

## job-agent invariants

- Never point Playwright/scrapers at the user's main Chrome profile. Each scraper must use its isolated profile under `state/sessions/<name>_profile/`; do not add `--profile-directory=Default` or bypass profile-lock cleanup semantics to use the main profile.
- Never commit `settings-v3.json`, `.env`, `state/jobs.db`, `state/profile.json`, `state/sessions/`, or `state/tailored_resumes/`.
- Never push directly to `main`; use a feature branch and PR. Never use `--no-verify` to skip pre-commit/pre-push hooks.
- Verify file/line and behavior claims from subagents, tools, OpenHands, Codex, or external reports against actual source/tests before acting on or quoting them.
- Employer-facing submissions and messages are external side effects. Do not claim a submission succeeded without durable evidence. Reconcile an ambiguous outcome before retrying to avoid duplicate applications.

## Skills

OpenHands skills live under `.agents/skills/`. Claude Code skills live under `.claude/skills/`. Skills supplement these rules and do not override them.