# Developer Onboarding

Engineering reference for the job-agent apply workflow. Read this before changing any apply automation. Keep it current after every significant change.

**Last updated:** June 2026

---

## System Overview

The agent has three phases that run independently:

1. **Discover** — Playwright scrapes LinkedIn/Jobright/USAJobs, Claude scores each job, results go into SQLite
2. **Review** — Terminal queue (Rich UI) where the user approves/skips/bookmarks each scored job
3. **Apply** — Fully automated: per-job ATS scoring via Claude Haiku, resume tailoring, form autofill, submit

All site logins are automated via credentials in `.env`. Workday company portals are the exception (one-time manual login per company via `prepare-sessions`).

---

## Browser Architecture

**All Playwright runs use real Chrome** (`channel="chrome"` in `BaseScraper._start_browser()`), not Playwright's bundled Chromium.

Reasons:
- Chrome Web Store extensions (Jobright AI autofill) work correctly
- macOS Keychain password autofill works
- Chrome-specific cookie encryption works

Each source scraper gets its own persistent Chrome profile directory at `state/sessions/{source}_profile/`. This keeps the job-agent sessions isolated from the user's personal Chrome profile — no locking, no corruption risk.

Sessions persist in the profile directory between runs. When a session expires, the scraper's `_auto_login()` method re-authenticates automatically using `.env` credentials before continuing.

The Jobright autofill Chrome extension is installed once into the job-agent Chrome profile via:
```bash
python src/main.py setup
```
After that, the extension loads automatically from the profile on every run.

---

## Credential and Login Flow

Credentials live in `.env`:
```
LINKEDIN_EMAIL / LINKEDIN_PASSWORD
JOBRIGHT_EMAIL / JOBRIGHT_PASSWORD
INDEED_EMAIL / INDEED_PASSWORD
```

Each scraper checks on startup whether it's logged in. If not, `_auto_login()` fills the login form programmatically. No manual interaction is needed for scheduled runs.

The cloud dashboard (`DASHBOARD_URL` / `SYNC_SECRET`) syncs job approvals from the web UI to local SQLite. `orchestrator.load_credentials_from_dashboard()` also pulls credentials from the dashboard at runtime.

---

## Key Files

| File | Role |
|---|---|
| `src/main.py` | CLI command definitions and argument parsing |
| `src/orchestrator.py` | Top-level flow: discover, apply loop, setup, prepare-sessions, analytics wiring |
| `src/sources/base.py` | `BaseScraper`: Chrome launch, persistent profile, session lock cleanup, delay helpers |
| `src/sources/jobright.py` | Jobright discovery, Orion tailoring, Claude ATS scoring, external ATS autofill/apply |
| `src/sources/linkedin.py` | LinkedIn discovery, saved-job import, Easy Apply, full-page apply, external ATS fallback |
| `src/sources/usajobs.py` | USAJobs.gov discovery and application |
| `src/scorer.py` | Claude-powered job scoring against role/comp criteria |
| `src/state_manager.py` | SQLite CRUD, `record_apply_attempt()`, `record_application_analytics()` |
| `src/review_queue.py` | Rich terminal review UI |
| `src/resume_helper.py` | Resume path resolution and form-field fallback filler |
| `dashboard/main.py` | Cloud dashboard API, sync, external placeholder jobs, credential endpoints |
| `state/profile.json` | Real resume data for LinkedIn Easy Apply autofill (gitignored) |
| `config.json` | Scoring config, search settings, local resume path |
| `.env` | Credentials + API keys (never committed) |

---

## Apply Architecture

### Entry Point

`orchestrator.apply_approved()` → iterates approved jobs from SQLite → calls `scraper.apply(job, auto_submit)` for each.

### Jobright External ATS Jobs

`JobrightScraper.apply_external_ats_job(job, auto_submit)`:

1. Open company ATS URL
2. **Claude ATS scoring** — extract job description, call `_claude_ats_and_tailor(job, jd_text)`, get score/keywords/tailored content
3. If ATS score < 85 and `auto_submit=True` → print yellow warning, continue
4. If no Orion tailored resume available → generate Claude tailored PDF via `_generate_tailored_resume_pdf()`
5. Click the ATS entry button via `_click_ats_apply_button()` (SmartRecruiters: JS click fallback)
6. Trigger Jobright autofill extension via `_trigger_autofill()`
7. Run pre-submit validation checklist via `_run_pre_submission_validation()`
8. Final submit via `_confirm_and_submit()` — sets `self._apply_analytics`

