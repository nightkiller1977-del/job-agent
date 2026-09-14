# job-agent

`job-agent` is AI Commander's **job discovery, scoring, resume-tailoring, ATS automation, and application execution agent**. It discovers opportunities, evaluates them against configured role/compensation criteria, prepares tailored application material, drives supported application flows with Playwright, and records evidence about what actually happened.

For deeper engineering internals and apply-workflow architecture, see [`DEVELOPER_ONBOARDING.md`](DEVELOPER_ONBOARDING.md).

## Current state

**State: Functional autonomous pipeline under submission-truthfulness and reliability hardening.**

Capabilities on `main` include:

- Multi-source job discovery and normalization.
- Job scoring/ranking against configured role, seniority, compensation, and work-style criteria.
- Resume tailoring with fabrication guards and score/review gates.
- Persistent job/application state.
- ATS routing and adapter-based application flows.
- LinkedIn and external ATS application handling.
- Browser/session isolation using persistent per-source profiles.
- Automated session recovery for scheduled runs, with bounded human-assist paths where a source requires interactive authentication/2FA.
- Dashboard and scheduled/background execution support.
- Runtime incident reporting into the wider AI Commander recovery path.
- Receipt/submission verification that distinguishes confirmed evidence from ambiguous outcomes instead of claiming success from stale DOM text or timing alone.
- Regression coverage around receipt freshness, restart safety, delayed success signals, and adapter behavior.
- Shared OpenHands and Claude repository skills so development agents follow the same reuse, security, simplicity, and evidence rules.

The agent should **not** be described as “fully reliable” merely because it can reach and fill an application. Browser automation has ambiguous failure modes: a submit click can time out after the employer accepted it, a success message can be stale, an authentication state can expire mid-run, and an ATS can change its markup. The system therefore treats uncertain submission state as uncertain and reconciles before retrying.

## Direction

The priority is to make the agent **truthful and restart-safe before increasing application volume or adding more sources**.

1. **Eliminate false submission claims.** `applied`/confirmed states require fresh evidence tied to the current submission attempt. Stale/same-text UI, pre-submit DOM, or a click alone are not receipts.
2. **Prevent duplicate employer submissions.** When a submission outcome is ambiguous, reconcile the ATS/job state before retrying. Timeout must never automatically mean “safe to submit again.”
3. **Keep `submission_unverified` as a real state.** Uncertainty is preferable to a false positive. Follow-up logic should investigate/reconcile it rather than silently convert it to success or failure.
4. **Prefer deterministic ATS adapters.** Vendor-specific APIs/selectors and structured adapters should be used before generic model-driven browser guessing when a reliable deterministic path exists.
5. **Keep application side effects idempotent/restart-safe.** Persistent state, adapter event logs, attempt identity, and receipt evidence should allow a crashed/restarted run to understand what already happened.
6. **Make session recovery autonomous but bounded.** Recover normal expired sessions automatically; fail closed or request human assistance for authentication states that cannot be safely automated.
7. **Reduce unnecessary LLM orchestration.** Discovery normalization, session checks, adapter selection, form validation, receipt verification, retries, and reconciliation should remain deterministic where possible. Use models for scoring/tailoring/reasoning where they add value.
8. **Expand adapter coverage based on observed failure classes.** Workday, Greenhouse, Lever, Ashby, BrassRing, LinkedIn and fallback flows should converge on shared contracts rather than one-off fixes.
9. **Feed operational failures into AI Commander.** Structured incidents should include enough evidence for Guardian/Repair/OpenHands to distinguish source changes, authentication failures, browser/runtime faults, and code defects.
10. **Test consequential behavior only in controlled fixtures/test tenants.** Do not use real employer submissions as regression-test side effects.

## Execution model

```text
Discover
   ↓
Normalize / dedupe
   ↓
Score / filter
   ↓
Tailor resume if required
   ↓
Preflight
   ↓
Choose deterministic ATS/apply adapter
   ↓
Validate session + form
   ↓
Submit (when authorized)
   ↓
Verify fresh receipt/evidence
   ├─ confirmed ─────────────► record applied
   ├─ confirmed not sent ───► bounded recovery/retry
   └─ ambiguous ────────────► submission_unverified + reconcile
```

## Safety and truthfulness rules

- A click is not proof of submission.
- Existing success text is not proof of a new submission.
- A timeout is not proof of failure.
- Ambiguous side effects must be reconciled before retry.
- Resume tailoring may change emphasis/wording but must not invent employers, roles, dates, education, certifications, or skills.
- Real credentials and personal profile data remain outside Git.
- Tests must not create live employer applications.
- Model output is advisory; deterministic browser/ATS evidence establishes application state.

## Configuration

Copy the example configuration files and keep real values untracked:

```bash
cp .env.example .env
cp config.example.json config.json
```

`config.json`, `state/profile.json`, sessions, tailored resumes, credentials, and other user-specific state should remain gitignored/private. Use the shared `aicc-secrets` flow where the wider AI Commander deployment manages credentials centrally.

## Common commands

```bash
# Create environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome

# Discover/score jobs
python src/main.py discover

# Check approved queue and environment
python src/main.py preflight

# Controlled apply testing
python src/main.py apply --limit 1 --no-auto-submit

# Authorized automatic application run
python src/main.py apply --auto-submit

# Prepare/recover sessions where needed
python src/main.py prepare-sessions

# Operational status/checks
python src/main.py status
python src/main.py ops-check
```

Use `src/main.py --help`, current config examples, and `DEVELOPER_ONBOARDING.md` as the source of truth for the full command surface.

## Testing

Run the relevant pytest suites before merging application-path changes. Receipt verification, adapter selection, restart safety, session recovery, and submission reconciliation are especially load-bearing because they protect real-world side effects.

The project deliberately keeps RED-then-GREEN regression history where useful so a test demonstrates the bug it is intended to prevent. Do not bypass pre-push hooks or weaken truthfulness assertions to make a change mergeable.

## Relationship to AI Commander

- `Ai-Command-Center-Desktop-App` schedules/observes the agent and owns the broader stability/Guardian/Repair path.
- `Aicc-Coordinator` receives trusted operational incidents and coordinates durable recovery work.
- `AI-Commander-Brain-Memory-` is the direction for reusable lessons, incident history, job-search preferences, and model-independent personal context under policy.
- `email-agent` can provide job-related inbox signals without becoming the application authority.
- `aicc-secrets` provides shared managed credentials where configured.

## Documentation rule

This README describes the role, capabilities merged to `main`, and current direction. A supported ATS is not guaranteed to remain unchanged, and a historical successful run is not proof that the next live submission will succeed. Tests, receipts, ledger/state data, runtime evidence, GitHub changes, and controlled acceptance runs remain the source of truth. Avoid dated status snapshots and fixed “fully automated / bug-free” claims.