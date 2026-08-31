# job-agent

A fully automated job application pipeline that scrapes listings across multiple job boards, scores them against your role and compensation criteria using Claude AI, presents a terminal review queue, and applies — including resume tailoring, ATS form autofill, and final submission — via Playwright-controlled Chrome. When a site session expires, the agent heals itself automatically, writes a regression test, and sends you an iMessage summary of what it fixed.

For engineering internals and apply-workflow architecture, see [`DEVELOPER_ONBOARDING.md`](DEVELOPER_ONBOARDING.md).

---

## Capabilities at a Glance

| Capability | Details |
|---|---|
| **Multi-source discovery** | LinkedIn, Jobright, Indeed, USAJobs |
| **AI scoring** | Claude evaluates each job against your role, seniority, and comp criteria |
| **Terminal review queue** | Approve / skip / bookmark with keyboard controls |
| **Resume tailoring** | Claude rewrites resume bullets + summary for each specific role |
| **ATS autofill** | Playwright fills Workday, BrassRing, Greenhouse, Lever, and fallback portals |
| **LinkedIn Easy Apply** | Full modal and full-page `/apply/` flows, reading `profile.json` |
| **Session management** | Persistent Chrome profiles per source; one-time prepare-sessions for Workday SSO |
| **Self-healing auth** | Expired sessions auto-recovered; human-assist path for 2FA-gated sources |
| **iMessage alerts** | Auth failures, self-heal outcomes, and correction summaries sent to your phone |
| **macOS notifications** | Native banners for every error, warning, and reauth event |
| **Regression test generation** | Each successful self-heal appends a pytest test documenting the exact failure |
| **Cloud dashboard** | Render.com API for approving/rejecting jobs from any browser |
| **Scheduled runs** | Cron-safe; Chrome opens and closes automatically, no user presence needed |

---

## Architecture

```
job-agent/
├── src/
│   ├── main.py              # CLI entry point — all commands defined here
│   ├── orchestrator.py      # Flow coordinator: discover → score → review → apply
│   │                        #   catches AuthFailedError, invokes ReauthManager, retries
│   ├── scorer.py            # Claude Haiku job scoring (role match, ATS score, tailoring)
│   ├── reauth.py            # ReauthManager — self-healing auth (automated + human paths)
│   │                        #   on success: writes regression test + sends iMessage
│   ├── notifier.py          # agent_status.json writer + macOS notification dispatcher
│   │                        #   record_reauth_event() appends to a capped event log
│   ├── state_manager.py     # SQLite persistence: jobs, status, analytics
│   ├── review_queue.py      # Rich terminal review UI
│   ├── resume_helper.py     # Resume path resolution + Claude PDF generation
│   └── sources/
│       ├── base.py          # BaseScraper: Chrome profile isolation, session export,
│       │                    #   _safe_evaluate(), _safe_goto(), AuthFailedError
│       ├── jobright.py      # Discovery, Orion AI tailoring, Claude ATS, ATS autofill
│       ├── linkedin.py      # Discovery, saved jobs, Easy Apply, external ATS routing
│       ├── indeed.py        # Discovery + application
│       └── usajobs.py       # USAJobs.gov discovery + application
├── tests/
│   ├── test_reauth_unit.py        # 53 unit tests — auth, safe_evaluate, reauth routing
│   ├── test_reauth_feature.py     # 16 integration tests — orchestrator end-to-end flows
│   ├── test_reauth_regressions.py # Auto-generated — appended on each self-heal
│   ├── test_state_manager.py
│   ├── test_apply_functional.py
│   └── test_credentials.py
├── dashboard/               # Render.com cloud API + job approval UI
├── state/
│   ├── jobs.db              # SQLite (auto-created, gitignored)
│   ├── profile.json         # Work history, skills, contact info for form autofill (gitignored)
│   ├── sessions/            # Per-source Chrome profiles + session JSON exports (gitignored)
│   ├── tailored_resumes/    # Claude-generated and Orion-generated PDFs (gitignored)
│   └── agent_status.json    # Live alerts + reauth event log
├── config.example.json      # Template — copy to config.json (gitignored) and edit
├── config.json              # Scoring config, resume path, search settings (gitignored, not committed)
├── .env.example             # Template with all required and optional keys
├── SECURITY.md              # Vulnerability disclosure and credential management policy
├── CLAUDE.md                # Critical constraints for Claude Code (Chrome profile isolation)
├── DEVELOPER_ONBOARDING.md  # Engineering internals and apply-workflow architecture
└── requirements.txt
```

