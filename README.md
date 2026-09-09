# Job Agent

Job Agent is AI Commander's **job-discovery, qualification, resume-tailoring, review, and application-automation pipeline**. It brings job opportunities from multiple sources into one normalized workflow, scores them against the candidate profile, prepares job-specific application materials, places work into a review queue, and can automate supported application flows through browser/ATS adapters.

## Current status — September 9, 2026

**State: Active application-automation service / substantial implementation with ongoing ATS and reliability hardening.**

The old README's simpler “Claude scores jobs and Playwright submits them” framing is no longer complete. `main` now includes:

- Multi-source job discovery/ingestion, including LinkedIn-, Jobright-, Indeed-, USAJobs-, and ATS-oriented paths where supported.
- Job normalization, duplicate prevention, and durable submission/application ledger behavior.
- AI-assisted qualification/scoring using a model-client abstraction rather than a Claude-only implementation.
- Local-model support with Ollama model selection constrained by available host memory/capacity.
- Job-specific resume tailoring.
- A resume-quality score gate: tailored output must reach the configured quality threshold (currently 90 in the implemented workflow) before it is accepted for the application path.
- Structured model-output parsing/repair for common JSON formatting/control-character failures.
- Human review/approval queue before consequential submission workflows where configured.
- Browser automation and ATS adapter infrastructure for supported application flows.
- Session/profile locking, authentication-health checks, CAPTCHA/reauth handling, and recovery behavior.
- SubmissionLedger/idempotency protections intended to prevent accidental duplicate applications.
- Runtime incident reporting integration with `Aicc-Coordinator` for trusted operational failures.
- Security and secret-management documentation (`SECURITY.md`, `SECRETS.md`).

The service is **not correctly described as “fully automatic for every job site.”** Application capability depends on the source/ATS, authentication state, anti-bot controls, page changes, required questions, available candidate data, and policy/review gates. When evidence is insufficient or a site requires user intervention, the pipeline should fail safely or return the item for review rather than invent answers or bypass site controls.

## Pipeline

```text
Job sources / ATS discovery
          │
          ▼
 normalize + deduplicate
          │
          ▼
 qualification / scoring
          │
          ▼
 resume tailoring + quality gate
          │
          ▼
 review / approval queue
          │
          ▼
 supported ATS/browser adapter
          │
          ▼
 submission evidence + SubmissionLedger
          │
          └────► runtime incident to Coordinator when needed
```

## Design priorities

### Accuracy before application volume

The agent should not optimize for the largest possible number of applications. It should prioritize jobs that actually match the profile and create materials grounded in the candidate's real experience.

### Resume tailoring must remain truthful

Tailoring can change emphasis, ordering, terminology, and phrasing to improve alignment with a job description. It must not fabricate employers, titles, dates, education, certifications, clearances, accomplishments, or skills that are not supported by the source profile/resume.

### Review and evidence

Consequential automation should preserve a review path and durable evidence of what was attempted/submitted. A browser click is not sufficient evidence that an application was accepted.

### Idempotency

SubmissionLedger and related locking exist so retries, agent restarts, or duplicate discoveries do not silently create repeated applications.

### Cost-aware/local-first inference

The model layer can use local Ollama models when suitable, and local model choice is bounded by real host capacity. External model use should remain controlled by the broader AI Commander cost/secret policy rather than hard-coded per workflow.

## ATS automation

`ATS_ADAPTER_PLAN.md` documents the adapter strategy and remaining expansion work. Treat that file as planning/reference material: implemented adapters and tests on `main` determine what is actually supported today.

Browser/ATS automation must not attempt to defeat CAPTCHAs, access controls, or site protections. When reauthentication or human verification is required, the workflow should surface that condition for user action.

## Local development

Review `.env.example`, `config.example.json`, `DEVELOPER_ONBOARDING.md`, and the current project dependency files for setup.

Run the repository's defined tests before changing application/submission logic. Keep real credentials, browser session secrets, API keys, and personal application data out of Git; use the documented secret-management path.

## Related repositories

| Repository | Relationship |
|---|---|
| `Ai-Command-Center-Desktop-App` | Operator-facing orchestration, review visibility and broader AI/model policy |
| `Aicc-Coordinator` | Trusted operational incident intake and cross-service repair coordination |
| `email-agent` | Job-related inbox signals and email triage workflows |
| `aicc-secrets` | Shared encrypted credential/configuration authority where integrated |

## Documentation rule

This README describes capabilities that exist on `main` as of the date above. Job-source availability, ATS compatibility, automated tests, application evidence, runtime incidents, GitHub merges, and Jira status are the source of truth for what is operational. Do not describe planned ATS coverage as implemented, and do not treat a generated application attempt as a confirmed submission without evidence.