### LinkedIn Jobs

`LinkedInScraper.apply(job, auto_submit)`:

1. Attempt Jobright resume tailoring via `JobrightScraper.tailor_resume_for_external_job(job)`
2. Fall back to `resolve_resume_path(config)`
3. Open LinkedIn job URL
4. Support both legacy modal Easy Apply and newer full-page `/jobs/view/<id>/apply/` flows
5. Fill contact/profile fields from `state/profile.json` via `ResumeFieldFixer`
6. Upload tailored or fallback resume when LinkedIn exposes `input[type=file]`
7. If LinkedIn has no hosted apply flow → extract external URL → delegate to `JobrightScraper.apply_external_ats_job()`

### Jobright Orion Resume Tailoring

`_generate_tailored_resume()` (called before external ATS apply):

1. Navigate to `https://jobright.ai/jobs/external`
2. Find or add the external job URL via `_find_external_jobright_match()` / `_add_external_job_to_jobright()`
3. Open `CUSTOM RESUME` drawer
4. Run Orion wizard: `Improve My Resume for This Job` → `Full Edit` → `Select all` → `Generate My New Resume`
5. Download tailored PDF to `state/tailored_resumes/`
6. Sets `self._jobright_available = True` on success

Orion has ~60% failure rate. When it fails, `_generate_tailored_resume_pdf()` generates a Claude-based PDF instead.

### Claude ATS Scoring (`_claude_ats_and_tailor`)

Model: `claude-haiku-4-5-20251001`

Input: job description text (up to 6000 chars, extracted via `_extract_job_description()`) + resume text (from `pypdf` on the configured PDF, fallback to `state/profile.json`)

Returns JSON:
```json
{
  "ats_score": 87,
  "missing_keywords": ["FedRAMP", "Zero Trust"],
  "matching_keywords": ["Agile", "AWS", "Python"],
  "tailored_summary": "...",
  "tailored_bullets": [{"role": "...", "bullets": ["..."]}],
  "cover_letter": "...",
  "recommendation": "..."
}
```

Score and missing keywords are stored in `self._last_ats_score` / `self._last_ats_missing_keywords` for analytics.

### Claude Tailored PDF Generation (`_generate_tailored_resume_pdf`)

Used as fallback when Orion fails. Builds an HTML resume from `state/profile.json` + Claude tailored content, renders to PDF via Playwright `page.pdf()`, saves to `state/tailored_resumes/{title}_{company}_claude.pdf`. Zero external dependencies beyond Playwright (already installed).

### Analytics

On successful submit, `_confirm_and_submit()` sets `self._apply_analytics`:
```python
{
    "submitted": True,
    "submissionTime": "...",
    "applicationMethod": "Direct ATS" | "LinkedIn Easy Apply" | ...,
    "atsScore": 87,
    "resumeVersion": "resume_claude.pdf",
    "missingKeywords": [...],
    "jobrightAvailable": True,
    "jobrightUrl": "https://...",
    "interviewReceived": False,
    "offerReceived": False,
}
```

`orchestrator.apply_approved()` calls `state.record_application_analytics(job_id, analytics)` after each successful submit, merging into the `extra_json` column in SQLite.

---

## Resume Priority Order

1. Claude-generated tailored PDF: `state/tailored_resumes/<title>_<company>_claude.pdf`
2. Jobright Orion tailored PDF: `state/tailored_resumes/` (newest match)
3. `local_resume_path` in `config.json`
4. `LOCAL_RESUME_PATH` / `RESUME_PATH` env vars
5. Common folders: `state/resumes/`, `~/Documents/Job App/`, `~/Downloads/`, `~/Desktop/`

Stable base resume location: `state/resumes/resume.pdf` — place a copy here for consistent fallback.

---

## SmartRecruiters Fix

SmartRecruiters "I'm interested" button uses Unicode right single quote (U+2019) in the HTML. Standard `text-matches()` Playwright selector fails. Fixed in `_click_ats_apply_button()` via JS:

```javascript
const btn = els.find(el => /i[''']m interested/i.test((el.textContent || '').trim()));
```

"I'm interested" is **not** in `submit_selectors` — it's a page-entry CTA, not a final submit.

---

## Critical Safety Rules

- **Never remove `form_empty_not_submitted` guard** — protects against false-submit on GDIT and similar ATS pages where the form appears empty but a submit button is present
- **Never relax `submit_selectors` anchors** — removing `^`/`$` regex anchors re-introduces false-submit on buttons like "Apply Now" that appear before the form
- **`--auto-submit` is required for real submission** — runs without it stop before the final click
- Never commit `.env`, `state/jobs.db`, `state/profile.json`, `state/sessions/`, or `state/tailored_resumes/`
- Browser runs headed (non-headless) — Chrome extensions don't work in headless mode

---

## profile.json

`state/profile.json` feeds `ResumeFieldFixer` for LinkedIn Easy Apply form autofill. It must contain real data or LinkedIn validation will fail.

Required fields: `personal_info` (name, email, phone, city, state, zip), `work_history` (with real company names, titles, dates, descriptions), `education`, `skills`, `certifications`.

The file is gitignored. Do not commit it.

---

## Session Management

Most logins are fully automatic. Exceptions:

**Workday portals** — each company (CVS Health, UMCareerStaff, Simpro, etc.) has its own Workday instance. These require a one-time manual login:

```bash
python src/main.py prepare-sessions
python src/main.py prepare-sessions --source jobright --company "CVS"
```

Jobs blocked on Workday session show status `needs-session` in preflight output.

**Jobright extension** — install once via `setup`, then auto-loads from Chrome profile:

```bash
python src/main.py setup
```

---

## Scheduling

The apply run is safe to schedule — all logins are automatic, Chrome windows open and close unattended:

```bash
# crontab -e
0 8 * * 1-5 cd /Users/alarkins/Dev/Projects/job-agent && source .venv/bin/activate && python src/main.py apply --auto-submit >> /tmp/job-agent.log 2>&1
```

---

## Key Commands Reference

```bash
source .venv/bin/activate

# One-time setup (install Jobright extension)
python src/main.py setup

# Discovery
python src/main.py discover --source linkedin --no-review
python src/main.py discover --source linkedin-saved --no-review
python src/main.py discover --source jobright --no-review
python src/main.py discover --source usajobs --no-review
python src/main.py hydrate

# Preflight before any apply run
python src/main.py preflight
python src/main.py ops-check

# Session prep (Workday portals only)
python src/main.py prepare-sessions
python src/main.py prepare-sessions --source jobright --company "CVS"

# Apply
python src/main.py apply --auto-submit                        # full queue
python src/main.py apply --source jobright --limit 1 --no-auto-submit  # safe test
python src/main.py apply --job-id <id> --auto-submit           # single job

# Stats
python src/main.py status
```

---

## Verification Checklist

Run before claiming the apply workflow is working:

```bash
# Syntax check
PYTHONPYCACHEPREFIX=/private/tmp/job-agent-pycache \
  .venv/bin/python -m py_compile \
  src/main.py src/orchestrator.py src/resume_helper.py \
  src/sources/jobright.py src/sources/linkedin.py src/state_manager.py \
  dashboard/main.py

git diff --check
python src/main.py preflight
python src/main.py ops-check
python src/main.py apply --source jobright --company Pfizer --no-auto-submit
python src/main.py apply --source linkedin --limit 1 --no-auto-submit
```

---

## Known Issues (as of June 2026)

- **FICO LinkedIn Easy Apply** — stuck on page 2/3 with unknown validation error; `state/profile.json` real data may resolve it; not yet confirmed
- **Netflix (EightFold AI), Bayview, LabConnect (DayForce HCM)** — new ATS types, not fully handled
- **Workday sessions** — CVS Health, UMCareerStaff, Simpro need `prepare-sessions` before they can be applied to
- **Jobright Orion ~60% failure rate** — Claude PDF fallback handles this automatically
- **ATS score gate is non-blocking** — score < 85 prints a warning but does not stop `--auto-submit` runs (by design, can be tightened)