### Data Flow

```
main.py
  └─ Orchestrator.discover()
        ├─ ScraperCls(config).scrape()   ← raises AuthFailedError on session expiry
        │     └─ ReauthManager.handle()  ← automated or human-assist reauth
        │           ├─ _reauth_automated()  → _auto_login() → export session JSON
        │           └─ _reauth_human()     → iMessage → poll mtime → detect refresh
        │                 └─ on success: _write_regression_test() + _notify_correction()
        ├─ JobScorer.score()             ← Claude Haiku evaluation
        └─ ReviewQueue.run()             ← terminal approve/skip/bookmark

  └─ Orchestrator.apply_approved()
        ├─ scraper.apply(job)            ← raises AuthFailedError if session gone
        │     └─ ReauthManager.handle(context="apply")  ← shorter timeout
        └─ retry scraper.apply(job)
```

### Self-Healing Auth

```
AuthFailedError raised by scraper
          │
          ▼
  ReauthManager.handle(source, detail, context)
          │
    ┌─────┴──────┐
    │            │
automated    human-assisted
(jobright,   (usajobs)
 indeed,
 linkedin)
    │            │
_auto_login()  iMessage → poll session file mtime
    │            │
    └────────────┘
          │
     on success
          ├─ record_reauth_event(source, mode, "success")
          ├─ _write_regression_test()   → tests/test_reauth_regressions.py
          ├─ _notify_correction()       → iMessage to NOTIFY_PHONE
          └─ return True               → orchestrator retries the scraper
```

### Browser Architecture

Each source scraper uses an **isolated Chromium profile** under `state/sessions/<source>_profile/`. This avoids Chrome's ProcessSingleton lock that would occur if any scraper pointed at the main Chrome profile. Session cookies are exported to `state/sessions/<source>_chromium.json` after every successful login so headless background runs can load them without re-authenticating.

`_safe_evaluate()` and `_safe_goto()` on `BaseScraper` protect every `page.evaluate()` and `page.goto()` call:

- Non-fatal Playwright errors (selector timeouts, parse failures) return a configurable default and log a warning.
- Browser-death signals (`closed`, `detached`, `crashed`, `browser has been`) are re-raised immediately so the scraper loop exits cleanly rather than silently swallowing the failure.

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — add your API key and site credentials
```

Key `.env` values:

```
ANTHROPIC_API_KEY=...          # Required for scoring and resume tailoring
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...
JOBRIGHT_EMAIL=...
JOBRIGHT_PASSWORD=...
INDEED_EMAIL=...
INDEED_PASSWORD=...
COMPANY_EMAIL=...              # Workday / BrassRing portal auto-login
COMPANY_PASSWORD=...
NOTIFY_PHONE=+1XXXXXXXXXX      # iMessage alerts and self-heal notifications
REAUTH_TIMEOUT_MINUTES=30      # How long to wait for a human-assisted session refresh
REAUTH_TIMEOUT_APPLY_MINUTES=10
DASHBOARD_URL=...              # Render.com cloud dashboard URL
SYNC_SECRET=...
```

Copy `config.example.json` to `config.json` (gitignored — this is where your real target roles, compensation thresholds, and resume path live) and set your resume path:

```json
{ "local_resume_path": "/path/to/your/resume.pdf" }
```

Fill in `state/profile.json` with your work history, skills, and contact information — this feeds LinkedIn Easy Apply autofill and Claude resume tailoring.

### 3. One-time setup

```bash
python src/main.py setup
```

Chrome opens to the Web Store. Click **Add to Chrome** on the Jobright AI extension, then close the window. All source logins are automatic from `.env` — no other manual steps required.

### 4. Run

```bash
# Scrape all sources, score, and open review queue
python src/main.py discover

