# Job Agent Autonomy Design

**Date:** 2026-09-13  
**Repository:** `nightkiller1977-del/job-agent`  
**Goal:** Make Job Agent reliably autonomous for discovery, scoring, tailoring, approved-job submission, session self-healing, bounded recovery, and repair proposal generation without allowing ambiguous submissions or autonomous code merges.

## 1. Scope and operating policy

Job Agent is considered autonomous when routine scheduled operation no longer requires a human to start, resume, or repair ordinary runs. Human intervention remains required only for:

- CAPTCHA or MFA/2FA that cannot be completed with an approved stored mechanism;
- genuinely ambiguous submission outcomes that enter reconciliation hold;
- policy-required human review or missing answers that cannot be derived safely;
- approval/merge of code-repair PRs.

The agent may autonomously:

- discover jobs;
- score and rank jobs using existing policy thresholds;
- tailor resumes/answers within existing profile/policy constraints;
- submit jobs already eligible under existing auto-submit policy;
- refresh sessions when an approved automated path and credentials exist;
- recover from ordinary browser/ATS failures when duplicate-submission safety is preserved;
- quarantine uncertain or repeatedly failing jobs;
- diagnose unknown failures, generate tests/fixes, and open repair PRs.

The agent must never:

- classify a click alone as a successful application;
- retry an unresolved possible submission;
- bypass CAPTCHA/MFA or security controls;
- run an unattended apply from an unapproved/dev branch;
- self-merge a repair PR;
- silently switch to a paid model/provider when zero/low-cost policy disallows it.

## 2. Current baseline

PR #121 is merged to `main` and establishes submission-truth invariants:

- attempt-scoped receipt freshness;
- Python-owned receipt baselines;
- `absent` / `dispatched` / `uncertain` submit outcomes;
- reconciliation holds for possible submissions;
- duplicate-submit fencing across recovery and restart;
- full repository regression coverage for receipt and dispatch truthfulness.

PR #123 is test-only follow-up coverage for same-text occurrence freshness and is not on the production-autonomy critical path.

PR #120 is an **input for selective porting only**, not a merge candidate for autonomy. Its branch is based on pre-#121 `main`, is currently non-mergeable against the new baseline, and mixes session work with unrelated blocker-intelligence/adaptive-retry changes. Session Autonomy must start from current `main` and port only behavior explicitly required by PR A below. `blocker_intelligence.py`, model-backed blocker classification, and adaptive retry-cap work from #120 are excluded from PR A.

The remaining production autonomy gaps are concentrated in session recovery, scheduler durability, run-state recovery, and repair/model routing.

## 3. Architecture boundaries

Implementation is split into three independently reviewable PRs. Each PR must start from current `main`, use TDD, run the full suite, and remain narrow enough that a reviewer can reject it without blocking the next design phase.

### PR A — Session Autonomy

**Purpose:** Let scheduled runs self-heal expired/missing sessions where an approved automated path exists, while persisting notification suppression across processes and preserving human escalation for sources that still require intervention.

**Primary files:**

- `src/session_watchdog.py`
- `src/orchestrator.py`
- `src/notifier.py`
- `src/sources/linkedin.py` where session persistence requires it
- existing reauth/state helpers needed to clear/reload source-auth state
- focused tests under `tests/`

**Required behavior:**

1. LinkedIn health checks use only authentication-relevant cookies (`li_at`, and any explicitly validated auth-cookie alternatives already supported by the codebase). Short-lived analytics/tracking cookies must not mark the session expired.
2. Background preflight performs at most one automated reauth attempt per source per run when:
   - the source supports approved automated reauth;
   - required credentials are available;
   - the source is expired/missing or has a source-auth blocker.
3. Successful reauth must clear only qualifying source-login blockers, persist the refreshed session, reload/re-read affected jobs from durable state, and reclassify them in the same run.
4. External-portal login requirements (for example employer Workday portals) must not be incorrectly cleared by source-level LinkedIn/Indeed/Jobright reauth.
5. Deep-link/human notifications are emitted by one path only. A failed automated reauth may fall through to one human notification, never two.
6. Warning dedupe must persist across scheduled Python processes. A stable dedupe key and timestamp are stored in the existing durable agent-status/state area; process-local dictionaries may remain only as an optimization.
7. Session refresh/save occurs immediately after authentication is known good so later failures do not discard rotated cookies/session state.
8. PR A must not introduce model-backed blocker classification, adaptive retry policy, or unrelated scoring/model behavior from PR #120.

