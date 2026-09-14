# Job Agent

Job Agent is a Python + Playwright automation project for managing the repetitive parts of a job search: discovering openings, ranking fit, tailoring application material, navigating supported application systems, and keeping a durable record of what actually happened.

The project is designed around one important rule: **an application is not considered submitted unless there is fresh evidence that the current submission attempt succeeded**. A button click, a timeout, or old success text is not enough.

## Why this project exists

Most job-search tools focus on only one part of the process — discovery, ranking, resume generation, or browser automation. Job Agent connects those steps into one workflow while trying to avoid one of the most dangerous automation failures: submitting the same application twice because a browser timeout was mistaken for a failed submission.

The project can be useful if you want to experiment with a self-hosted job-search workflow that combines:

- job discovery from multiple sources;
- configurable scoring and filtering;
- AI-assisted resume tailoring;
- Playwright-based browser automation;
- ATS-specific adapters where deterministic automation is possible;
- persistent application/session state;
- submission receipt verification and reconciliation.

## Project status

**Active development.** The end-to-end pipeline is functional, but job sites and applicant tracking systems change frequently. Reliability work is focused on session recovery, ATS compatibility, restart safety, receipt verification, and preventing duplicate submissions.

This project should not be treated as a guarantee that every supported site or employer flow will work unchanged over time.

## Highlights

- **Multi-source discovery** — supports job discovery and normalization from sources such as LinkedIn, Jobright, Indeed, USAJobs, and additional configured feeds.
- **Configurable fit scoring** — ranks roles against your target titles, seniority, compensation, location/work-style preferences, and other configured criteria.
- **Resume tailoring with fabrication guards** — can rewrite emphasis and wording for a role, but the workflow is designed not to invent employers, titles, dates, education, certifications, or skills.
- **ATS-aware automation** — vendor-specific adapters and selectors are preferred over generic browser guessing when a reliable deterministic path exists.
- **Persistent browser sessions** — source-specific browser profiles reduce repeated logins and support scheduled/background runs.
- **Session recovery** — normal expired sessions can be recovered automatically, with bounded human assistance for authentication flows such as 2FA.
- **Submission truthfulness** — confirmed submission state requires fresh evidence associated with the current attempt.
- **Ambiguous-outcome handling** — uncertain results remain `submission_unverified` until reconciled instead of being silently reported as success.
- **Restart-safe state** — application attempts, receipts, event history, and session state are persisted so a restart can reason about prior side effects.
- **Dashboard and notifications** — optional dashboard, notification, and approval integrations are available for remote monitoring and control.

## How it works

```text
Discover jobs
     ↓
Normalize + deduplicate
     ↓
Score and filter
     ↓
Tailor resume when needed
     ↓
Preflight application
     ↓
Select ATS/application adapter
     ↓
Validate session + required fields
     ↓
Submit when authorized
     ↓
Verify fresh receipt/evidence
     ├─ confirmed ─────────────► record applied
     ├─ confirmed not sent ───► bounded recovery/retry
     └─ ambiguous ────────────► submission_unverified + reconcile
```

The distinction between **submission** and **verification** is intentional. Browser automation often encounters states where the browser does not know whether the remote site accepted a request. The agent preserves that uncertainty instead of automatically retrying a potentially successful application.

## Getting started

### 1. Create a Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome
```

### 2. Create local configuration

```bash
cp .env.example .env
cp config.example.json config.json
```

Fill in only the providers and job sources you intend to use. Blank source credentials can be left disabled.

The project supports local Ollama first, with optional external model providers through configured OpenRouter, Anthropic, or OpenAI credentials. See `.env.example` for the current variables and defaults.

Keep `.env`, `config.json`, `state/profile.json`, browser sessions, resumes, and other personal data out of Git.

### 3. Add your profile and resume

Configure your target-role criteria and source resume in `config.json`, then provide the personal/work-history information required by the application flows in the local `state/` configuration described by the repository documentation.

### 4. Run discovery

```bash
python src/main.py discover
```

### 5. Review the environment before applying

```bash
python src/main.py preflight
```

For first-time testing, use a controlled non-submitting run:

```bash
python src/main.py apply --limit 1 --no-auto-submit
```

Only enable automated final submission after you have reviewed your configuration and the target-site behavior:

```bash
python src/main.py apply --auto-submit
```

## Common commands

```bash
# Discover and score jobs
python src/main.py discover