# Single source
python src/main.py discover --source linkedin
python src/main.py discover --source linkedin-saved --no-review
python src/main.py discover --source usajobs
python src/main.py discover --source jobright
python src/main.py discover --source indeed

# Hydrate externally pasted dashboard jobs
python src/main.py hydrate

# Check approved queue before applying
python src/main.py preflight

# Apply to all approved jobs — fully automatic
python src/main.py apply --auto-submit

# Targeted apply (safer for first-time testing)
python src/main.py apply --limit 1 --no-auto-submit
python src/main.py apply --company "Microsoft" --no-auto-submit
python src/main.py apply --job-id <job_id> --auto-submit

# Resolve blocked Workday sessions
python src/main.py prepare-sessions
python src/main.py prepare-sessions --source jobright --company "CVS"

# Show stats
python src/main.py status

# Pre-flight check + mock apply-path tests
python src/main.py ops-check
```

---

## What's Automated vs Manual

| Task | Status |
|---|---|
| LinkedIn login | ✅ Auto from `.env` |
| Jobright login | ✅ Auto from `.env` |
| Indeed login | ✅ Auto from `.env` |
| USAJobs login | ✅ Auto from `.env` via login.gov |
| Company ATS portals (Workday, BrassRing) | ✅ Auto attempt via `COMPANY_EMAIL/PASSWORD` |
| ATS scoring + resume tailoring | ✅ Claude Haiku API |
| Tailored resume PDF generation | ✅ Playwright + HTML template |
| LinkedIn Easy Apply autofill | ✅ Reading `state/profile.json` |
| Jobright extension autofill | ✅ Extension in Chrome profile |
| Final application submit | ✅ With `--auto-submit` |
| Session self-healing (LinkedIn, Jobright, Indeed) | ✅ Fully automated |
| Session self-healing (USAJobs 2FA) | ✅ iMessage prompt + mtime polling |
| Regression test on each self-heal | ✅ Auto-written to `tests/test_reauth_regressions.py` |
| iMessage on each self-heal | ✅ Sent to `NOTIFY_PHONE` |
| Workday portals that reject `COMPANY_EMAIL` | ⚠️ One-time `prepare-sessions` per company |
| Jobright extension install | ⚠️ One-time `setup` |
| USAJobs 2FA (first run only) | ⚠️ Human in browser, then auto thereafter |

---

## Apply Workflow

### Resume Priority Order

1. Claude-generated tailored PDF (`state/tailored_resumes/<title>_<company>_claude.pdf`)
2. Jobright Orion AI tailored resume (if Orion download succeeded)
3. `local_resume_path` in `config.json`
4. `LOCAL_RESUME_PATH` / `RESUME_PATH` env vars
5. Common locations: `state/resumes/`, `~/Documents/`, `~/Downloads/`, `~/Desktop/`

### Claude ATS Scoring

Before filling any form, Claude Haiku analyzes the job description and returns:

- **ATS score** (0–100) — keyword match against your resume
- **Missing keywords** — in the JD but not your resume
- **Tailored summary + bullets** — rewritten for this specific role
- **Cover letter** — 3-paragraph, role-specific

If the ATS score is below 85 and `--auto-submit` is set, a warning is printed but the application proceeds.

### Jobright Jobs

1. Opens job detail page and extracts company ATS URL
2. Runs Jobright Orion AI resume tailoring; falls back to Claude PDF if Orion fails
3. Opens ATS URL and runs Claude ATS scoring
4. Triggers Jobright Autofill extension for form filling
5. Runs pre-submit validation checklist (resume uploaded, required fields filled)
6. Submits unless `--auto-submit` is not set

### LinkedIn Jobs

1. Runs Jobright resume tailoring first (same as above)
2. Opens LinkedIn job page
3. Supports both legacy modal Easy Apply and newer full-page `/jobs/view/<id>/apply/` flows
4. Autofills phone, contact, and profile fields from `state/profile.json`
5. Uploads tailored or fallback resume
6. If no LinkedIn apply flow found, extracts external ATS URL and routes to ATS path

---

## Scoring Configuration

Scoring criteria are configured in `config.json`. The agent evaluates each job listing against your defined:

- **Target roles** — job titles and seniority levels you want to apply to
- **Rejected roles** — individual contributor or out-of-scope titles to skip
- **Compensation thresholds** — minimum salary by work type (remote, on-site, hybrid, cleared, federal)

Claude uses these to score each listing 0–100 and assign a recommendation. You review borderline scores in the terminal queue before any application is submitted.

See `.env.example` and `config.example.json` for all configurable fields.

---

## Review Queue Controls

```
[A] Apply     — mark job for application
[S] Skip      — skip and never show again
[B] Bookmark  — save for later reference
[Q] Quit      — exit (progress is saved)
```

---

## Testing

```bash
source .venv/bin/activate
pytest tests/test_reauth_unit.py tests/test_reauth_feature.py -v
```

| Suite | Tests | Coverage |
|---|---|---|
| `test_reauth_unit.py` | 53 | `AuthFailedError`, `_safe_evaluate`, `_safe_goto`, `record_reauth_event`, `ReauthManager` routing, automated reauth, human reauth, `_write_regression_test`, `_notify_correction` |
| `test_reauth_feature.py` | 16 | Full orchestrator flows: discover + apply with reauth success/failure/retry-failure paths; `_safe_evaluate` integration through concrete scrapers |
| `test_reauth_regressions.py` | auto-grows | Appended by `ReauthManager` every time a real session self-heals in production |
| `test_state_manager.py` | — | SQLite persistence layer |
| `test_apply_functional.py` | — | Apply-workflow end-to-end |
| `test_credentials.py` | — | Credential loading and validation |

---

## Notifications

Every significant event writes to `state/agent_status.json` and fires a native macOS notification banner. The `reauth_events` list in that file is capped at 100 entries and records every reauth attempt outcome for post-run inspection.

| Event | Channel |
|---|---|
| Auth failure detected | macOS notification |
| Automated reauth started | macOS notification |
| Session refreshed (automated) | macOS notification + iMessage |
| Session refreshed (human-assisted) | macOS notification + iMessage |
| Reauth timed out | macOS notification |
| Missing credentials | macOS notification |

iMessage format on self-heal:

```
✅ Job Agent self-healed: JOBRIGHT
What failed: redirect to /login
How fixed: automated reauth
When: 2026-06-26 14:22:01 UTC
Status: Session refreshed — source will be retried
```

---

## Safety

- `--auto-submit` is required for actual submission — runs without it stop before the final click
- All discovered jobs are stored in SQLite — the agent never applies to the same job twice
- Browser runs in headed (visible) mode — Chrome windows open and close automatically
- `python src/main.py preflight` reports session blockers before a full run
- `python src/main.py ops-check` runs preflight + mock Playwright apply-path tests
- Never commit `.env`, `state/jobs.db`, `state/profile.json`, `state/sessions/`, or `state/tailored_resumes/`

---

## Scheduling

```bash
# Example: run every weekday at 8am
# crontab -e
0 8 * * 1-5 cd /path/to/job-agent && source .venv/bin/activate && python src/main.py apply --auto-submit >> /tmp/job-agent.log 2>&1
```

Chrome opens and closes automatically — no user presence required. If a session expires mid-run, `ReauthManager` recovers it and retries the source without interrupting the rest of the queue.

---

## Security

All credentials are managed via environment variables. See [`.env.example`](./.env.example) for the full list of required and optional keys, and [`SECURITY.md`](./SECURITY.md) for the credential management policy and secrets resolution architecture.

**Never commit `.env` to the repository.** The secrets resolver (`src/secret_store.py`) supports a phased credential architecture — local `.env`, encrypted store (Phase 2), and a dedicated CLI (Phase 3).

---

## License

TBD
