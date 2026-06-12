# job-agent

A production job application agent that scrapes job listings, scores them against your criteria using Claude AI, presents a terminal review queue, and automates applications via Playwright using real Chrome.

For engineering internals and apply-workflow architecture, see [`DEVELOPER_ONBOARDING.md`](DEVELOPER_ONBOARDING.md).

## How It Works (Overview)

1. **Discover** — scrapes LinkedIn, Jobright, USAJobs; Claude scores each job against your role/comp criteria
2. **Review** — terminal queue where you approve, skip, or bookmark each scored job
3. **Apply** — fully automated: generates a Claude-tailored resume, autofills the ATS form, submits

All site logins (LinkedIn, Jobright, Indeed) are handled automatically using credentials in `.env`. No manual sign-in is needed for scheduled runs.

---

## Quick Start

### 1. Install dependencies
```bash
cd ~/Dev/Projects/job-agent
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
ANTHROPIC_API_KEY=...         # Required for scoring and ATS analysis
LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...
JOBRIGHT_EMAIL=...
JOBRIGHT_PASSWORD=...
INDEED_EMAIL=...
INDEED_PASSWORD=...
DASHBOARD_URL=...             # Render.com cloud dashboard URL
SYNC_SECRET=...
```

Set your resume path in `config.json`:
```json
{ "local_resume_path": "/Users/yourname/resume.pdf" }
```

Fill in your real work history, education, and skills in `state/profile.json` — this feeds LinkedIn Easy Apply form autofill and Claude resume tailoring. See the file for the expected structure.

### 3. One-time setup (install Chrome extension)

Logins are automatic from `.env`. The only manual step is installing the Jobright AI autofill extension into the job-agent Chrome profile — run this once:

```bash
python src/main.py setup
```

Chrome opens to the Web Store. Click **Add to Chrome** on the Jobright AI extension, then close the window.

### 4. Run

```bash
# Scrape all sources, score, show review queue
python src/main.py discover

# Single source
python src/main.py discover --source linkedin
python src/main.py discover --source linkedin-saved --no-review
python src/main.py discover --source usajobs
python src/main.py discover --source jobright

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

# Show stats
python src/main.py status
```

---

## What's Automated vs Manual

| Task | Automated? |
|---|---|
| LinkedIn login | ✅ Auto from `.env` |
| Jobright login | ✅ Auto from `.env` |
| Indeed login | ✅ Auto from `.env` |
| ATS score + resume tailoring | ✅ Claude Haiku API |
| Tailored resume PDF generation | ✅ Playwright + HTML template |
| Form autofill (Jobright extension) | ✅ Extension in Chrome profile |
| Final application submit | ✅ With `--auto-submit` |
| Workday company portals | ⚠️ One-time per company via `prepare-sessions` |
| Jobright extension install | ⚠️ One-time via `setup` |

---

## Apply Workflow

### Claude ATS Scoring (runs on every external ATS job)

Before filling any form, Claude Haiku analyzes the job description against your resume and returns:
- **ATS score** (0–100) — keyword match percentage
- **Missing keywords** — what's in the JD but not your resume
- **Tailored summary + bullets** — rewritten for this specific role
- **Cover letter** — 3-paragraph, role-specific

If the ATS score is below 85 and `--auto-submit` is set, a yellow warning is printed but the application still proceeds.

### Resume Priority Order

1. Claude-generated tailored PDF (`state/tailored_resumes/<title>_<company>_claude.pdf`)
2. Jobright Orion AI tailored resume (if Orion download succeeded)
3. `local_resume_path` in `config.json`
4. `LOCAL_RESUME_PATH` / `RESUME_PATH` env vars
5. Common locations: `state/resumes/`, `~/Documents/Job App/`, `~/Downloads/`, `~/Desktop/`

### Jobright Jobs

1. Opens the Jobright job detail page
2. Extracts the company ATS URL
3. Runs Jobright Orion AI resume tailoring; falls back to Claude PDF if Orion fails (~60% failure rate)
4. Opens the ATS URL
5. Runs Claude ATS scoring on the job description
6. Triggers Jobright Autofill extension
7. Runs pre-submit validation checklist (resume uploaded, required fields filled)
8. Submits unless `--auto-submit` is not set

### LinkedIn Jobs