# Discover from one source
python src/main.py discover --source linkedin
python src/main.py discover --source indeed
python src/main.py discover --source usajobs
python src/main.py discover --source jobright

# Validate approved applications and environment
python src/main.py preflight

# Controlled application run without final submission
python src/main.py apply --limit 1 --no-auto-submit

# Authorized automated application run
python src/main.py apply --auto-submit

# Prepare or recover browser sessions
python src/main.py prepare-sessions

# Operational status and checks
python src/main.py status
python src/main.py ops-check
```

Use `python src/main.py --help` for the current command surface.

## Configuration and model routing

`.env.example` documents the supported runtime variables. The current inference path can use:

1. local Ollama;
2. an OpenRouter-compatible gateway;
3. Anthropic;
4. OpenAI.

You do not need to configure every provider. Features that depend on a missing provider or source should be disabled or skipped rather than requiring credentials you do not use.

`config.example.json` contains the non-secret job-search configuration. Real configuration belongs in your local `config.json` and should not be committed.

## Submission verification

Consequential browser automation needs stronger evidence than “the click did not throw an exception.” Job Agent therefore uses several rules that are important for both users and contributors:

- A click is not proof that an employer received an application.
- Existing success text is not proof that a new submission succeeded.
- A timeout is not proof that submission failed.
- An ambiguous result must be reconciled before another submit attempt.
- Fresh receipt evidence must be associated with the current attempt.
- `submission_unverified` is a valid result and should not be converted to success merely to make reporting look cleaner.

These rules are intentionally conservative because duplicate employer submissions are harder to undo than an application that needs manual review.

## Browser sessions

Each job source uses isolated browser/session state instead of sharing a normal everyday Chrome profile. This reduces browser-profile locking issues and makes background runs more predictable.

Some sites can be reauthenticated automatically. Others may require human interaction, especially when MFA, CAPTCHA, or unusual security challenges are involved. The agent is intended to fail safely rather than bypass those controls.

## Testing

Run the relevant pytest suite before changing application or receipt logic:

```bash
pytest
```

High-risk areas include:

- submission receipt freshness;
- duplicate-submission prevention;
- restart/retry behavior;
- ATS adapter selection;
- browser/session recovery;
- form validation and final-submit gating.

Tests for application flows should use controlled fixtures, mocks, or authorized test environments. Regression testing should not create real employer applications.

## Safety, privacy, and responsible use

This project automates interactions with third-party websites. Before using it, review the terms and automation policies of the services you connect to and make sure your use is authorized.

Do not commit personal information or credentials. Use `.env.example` and `config.example.json` only as templates, and review [`SECURITY.md`](SECURITY.md) for security guidance.

The project is intentionally not designed to defeat MFA, CAPTCHA, access controls, anti-bot protections, or other site security mechanisms.

## Documentation

Additional documentation is available in the repository:

- [`DEVELOPER_ONBOARDING.md`](DEVELOPER_ONBOARDING.md) — architecture and engineering internals.
- [`INTEGRATION_GUIDE.md`](INTEGRATION_GUIDE.md) — integration details.
- [`ATS_ADAPTER_PLAN.md`](ATS_ADAPTER_PLAN.md) — ATS adapter architecture and direction.
- [`SECURITY.md`](SECURITY.md) — credential and security practices.
- [`SECRETS.md`](SECRETS.md) — secret-resolution details.

## Relationship to AI Commander

Job Agent was developed as a specialized agent within the broader **AI Commander** ecosystem, where it can participate in centralized scheduling, health monitoring, incident recovery, shared model routing, and optional memory/knowledge workflows.

Those integrations are not required to understand the repository or experiment with the core job-search pipeline. The repository remains useful as a standalone project, while AI Commander provides a larger orchestration environment around it.

## Contributing

Contributions are most useful when they improve correctness rather than simply adding more automation. Good areas to contribute include:

- ATS adapters;
- receipt verification;
- site-change regressions;
- restart/idempotency behavior;
- safer session handling;
- tests and controlled fixtures;
- documentation.

For changes that can produce real-world side effects, include regression coverage and preserve the conservative submission-verification rules.