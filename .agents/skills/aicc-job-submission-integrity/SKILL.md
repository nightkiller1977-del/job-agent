---
name: aicc-job-submission-integrity
description: Use when changing job discovery, ATS adapters, browser sessions, application submission, receipt verification, retry/recovery, or employer-facing automation.
---
# Job Submission Integrity
1. Keep scraper/browser profiles isolated; never use the user's live main Chrome profile.
2. Treat submission as a non-idempotent external side effect unless the ATS provides a verified idempotency mechanism.
3. Record intent/attempt state before the side effect when supported, then reconcile the actual employer/ATS state after timeout or restart.
4. Never retry an ambiguous submission until reconciliation rules say it is safe.
5. Require durable success evidence before marking `submitted`; UI clicks, model text, or stale DOM are insufficient.
6. Keep `submission_unverified` distinct from success and failure.
7. Add regression tests for delayed success, stale DOM, duplicate-risk restart, and false receipt patterns when touching verification.
8. Do not perform live employer submissions merely to collect test evidence unless explicitly authorized.