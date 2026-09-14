---
name: aicc-code-review
description: Use to review code or a PR and assess whether AI Commander changes are ready to merge.
---
# Code Review
Review in this order: correctness/security, reuse, simplicity, validation evidence.
- Read surrounding code/contracts, not only the diff.
- Check errors, failure semantics, side effects, concurrency, retries, cleanup, and restart safety where relevant.
- Identify existing code/patterns that should be reused.
- Challenge unnecessary abstraction/dependencies.
- Verify positive and negative tests.
- Separate blockers from optional cleanup and recommend the smallest safe correction.