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
- Verify file/line and behavior claims from subagents, tools, OpenHands, GitHub Copilot Code Review, the AI Commander Code Review Agent, or external reports against actual source/tests before acting on or quoting them.
- Employer-facing submissions and messages are external side effects. Do not claim a submission succeeded without durable evidence. Reconcile an ambiguous outcome before retrying to avoid duplicate applications.

## Skills

OpenHands skills live under `.agents/skills/`. Claude Code skills live under `.claude/skills/`. Skills supplement these rules and do not override them.

## Pull request review policy

- **Use GitHub Copilot Code Review and the AI Commander Code Review Agent for AI-assisted pull-request review.**
- When an external AI review is needed, request **GitHub Copilot** through GitHub's normal reviewer mechanism.
- Also inspect the **AI Commander Code Review Agent** result when it is available; treat its findings as hypotheses to verify against the current head, source, tests, and deterministic evidence.
- Do **not** request, invoke, enable, or depend on Codex/OpenAI/ChatGPT pull-request review, including `@codex review`.
- Historical Codex or other reviewer comments may remain as evidence, but do not trigger new Codex review rounds.
- Copilot and AI Commander review do not replace deterministic merge evidence: required CI, tests, lint, security checks, and repository-specific validation still must pass.

## AI Commander ecosystem map and shared infrastructure

**This repository:** `job-agent` owns Python/Playwright job discovery, scoring, tailoring, ATS automation, receipt verification, and duplicate-submission-safe reconciliation. Do not move another repository's authority here or build a parallel scheduler, policy engine, secret store, memory system, model gateway, or incident framework.

| Repository | Authoritative responsibility |
|---|---|
| `Ai-Command-Center-Desktop-App` | Operator UI and local control plane; approvals, workflows, Guardian/Repair, MCP, model admission, OpenHands. |
| `Aicc-Coordinator` | Durable cross-host work authority: workers, leases, operations, approvals, Jira, fleet health, incidents, evidence. |
| `AI-OpenRouter` | Only shared external-model egress/budget gateway; not the default inference path. |
| `AI-Commander-Brain-Memory-` | Model-independent memory, artifact ingestion, provenance, local recall, optional policy-approved Atlas sync. |
| `Aicc-ModelIntelligence` | Model discovery/benchmark/rollout recommendations; never overrides live host admission. |
| `job-agent` | Job discovery and Playwright/ATS application workflow; owns submission receipts and ambiguity reconciliation. |
| `email-agent` | Inbox triage/extraction/schedules; consequential mail actions remain gated and reconciled. |
| `icloud-mail-mcp` | Low-level bounded IMAP/SMTP MCP tools; does not own higher-level email intelligence. |
| `Web-Intelligence-Agent` | Source-backed browser/research evidence; does not become a general privileged browser. |
| `Code-Review-Agent` | AI-assisted PR review; findings remain hypotheses until verified against the current head. |
| `aicc-metrics-agent` | Grafana Alloy metrics collection/remote write; observability evidence, not workflow authority. |
| `Consys-workspace-agent` | Consys/M365 business context and workflow planning; shared policy/execution owners perform mutations. |
| `aicc-secrets` | Encrypted secret/deployment authority; may store local-only values and distributes to Render only the explicitly mapped values. |

### Shared infrastructure and tools

- **Secrets:** `~/Dev/Projects/aicc-secrets` is the encrypted local authority. `secrets.enc.env` is SOPS+age encrypted; age private keys and plaintext output stay outside Git. Authorized local processes decrypt into their own process environment at startup, including `AI-OpenRouter/boot.sh`, the `ai-commander-service` systemd unit, and Job Agent scheduled units. Never source decrypted values into logs, prompts, tests, screenshots, memory, issues, or PR text.
- **Variable ownership:** use service-scoped store keys and least-privilege credentials, but verify the consumer's runtime name. For Job Agent's Render service, the store key `MONGODB_URI_JOB_AGENT_DASHBOARD` is mapped to the runtime variable `MONGODB_URI`; source and `render.yaml` read `MONGODB_URI`. Do not copy another repository's variable name or credential.
- **Render:** `aicc-secrets/render-sync-map.json` is the deployment allowlist; `aicc-secrets/scripts/sync-to-render.sh` applies only mapped values and may rename a store key to the consumer's runtime variable. An encrypted value existing in the store does not authorize distribution. Render-hosted services use their own `render.yaml`/Docker/runtime contracts.
- **Private networking:** Tailscale is currently used for Job Agent's fail-closed noVNC remote re-auth path and supported tunneled Ollama access in Code Review Agent. The broader deny-by-default Coordinator/worker Tailscale mesh is planned work until its grants, identity binding, and acceptance evidence land; do not assume tailnet reachability equals authorization.
- **Local models and repair:** Ollama is the local runtime. Desktop routing/admission owns live resource checks. OpenHands runs as an isolated, bounded coding escalation through Desktop/Repair; it receives scoped workspaces and no general host/GitHub/secret access.
- **Data stores:** each service owns its schema and persistence contract. Desktop/Job/Brain local durability commonly uses SQLite; Coordinator uses PostgreSQL; OpenRouter, Model Intelligence, and optional Brain shared retrieval use service-scoped MongoDB databases/credentials. Do not create cross-service table/collection coupling.
- **Observability:** services expose bounded metadata; `aicc-metrics-agent`/Grafana Alloy handles shared scraping and remote write. Prompts, message bodies, credentials, personal content, raw URLs, and unbounded identifiers must not become metrics/log labels.
- **Automation and integration:** GitHub is the code/PR evidence surface; Jira `ACES` is the active implementation-work authority; MCP supplies bounded tool contracts. Browser automation uses Playwright/Chrome only where the owning agent defines it. systemd/launchd and container schedules must preserve single ownership and restart-safe side effects.

### Cross-repository change rules

1. Verify the current contract in the owning repository before changing a consumer.
2. Update producer, consumer, deployment mapping, and documentation together when a shared contract changes.
3. Preserve local-first behavior: cloud, Render, MongoDB Atlas, and Tailscale outages must degrade explicitly without inventing success.
4. Treat network reachability, a stored secret, or a model response as capability inputs—not authorization or completion proof.
5. Record follow-up work in the owning repository/Jira component instead of duplicating the capability locally.
