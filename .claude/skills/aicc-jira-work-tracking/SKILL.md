---
name: aicc-jira-work-tracking
description: Use whenever starting, updating, reviewing, completing, or discovering implementation work that must be tracked in Jira.
---
# AI Commander Jira Work Tracking

## Instance
- Site: `https://shoalinwu.atlassian.net`
- Active project: `ACES`
- Browse pattern: `https://shoalinwu.atlassian.net/browse/<KEY>`
- `KAN` is legacy/historical unless explicitly requested.
- Component/repository routing is Coordinator-authoritative. Consult current Aicc-Coordinator registration/config and do not invent mappings.
- Never put Jira email, API token, Cloud ID, enrollment secret, or any credential in repo guidance, prompts, logs, tests, or PR text.

Current documented ACES mappings:
- AI Command Center → `nightkiller1977-del/Ai-Command-Center-Desktop-App`
- AICC Coordinator → `nightkiller1977-del/Aicc-Coordinator`
- Job Agent → `nightkiller1977-del/job-agent`
- Email Agent → `nightkiller1977-del/email-agent`
- ConnectionSphere → `nightkiller1977-del/connectionsphere`
- TrustGraph → `nightkiller1977-del/TrustGraph`
- Routing → `nightkiller1977-del/Aicc-Coordinator`
- Code Review Agent → `nightkiller1977-del/Code-Review-Agent`
- AI OpenRouter → `nightkiller1977-del/AI-OpenRouter`

If a repository/component is absent, do not guess; use the Coordinator's current routing data.

## Before coding
1. Identify the Jira work ticket.
2. Verify it has a parent Epic/roadmap ticket. If not, identify/create the parent first.
3. Read parent and child; parent outcome/direction/constraints govern the child.
4. Confirm repository/component scope, dependencies, acceptance criteria, and authorization.
5. Move/update the work ticket to the appropriate working state before implementation.

## During work
- Keep changes tied to the ticket and parent.
- Record meaningful findings, blockers, changed decisions, and validation evidence in Jira.
- Create a separate linked child ticket for discovered out-of-scope work.
- Reconcile Jira before changing direction when code reality conflicts with the ticket.

## PR and completion
- PR must name work ticket and parent ticket.
- Jira ticket must reference the PR.
- Move/update status to `PR Open` when the PR is opened.
- Record validation evidence and move through `In Verification`.
- Failed validation → `Verification Failed`; resumed work → `AI Commander Working`.
- Do not mark `Done` until required tests/delivery/reconciliation evidence and parent/ticket completion criteria are satisfied.