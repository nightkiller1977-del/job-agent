# Job Agent E2E Reliability Design

**Work item:** ACES-387

**Parent:** ACES-18 — Job Agent Roadmap

**Status:** Approved for design; implementation plan pending review

## Goal

Make every operational failure in the Job Agent either self-healing when safe, clearly actionable when human or infrastructure intervention is required, or durably classified without risking duplicate employer submissions.

## Evidence and scope

The live state and run logs show these failure classes:

1. Dashboard pull, status push, and discovery sync can fail before an HTTP response, but the current broad exception handlers retain only `str(exc)`. Empty-string exceptions, including common HTTP client timeouts, become blank dashboard alerts.
2. Scheduled commands correctly fail closed when the checkout drifts from its pinned branch, but neither the scheduler status nor alerts summarize this condition with its remediation. The current runtime is pinned to `main` and on `main`; historical failures occurred during development-branch checkouts.
3. macOS-only `osascript` notification attempts run on Linux and generate noisy secondary errors. They must not conceal source-session or run failures.
4. Session recovery reports a mix of invalid login, human 2FA, CAPTCHA, email polling timeout, and notification transport failure. The agent must not misrepresent those as interchangeable reauth failures.
5. Model scoring has local request timeouts and DNS failures at the remote gateway. A provider failure must be attributed to its provider and failure kind, not reported as a generic cascade failure only.
6. Application flows encounter genuine CAPTCHA, invalid/unresolvable ATS URLs, missing submit controls, and ambiguous post-click outcomes. A click without durable receipt evidence must remain `submission_unverified`; it is never retried automatically.
7. Existing ACES-284 owns circuit-breaker decay/re-arm policy. This work may consume its classification contract but must not independently change breaker eligibility or retry policy.

Stale alerts for missing `config.json` and `state/profile.json` are excluded from the active problem: both files exist now. The design still adds current preflight reporting so stale and current status are distinguishable.

## Design

### 1. Structured operational-failure contract

Add a small, local helper for safe error descriptions used at network and process boundaries. It will emit:

- operation name (for example, `cloud_pull_approved`);
- endpoint class, never an endpoint URL or header value;
- exception type and a bounded normalized message;
- classification (`timeout`, `dns`, `connect`, `tls`, `http_status`, `configuration`, or `unknown`);
- transport retryability (diagnostic only, never retry authorization);
- status code only when a response exists.

Operation names and endpoint classes come from module-owned allowlisted enums rather than request URLs. The helper must redact nested-cause text, URLs, query strings, authorization headers, sync secrets, credentials, and response bodies. Its output always contains a bounded nonblank message, including for `TimeoutError("")`. It will be used by dashboard sync, provider preflight, and platform-notification paths instead of introducing separate logging systems.

The structured classification is durably written to the existing bounded run journal (`state/runs/<run-id>.jsonl`) as a new event payload. It is not written to jobs, ledger records, or dashboard payloads in the foundation PR, so their schemas remain compatible. Console output and notifications consume the same safe record but are not themselves the durable source. Persisted observability beyond run journals requires a separately scoped ticket and migration.

### 2. Cloud synchronization

Keep dashboard writes non-fatal, because discovery and the local SQLite journal are intentionally local-first. The cloud client will:

- distinguish missing configuration from transport failure and non-2xx response;
- log and notify a safe structured failure record;
- retry only operations named in an explicit retry-authorization allowlist, with a small bounded backoff and attempt limit;
- preserve the exact existing payload and sync-secret header contract;
- never retry `/api/action`, including after an ambiguous client timeout, because the server may have committed the state change.

The `/health` check remains unauthenticated and is only a diagnostic readiness probe; it is not a substitute for successful authenticated sync. Missing HTTP response never proves that a write was not processed. Retry authorization independently evaluates: operation allowlist, operation idempotency or server-side deduplication, submission state, configured attempt cap, and ACES-284's breaker decision. A `transport_retryable` diagnostic therefore never authorizes repeating a state-changing action.

### 3. Scheduler and host readiness

Extend the existing read-only operational status command to expose: observation timestamp, runtime branch vs pinned branch, virtual-environment executable presence, config/profile availability, timer/service last result, and source/provider readiness. The branch guard remains fail-closed. A run blocked by it receives a concise remediation that does not imply its application queue was processed.

