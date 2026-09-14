---
name: aicc-root-cause-fix
description: Use for bugs, regressions, flaky failures, stabilization, or unexpected AI Commander behavior.
---
# Root-Cause Fix
1. Reproduce the failure with the smallest reliable test/fixture/trace.
2. Trace current behavior before editing.
3. Fix the component that owns the failure.
4. Add/strengthen a regression test when practical.
5. Check retries, timeouts, cleanup, concurrency, idempotency, restart behavior, and ambiguous side effects where relevant.
6. Run targeted tests, then the relevant broader suite.
7. Report root cause, fix, evidence, and remaining risk.

Do not weaken validation, policy, authorization, or tests to hide a failure.