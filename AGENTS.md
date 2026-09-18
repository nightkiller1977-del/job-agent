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

## Jira work tracking is mandatory

Jira is the planning and source-of-truth layer for AI Commander implementation work. Before any code-changing implementation, repair, refactor, or feature work, use the `aicc-jira-work-tracking` skill.
- Site: `https://shoalinwu.atlassian.net`; active project: `ACES`; issue pattern: `https://shoalinwu.atlassian.net/browse/<KEY>`.
- `KAN` is legacy/historical unless explicitly requested.
- Every work item must have a Jira ticket and an appropriate parent Epic/roadmap ticket. Do not work orphan tickets.
- Read/follow the parent before coding. If child/request conflicts with parent, reconcile Jira first.
- Keep Jira current through work start, findings/blockers/decisions, PR open, validation, and completion.
- Every PR identifies both work ticket and parent; Jira references the PR.
- Out-of-scope discoveries get separate Jira child tickets under the appropriate parent.
- Do not mark `Done` before required validation/delivery/reconciliation evidence exists.
- Component/repository routing is Coordinator-authoritative; do not invent mappings.
- Never store Jira email, API tokens, Cloud ID, enrollment secrets, or credentials in repo guidance, prompts, logs, tests, or PR text.

## job-agent invariants

- Never point Playwright/scrapers at the user's main Chrome profile. Each scraper must use its isolated profile under `state/sessions/<name>_profile/`; do not add `--profile-directory=Default` or bypass profile-lock cleanup semantics to use the main profile.
- Never commit `settings-v3.json`, `.env`, `state/jobs.db`, `state/profile.json`, `state/sessions/`, or `state/tailored_resumes/`.
- Never push directly to `main`; use a feature branch and PR. Never use `--no-verify` to skip pre-commit/pre-push hooks.
- Verify file/line and behavior claims from subagents, tools, OpenHands, Codex, or external reports against actual source/tests before acting on or quoting them.
- Employer-facing submissions and messages are external side effects. Do not claim a submission succeeded without durable evidence. Reconcile an ambiguous outcome before retrying to avoid duplicate applications.

## Skills

OpenHands skills live under `.agents/skills/`. Claude Code skills live under `.claude/skills/`. Skills supplement these rules and do not override them.

## Pull request review policy

- **GitHub Copilot Code Review is the only AI pull-request reviewer to request or enable for this repository.**
- When an AI review is needed, request **Copilot** through GitHub's normal reviewer mechanism.
- Do **not** request, invoke, enable, or depend on Codex/OpenAI/ChatGPT pull-request review, including `@codex review`.
- Do **not** trigger the AI Commander Code Review Agent or another AI reviewer for PR review unless the repository owner explicitly changes this policy.
- Historical review comments from other reviewers may remain as evidence, but new review rounds must use Copilot only.
- Copilot review does not replace deterministic merge evidence: required CI, tests, lint, security checks, and repository-specific validation still must pass.