1. Attempts Jobright resume tailoring first (same as above)
2. Opens LinkedIn job page
3. Supports both legacy modal Easy Apply and newer full-page `/jobs/view/<id>/apply/` flows
4. Autofills phone/contact/profile fields from `state/profile.json`
5. Uploads tailored or fallback resume
6. If no LinkedIn-hosted apply flow found, extracts the external ATS URL and routes to the Jobright ATS path

### Session Management

Most logins are automatic. Workday portals are the exception — each company's Workday instance requires a one-time manual login:

```bash
python src/main.py prepare-sessions              # opens all session-blocked jobs
python src/main.py prepare-sessions --source jobright --company "CVS"
```

---

## Scoring Criteria

### Target Roles
- Director of Software Engineering / Director of IT
- VP of IT / VP of Software Engineering / AVP of Software Engineering
- CTO / CIO / Engineering Manager
- Program Manager (government/DoD)
- GS-15 / SES / SL — federal (always apply)
- DoD/cleared positions at matching seniority

### Rejected Roles
- Software Engineer / Developer (IC)
- Staff / Principal Engineer (IC)
- Data Engineer / DevOps Engineer (unless Manager/Director of)

### Compensation Thresholds
| Work type | Min |
|---|---|
| Remote | $180k |
| Miami on-site | $230k |
| DC area on-site | $260k–$320k |
| Other on-site | Skip |
| Cleared/DoD remote | $170k |
| Cleared/DoD hybrid | $250k |
| Federal GS-15/SES/SL | Always apply |
| Contract remote | $180k |

Missing salary: always flag for review.

---

## Review Queue Controls
```
[A] Apply     — mark job for application
[S] Skip      — skip and never show again
[B] Bookmark  — save for later reference
[Q] Quit      — exit (progress is saved)
```

---

## Safety

- `--auto-submit` is required for actual submission — runs without it stop before the final click
- All discovered jobs stored in SQLite (`state/jobs.db`) — never applies to the same job twice
- Browser runs in headed (visible) mode — Chrome windows open and close automatically; no interaction needed
- `python src/main.py preflight` reports session blockers before you start a full run
- `python src/main.py ops-check` runs preflight + mock Playwright apply-path tests

---

## Scheduling

```bash
# Example: run every weekday at 8am
# Add to crontab (crontab -e)
0 8 * * 1-5 cd /Users/alarkins/Dev/Projects/job-agent && source .venv/bin/activate && python src/main.py apply --auto-submit >> /tmp/job-agent.log 2>&1
```

Chrome windows open and close automatically. No user presence needed.

---

## Project Structure

```
job-agent/
├── src/
│   ├── main.py              # CLI entry point + command definitions
│   ├── orchestrator.py      # Flow coordinator (discover, apply, setup, prepare-sessions)
│   ├── scorer.py            # Claude-powered job scoring
│   ├── state_manager.py     # SQLite state + analytics persistence
│   ├── review_queue.py      # Rich terminal review UI
│   └── sources/
│       ├── base.py          # BaseScraper: Chrome profile, session management, delays
│       ├── jobright.py      # Jobright discovery, Orion tailoring, Claude ATS, external ATS autofill
│       ├── linkedin.py      # LinkedIn discovery, saved jobs, Easy Apply, full-page apply
│       └── usajobs.py       # USAJobs.gov discovery + application
├── state/
│   ├── jobs.db              # SQLite DB (auto-created, gitignored)
│   ├── profile.json         # Your real work history/skills for form autofill (gitignored)
│   ├── sessions/            # Persistent Chrome profiles per source (gitignored)
│   ├── tailored_resumes/    # Claude + Jobright generated PDFs (gitignored)
│   └── extensions/          # Local Chrome extension fallbacks (gitignored)
├── dashboard/               # Render.com cloud API + approval UI
├── config.json              # Scoring config, resume path, search settings
├── .env                     # Credentials + API keys (never committed)
├── DEVELOPER_ONBOARDING.md  # Engineering internals and architecture
└── requirements.txt
```

---

## Notes

- `state/profile.json` must contain real data (name, phone, work history, skills) — placeholder values cause LinkedIn Easy Apply validation failures
- The job-agent Chrome profile (`state/sessions/jobright_profile`) is separate from your personal Chrome profile — no risk of corruption or conflict
- Sessions in the Chrome profile persist between runs; if a session expires, the scraper auto-re-logs in using `.env` credentials
- Do not commit `.env`, `state/jobs.db`, `state/profile.json`, or anything under `state/sessions/` or `state/tailored_resumes/`
