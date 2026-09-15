---
name: aicc-pr-completion
description: Use when preparing a PR, finishing implementation, checking merge readiness, or producing completion evidence.
---
# PR Completion

1. Re-read the Jira work ticket and its parent; confirm the implementation follows parent direction and requested scope.
2. Verify the work ticket has a parent and the PR identifies both ticket and parent.
3. Ensure the diff contains no unrelated work; discovered extra work must have a separate Jira child ticket.
4. Confirm existing patterns are followed and capability is not duplicated.
5. Run targeted tests plus the relevant broader validation suite.
6. Verify negative paths, security boundaries, and restart/idempotency concerns that apply.
7. Do not bypass tests, hooks, approvals, or security controls.
8. Update documentation for changed contracts, architecture, configuration, or operations.
9. Update Jira with the PR URL, meaningful findings/blockers, and validation evidence; set status to `PR Open` / `In Verification` as appropriate.
10. Summarize the problem, approach, changed areas, validation, and remaining risk.
11. Do not call the PR merge-ready while required validation is missing/failing, Jira is stale, or ticket/parent linkage is missing.
12. Do not mark Jira `Done` until the work ticket and parent completion criteria and required delivery evidence are satisfied.