**Acceptance tests:**

- valid `li_at` + expired `lidc`/`UserMatchHistory` remains healthy;
- no valid auth cookie produces the correct non-healthy result;
- expired source + valid credentials + successful reauth moves a blocked job to `ready` in the same apply run;
- stale `extra_json` cannot keep a successfully reauthed job blocked;
- unrelated employer-portal blocks survive source reauth;
- failed reauth emits one escalation only;
- notification dedupe survives a fresh Python process;
- scheduled process restart still honors the persisted dedupe timestamp.

### PR B — Run Autonomy

**Purpose:** Make every scheduled run resumable, bounded, and non-destructive so one bad job or process restart cannot cause duplicate actions, infinite retries, or loss of work.

**Primary responsibilities:**

- scheduler/run lease or lock reuse/extension;
- durable run/job state transitions;
- quarantine and retry policy;
- batch continuation after per-job failure;
- explicit run outcome summaries.

**Required behavior:**

1. Overlapping scheduled apply runs are fenced by a cross-process lease/lock. A second run exits safely before opening submission flows.
2. Every job entering the apply pipeline ends the cycle in one explicit state:
   - `applied_verified`;
   - `submission_unverified` / reconciliation hold;
   - `needs_human` / session/MFA/CAPTCHA/policy pause;
   - `retryable_pre_submit_failure`;
   - `quarantined` after bounded retries;
   - `skipped_by_policy` / already applied.
3. A single job exception must be caught at the job boundary, recorded, and the batch must continue unless the failure indicates a global unsafe condition.
4. Retry counters and blocker classes are durable. Retry caps may be lowered by proven persisted history but never raised beyond the static safety ceiling without an explicit future design. PR B should prefer existing static blocker classification and existing retry interfaces; model-backed blocker classification is not required for autonomy.
5. Ambiguous submit outcomes never re-enter ordinary retry/recovery; they remain held until reconciliation.
6. Restart recovery loads durable state and resumes only jobs eligible for another attempt. Jobs already verified, held, quarantined, or policy-blocked do not re-dispatch.
7. Scheduled launcher branch pinning remains fail-closed.
8. Run summaries distinguish verified submissions from attempted/unverified/blocked work. “No crash” must never be reported as “applications submitted.”

**Acceptance tests:**

- two concurrent apply processes -> exactly one reaches browser/apply execution;
- one synthetic job crash does not prevent later jobs from running;
- process restart preserves reconciliation hold and quarantine state;
- a held possible-submit job receives zero additional submit clicks after restart;
- retryable failures stop at the configured cap and enter quarantine;
- successful verified job is not repeated by a fresh process;
- scheduler branch drift exits before discover/apply execution;
- run summary counts verified, held, human-blocked, retryable, and quarantined separately.

### PR C — Repair and Model Autonomy

**Purpose:** Allow Job Agent to diagnose and propose fixes for unknown failures without giving the repair loop authority to perform unsafe submissions, spend unapproved money, or merge its own code.

**Required behavior:**

1. Unknown/repeated ATS/browser failures generate structured failure evidence containing:
   - source/vendor;
   - stage/phase;
   - normalized status/reason;
   - sanitized DOM/browser evidence where policy allows;
   - attempt count and prior outcomes;
   - no secrets or raw credentials.
2. Repair orchestration may:
   - reproduce the failure with synthetic/offline fixtures where possible;
   - write a failing regression test;
   - implement a bounded fix;
   - run focused and full tests;
   - open a draft PR with evidence.
3. Repair orchestration may not:
   - submit another employer application to prove the fix;
   - clear reconciliation holds;
   - merge its own PR;
   - modify secrets or credential material outside existing approved secret-store flows.
