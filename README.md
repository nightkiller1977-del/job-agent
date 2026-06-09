# job-agent

A production job application agent that scrapes job listings, scores them against your criteria using Claude AI, presents a terminal review queue, and automates applications via Playwright.

For engineering handoff and apply-workflow internals, see `DEVELOPER_ONBOARDING.md`.

## Sources
- **jobright.ai** — matched/recommended jobs
- **LinkedIn** — discovery, saved-job import, Easy Apply/full-page apply, and external apply fallback
- **USAJobs.gov** — GS-15, SES, SL, and target role titles
- **External URLs** — pasted LinkedIn/Indeed/ATS/Jobright URLs can be added through the dashboard, hydrated locally, scored, and synced back

## Quick Start

### 1. Install dependencies
```bash
cd ~/Dev/Projects/job-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env and add your Anthropic API key
```

Optionally edit `config.json` to tweak compensation thresholds, search settings, or your local resume path.

### 3. Run

```bash
# Scrape all sources, score, and show review queue
python src/main.py discover

# Scrape a single source
python src/main.py discover --source linkedin
python src/main.py discover --source linkedin-saved --no-review
python src/main.py discover --source usajobs
python src/main.py discover --source jobright

# Hydrate externally pasted dashboard jobs using local browser sessions
python src/main.py hydrate

# Apply to all jobs you approved during review
python src/main.py apply

# Production-safe apply flow
python src/main.py preflight
python src/main.py ops-check
python src/main.py apply --limit 1 --no-auto-submit
python src/main.py apply --company "Microsoft" --no-auto-submit

# Fully automatic final submit, only after sessions/forms are verified
python src/main.py apply --auto-submit

# Show stats
python src/main.py status
```

## Scoring Criteria

### Target Roles (must match or close semantic match)
- Director of Software Engineering / Director of IT
- VP of IT / VP of Software Engineering / AVP of Software Engineering
- CTO / CIO
- Engineering Manager
- Program Manager (government/DoD context)
- GS-15 / SES / SL — federal positions (always apply)
- DoD/cleared positions at matching seniority

### Rejected Roles
- Software Engineer / Developer (IC)
- Staff / Principal Engineer (IC)
- Data Engineer / DevOps Engineer (unless Manager/Director of)

### Compensation Thresholds
| Work type | Min comp |
|---|---|
| Remote | $180k |
| Miami on-site | $230k |
| DC area on-site | $260k–$320k |
| Other on-site | Skip |
| Cleared/DoD remote | $170k |
| Cleared/DoD hybrid | $250k |
| Federal GS-15/SES/SL | Always apply |
| Contract remote | $180k |
| Contract hybrid | Flag for review |

Missing salary: always flag for review (never auto-skip).

## Review Queue Controls
```
[A] Apply     — mark job for application (runs via `apply` command)
[S] Skip      — skip and never show again
[B] Bookmark  — save for later reference
[Q] Quit      — exit queue (progress is saved)
```

## Safety
- **Never submits without user confirmation.** Every application pauses before final submit for your review.
- Use `--auto-submit` only after a targeted non-submit run has verified the source/session/form path.
- Use `python src/main.py preflight` before production apply runs. It pulls cloud-approved jobs into local SQLite and reports likely ATS blockers such as expired Workday sessions or portal login requirements.
- Use `python src/main.py ops-check` for the safe operational readiness check. It runs approved-queue preflight, then the mock Playwright apply-path suite for LinkedIn Easy Apply, LinkedIn external ATS, LinkedIn interstitial redirects, and Indeed → ATS handoff. Add `--skip-functional` when you only want the queue/session summary.
- Use `--limit`, `--job-id`, `--source`, or `--company` to run targeted application batches instead of attempting the full queue blindly.
- All discovered jobs are stored in SQLite (`state/jobs.db`) — you'll never apply to the same job twice.
- Browser runs in headed (visible) mode so you can see exactly what's happening.

## Apply Workflow

### Jobright and External Jobs
Jobright is the preferred apply path for company ATS pages because it provides both tailored resumes and autofill.

For Jobright external jobs, the agent follows the current Jobright UI:

1. Opens `https://jobright.ai/jobs/external`.
2. Finds or adds the external job URL.
3. Opens the matching job card's `CUSTOM RESUME` drawer.
4. Runs `Improve My Resume for This Job`.
5. Selects `Full Edit` and all missing keywords.
6. Clicks `Generate My New Resume`.
7. Downloads the generated tailored resume into `state/tailored_resumes/`.
8. Uses `APPLY WITH AUTOFILL` or the extracted ATS URL to open the company application form.
9. Uploads the tailored resume when the ATS exposes a file input.
10. Stops at final submit unless `--auto-submit` is explicitly set.

### LinkedIn Jobs
LinkedIn approved jobs first attempt Jobright tailoring before applying:

1. Search Jobright's External tab for the LinkedIn job.
2. If missing, add the LinkedIn URL through Jobright's `Add Job` field.
3. Generate/download the custom Jobright resume when available.
4. Open LinkedIn's apply path.
5. Support both older modal Easy Apply and newer full-page `/apply/` flows.
6. Fill contact/profile fields from `state/profile.json`.
7. Upload the tailored or configured resume when LinkedIn prompts for a file.

If LinkedIn does not expose a LinkedIn-hosted apply flow, the agent tries to extract the external company apply URL and delegates that ATS flow to Jobright's autofill-capable apply logic.

### Resume Resolution
The agent resolves resumes in this order:

1. Newly downloaded Jobright tailored resume.
2. `LOCAL_RESUME_PATH` or `RESUME_PATH` environment variable.
3. `local_resume_path` or `resume_path` in `config.json`.
4. Common local folders such as `state/tailored_resumes`, `state/resumes`, `~/Documents/Job App`, `~/Downloads`, and `~/Desktop`.

For a reliable fallback, place your base resume at:

```bash
mkdir -p state/resumes
# copy your current resume PDF to:
state/resumes/resume.pdf
```

## Project Structure
```
job-agent/
├── src/
│   ├── main.py           # CLI entry point
│   ├── orchestrator.py   # Flow coordinator
│   ├── scorer.py         # Claude-powered scoring
│   ├── state_manager.py  # SQLite state tracking
│   ├── review_queue.py   # Rich terminal UI
│   └── sources/
│       ├── base.py       # Base scraper (Playwright)
│       ├── jobright.py   # jobright.ai + external ATS autofill
│       ├── linkedin.py   # LinkedIn discovery, saved jobs, Easy Apply/full-page apply
│       └── usajobs.py    # USAJobs.gov + application flow
├── state/
│   └── jobs.db           # SQLite DB (auto-created)
├── config.json           # Scoring and search config
├── .env                  # Your API key (not committed)
└── requirements.txt
```

## Notes
- You must be logged in to all target sites before running discovery or apply. Use `python src/main.py prepare-sessions --source linkedin` or `--source jobright` to refresh browser sessions.
- For LinkedIn apply flows, the agent auto-fills common form fields (phone, years of experience, authorization, clearance) from `state/profile.json` and built-in senior-role defaults.
- For USAJobs, the agent selects your first saved resume and answers eligibility questions automatically.
- To set a local resume file path for upload prompts, update `local_resume_path` in `config.json` or set `LOCAL_RESUME_PATH`.