Platform notification delivery becomes capability-gated: macOS `osascript` integrations execute only on macOS. On another platform, the system records a deduplicated `notification_unavailable` secondary condition while preserving the original session failure as primary.

### 4. Provider and session taxonomy

At the existing boundaries, map deterministic exceptions to a narrow taxonomy:

- model: local timeout, DNS, connect/TLS, unauthenticated, quota/budget, unavailable, malformed output;
- session: expired/invalid credentials, human 2FA required, CAPTCHA, email code timeout, browser failure, and notification unavailable.

The model cascade may fall through to another configured provider but records an ordered, bounded history of provider attempts and why each was unavailable. Session automation does not attempt to bypass CAPTCHA or 2FA and retains isolated `state/sessions/<name>_profile/` browser profiles.

Each boundary result carries one `primary_failure` and zero or more `secondary_conditions`. The primary failure is the operation outcome; secondary conditions describe failures while reporting or recovering from that outcome. For example, a CAPTCHA during session recovery remains primary while a Linux notification capability gap is secondary; an email-code timeout remains primary when notification delivery also fails; and a provider DNS failure stays in the ordered provider history even when a later cascade tier succeeds.

### 5. Submission and adapter failure truthfulness

Preserve the existing result and ledger ownership. Adapter fixes only improve classification and diagnostics:

- invalid URL, absent URL, DNS failure, browser-navigation failure, selector/navigation exception, and missing submit control must retain distinct classifications;
- absent submit controls remain `submit_not_found`;
- CAPTCHA remains a human-required blocker;
- an unresolved external URL is distinguished from a DNS/navigation failure;
- post-click timeout, stale DOM, browser crash, or missing receipt remains `submission_unverified` and is reconciled before any retry or restart-driven continuation.

Each individual adapter change is a separate child work item under ACES-18, because endpoints, controls, and vendor behavior differ. This protects the E2E foundation PR from becoming an unreviewable selector rewrite.

## Ownership and trust boundaries

| Boundary | Owner | Automatic behavior | Human/external responsibility |
| --- | --- | --- | --- |
| Local ↔ dashboard | Job Agent sync client | Safe diagnostics and bounded idempotent transport retry | Dashboard service availability and credentials |
| Scheduler ↔ repository | Scheduled launcher | Fail closed on branch drift and missing runtime | Restore intended branch or re-install timer after deliberate branch change |
| Browser ↔ ATS | Source adapter/session | Isolated profile, classify outcome, reconcile ambiguity | CAPTCHA, 2FA, and employer-controlled login |
| Scorer ↔ provider | Model client | Cascade and exact failure classification | Network/DNS, provider availability, valid credentials |

## Compatibility and rollback

- Existing sync payloads, headers, database fields, apply outcome tokens, and ledger records stay compatible.
- New metadata is additive and bounded. Existing console text remains readable, with structured context appended.
- Retry policy does not broaden apply/submission retries. Removing the new retry wrapper returns cloud behavior to the current single-attempt semantics.
- No database migration is required for the foundation work; any future persisted observability schema gets its own ticket and migration plan.

## Verification

Tests use a local fake dashboard and mocked transport exceptions, not production secrets or live dashboard mutation. Coverage must include empty-message timeout handling; nested-cause redaction; enum-only operation/endpoint identity; DNS/connect/TLS classification; exact retry count and bounded backoff; a server-committed/client-timeout `/api/action` attempted exactly once; Linux notification capability gating with preserved primary failure and deduplicated secondary condition; timestamped scheduler observations; ordered, bounded provider history; model/session taxonomy; ATS resolution error separation; and restart-safe no-retry behavior for unverified submissions.

The validation sequence is targeted unit tests, local integration tests, scheduler dry run/status, then the relevant broader suite. No live employer submission, CAPTCHA bypass, 2FA bypass, production database reset, or deletion of user state is authorized.

## Work decomposition

1. Foundation PR: safe diagnostics, cloud transport policy, scheduler/notification capability checks, and corresponding tests.
2. Provider/session PR: deterministic model/session taxonomy and preflight reporting.
3. Adapter PRs: one child ticket per vendor/source confirmed by audit or fixture evidence.
4. ACES-284 reconciliation: breaker decay/re-arm policy in its owning work item, consumed by the adapter work but not duplicated here.