4. Model selection is capacity-aware:
   - prefer feasible local models and approved zero-cost routes;
   - never repeatedly load a model that exceeds current worker capacity;
   - when no safe model is feasible, return the exact status `MODEL_CAPACITY_UNAVAILABLE` and defer repair rather than crash the run;
   - model failure must not change submission ledger state.
5. Expensive/paid remote fallback remains disabled unless explicitly permitted by configuration/policy.

**Acceptance tests:**

- unknown synthetic ATS failure produces a repair evidence bundle and draft PR without touching live employers;
- repair PR cannot self-merge through the Job Agent path;
- insufficient local capacity selects a smaller feasible model or returns `MODEL_CAPACITY_UNAVAILABLE` cleanly;
- model/provider outage does not change job submission/reconciliation state;
- repair failure leaves original job quarantined/paused, not blindly retried.

## 4. End-to-end autonomous acceptance campaign

After PRs A-C are merged, autonomy is accepted only after repeated scheduled execution proves the integrated behavior.

Minimum acceptance bar:

- at least 3 consecutive scheduled discover/apply cycles;
- no manual start/resume between cycles;
- zero duplicate submits;
- zero false `applied` classifications;
- zero batch-wide crashes caused by a single job;
- all attempted jobs terminate in an explicit durable state;
- source-session expiry self-heals where credentials/policy allow;
- CAPTCHA/MFA and genuinely ambiguous submissions pause and notify rather than thrash;
- restarts preserve holds, quarantine, and already-applied state;
- no unapproved paid-model spend;
- no repair PR self-merges.

The acceptance campaign may use real jobs only when they are jobs the user genuinely wants to apply to. Synthetic fixtures remain the default for failure injection, duplicate-submit testing, and repair validation.

## 5. Error-handling policy

Errors are classified by scope:

- **Job-local:** record outcome, apply retry/quarantine policy, continue batch.
- **Source-session:** attempt approved self-heal once; otherwise pause affected source jobs and continue others.
- **Submission-ambiguous:** durable reconciliation hold; never auto-retry.
- **Global safety:** branch drift, corrupt/unreadable submission ledger, broken cross-process lease, or equivalent state-integrity failure causes fail-closed run termination before further submissions.
- **Model/repair:** defer repair and continue safe job processing where possible; never mutate submission truth based on model availability.

## 6. Observability and notifications

Autonomous operation must expose enough evidence to answer “what happened?” without reading raw logs.

Per run, persist/report:

- run id and scheduler source;
- discovered/scored/approved counts;
- verified applied count;
- reconciliation-held count;
- human/session-blocked count;
- retryable failure count;
- quarantined count;
- reauth attempts and outcomes;
- repair PRs opened;
- model-capacity deferrals;
- run-level fatal reason, if any.

Notifications should be actionable and durable-deduped. Repeated unchanged warnings should not alert every scheduled process.

## 7. Security and cost constraints

Priority order remains:

1. security / submission correctness;
2. code reuse and existing interfaces;
3. simplicity / minimal new infrastructure;
4. low or zero recurring cost.

No new cloud service is required merely to implement autonomy. Existing durable state/ledger/status stores should be reused unless a concrete test demonstrates they cannot provide the required cross-process guarantees.

Secrets remain in the existing secret-store flow and must never be written to logs, PR bodies, repair evidence, or model prompts.

## 8. PR and review discipline

- PR #123 stays independent because it is test-only coverage.
- PR A, PR B, and PR C each start from the then-current `main`.
- Each PR uses RED -> GREEN TDD commits and runs focused tests plus the full suite.
- A PR stops growing once its stated acceptance criteria are satisfied.
- New non-blocking findings become a follow-up PR.
- Only a newly proven P0/P1 defect in that PR’s core safety contract may expand its scope before merge.

## 9. Definition of done

Job Agent is “autonomous” only when the integrated system can run on schedule, recover ordinary sessions and browser failures, continue across job-local errors, safely pause ambiguous or human-required cases, survive restart without duplicate work, and propose repair PRs without self-merging or spending outside policy.

Passing unit tests alone is necessary but not sufficient; the three-cycle autonomous acceptance campaign is the final gate.