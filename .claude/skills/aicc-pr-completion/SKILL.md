---
name: aicc-pr-completion
description: Use when preparing a PR, finishing implementation, checking merge readiness, or producing completion evidence.
---
# PR Completion
1. Re-check scope; remove unrelated changes.
2. Confirm existing patterns are followed and capability is not duplicated.
3. Run targeted tests and relevant broader validation.
4. Verify negative paths, security boundaries, restart/idempotency concerns.
5. Never bypass tests, hooks, approvals, or security controls.
6. Update docs for changed contracts/config/operations.
7. Summarize problem, approach, validation, and remaining risk.
8. Do not call it merge-ready while required validation is missing/failing.