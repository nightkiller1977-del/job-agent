---
name: aicc-security-review
description: Use for security review or changes involving authorization, permissions, secrets, trust boundaries, external input, sensitive data, or privileged actions.
---
# Security Review
1. Identify identities, inputs, credentials, sensitive data, side effects, and trust boundaries.
2. Verify authn/authz independently; use least privilege and fail closed.
3. Validate untrusted input before files, shells, URLs, DBs, prompts, or privileged tools.
4. Keep secrets/private data out of source, logs, metrics, prompts, fixtures, and PR text.
5. Preserve approval, budget, audit, and policy controls.
6. Check replay, spoofing, injection, path traversal, unsafe retry, and privilege escalation when applicable.
7. Add negative/adversarial tests.
8. Prefer existing security primitives over bespoke mechanisms.