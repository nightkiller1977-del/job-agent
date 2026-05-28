# job-agent

A production job application agent that scrapes job listings, scores them against your criteria using Claude AI, presents a terminal review queue, and automates applications via Playwright.

## Sources
- **jobright.ai** — matched/recommended jobs
- **LinkedIn** — Easy Apply jobs filtered by Director+, last 7 days
- **USAJobs.gov** — GS-15, SES, SL, and target role titles

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
python src/main.py discover --source usajobs
python src/main.py discover --source jobright

# Apply to all jobs you approved during review
python src/main.py apply

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
- All discovered jobs are stored in SQLite (`state/jobs.db`) — you'll never apply to the same job twice.
- Browser runs in headed (visible) mode so you can see exactly what's happening.

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
│       ├── jobright.py   # jobright.ai
│       ├── linkedin.py   # LinkedIn + Easy Apply
│       └── usajobs.py    # USAJobs.gov + application flow
├── state/
│   └── jobs.db           # SQLite DB (auto-created)
├── config.json           # Scoring and search config
├── .env                  # Your API key (not committed)
└── requirements.txt
```

## Notes
- You must be logged in to all three sites before running discovery. If not logged in, the agent opens the browser and prompts you to log in before continuing.
- For LinkedIn Easy Apply, the agent auto-fills common form fields (years of experience, authorization, clearance) based on your profile.
- For USAJobs, the agent selects your first saved resume and answers eligibility questions automatically.
- Add your phone number to `USER_ANSWERS["phone_default"]` in `src/sources/linkedin.py` if LinkedIn prompts for it.
- To set a local resume file path (for upload prompts), update `local_resume_path` in `config.json`.
