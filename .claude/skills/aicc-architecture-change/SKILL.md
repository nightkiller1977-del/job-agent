---
name: aicc-architecture-change
description: Use for cross-service, cross-agent, routing, contract, storage, or other architectural changes in AI Commander.
---
# Architecture Change
1. Start from the existing architecture and exact seam that must change.
2. Extend an existing contract/service before introducing a parallel subsystem.
3. Minimize new coordination, persistence, network, and operational dependencies.
4. Define ownership, trust boundaries, failure/retry/idempotency, and compatibility first.
5. Preserve security/policy/local-first boundaries where applicable.
6. Add abstraction only for a required boundary or real duplication.
7. Document migration and rollback implications for cross-component changes.