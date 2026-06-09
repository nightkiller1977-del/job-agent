# Developer Onboarding

This file is the engineering handoff for the job-agent apply workflow. Read it before changing apply automation.

## Current Goal

The production goal is not just discovery. The app must:

1. Pull approved jobs from local SQLite and the cloud dashboard.
2. For LinkedIn and external jobs, use local browser sessions on the user's Mac.
3. Add external URLs to Jobright when needed.
4. Generate a Jobright custom resume for the specific job.
5. Download the tailored resume to `state/tailored_resumes/`.
6. Use that tailored resume for LinkedIn Easy Apply, LinkedIn full-page apply, or company ATS upload.
7. Use Jobright Autofill on company ATS pages.
8. Stop before final submit unless `--auto-submit` is explicitly provided.

## Key Commands

```bash
source .venv/bin/activate

# Discovery
python src/main.py discover --source linkedin --no-review
python src/main.py discover --source linkedin-saved --no-review
python src/main.py discover --source jobright --no-review

# External URL hydration from dashboard placeholders
python src/main.py hydrate

# Preflight before any apply run
python src/main.py preflight
python src/main.py preflight --source linkedin
python src/main.py preflight --source jobright
python src/main.py ops-check

# Prepare authenticated browser sessions
python src/main.py prepare-sessions --source linkedin
python src/main.py prepare-sessions --source jobright

# Safe apply tests
python src/main.py apply --source jobright --company Pfizer --no-auto-submit
python src/main.py apply --source linkedin --limit 1 --no-auto-submit

# Final submit mode, only after targeted safe runs verify the form path
python src/main.py apply --auto-submit
```

## Important Files

- `src/main.py` - CLI command definitions.
- `src/orchestrator.py` - Pulls cloud-approved jobs, preflight filtering, session prep, apply loop, external hydration.
- `src/sources/jobright.py` - Jobright discovery, custom resume generation, external ATS extraction, autofill/apply.
- `src/sources/linkedin.py` - LinkedIn discovery, saved jobs, Easy Apply/full-page apply, Jobright tailoring fallback.
- `src/resume_helper.py` - Resume path resolution and form-field fallback filler.
- `src/state_manager.py` - SQLite job state and timestamps.
- `dashboard/main.py` - Cloud dashboard API, sync, external placeholder jobs, credentials endpoints.
- `dashboard/templates/index.html` - Dashboard UI, external job add form, credential forms.
- `README.md` - User-facing operation guide.

## Jobright Apply Architecture

Jobright has two related apply paths.

### Jobright Job Apply

`JobrightScraper.apply(job, auto_submit=False)` handles approved Jobright jobs.

Expected sequence:

1. Open the Jobright job detail page.
2. Extract the company ATS URL using `_extract_external_url()`.
3. Generate/download a tailored resume using `_generate_tailored_resume()`.
4. Resolve a resume path with `resolve_resume_path()`.
5. Open the ATS URL.
6. Upload the resume when a file input is available.
7. Click the ATS apply/start control.
8. Trigger Jobright Autofill or fallback field filling.
9. Stop at final submit unless `auto_submit=True`.

### Jobright External Custom Resume Flow

For external jobs, Jobright's current UI is:

`/jobs/external` -> `CUSTOM RESUME` -> `Improve My Resume for This Job` -> `Full Edit` -> `Select all` -> `Generate My New Resume` -> `Download Resume`.

Relevant helpers:

- `_find_external_jobright_match(page, job)`
- `_add_external_job_to_jobright(page, job)`
- `_download_custom_resume_from_external_list(page, match, job)`
- `_generate_tailored_resume_current_ui(page, job, before)`
- `_run_orion_resume_wizard(page, job, before)`
- `_download_visible_resume(page, job, before)`

Jobright's `Download Resume` button is an Ant Design dropdown trigger in some states. `_download_visible_resume()` and `_download_from_open_dropdown()` must handle both immediate downloads and dropdown menu downloads.

## LinkedIn Apply Architecture

`LinkedInScraper.apply(job, auto_submit=False)` first attempts Jobright tailoring.

Expected sequence:

1. Call `JobrightScraper.tailor_resume_for_external_job(job)`.
2. Use tailored resume path if available.
3. Fall back to `resolve_resume_path(config)`.
4. Open LinkedIn job URL.
5. Support both:
   - legacy modal Easy Apply controls
   - newer full-page `/jobs/view/<id>/apply/` flow with a `Continue` link
6. Fill phone/contact fields from `state/profile.json`.
7. Upload resume if LinkedIn exposes `input[type=file]`.
8. Stop at final submit unless `auto_submit=True`.

If LinkedIn does not expose a LinkedIn-hosted apply flow, `_extract_external_apply_url()` tries to find/click the external apply URL and delegates to `JobrightScraper.apply_external_ats_job()`.

## Resume Handling

The preferred resume path is a newly downloaded tailored resume under:

```text
state/tailored_resumes/
```

Fallback resolution order is implemented in `src/resume_helper.py`:

1. Explicit tailored/downloaded resume path.
2. `LOCAL_RESUME_PATH` or `RESUME_PATH`.
3. `local_resume_path` or `resume_path` in `config.json`.
4. Common folders including `state/resumes`, `~/Documents/Job App`, `~/Downloads`, and `~/Desktop`.

For stable local testing, place a base resume here:

```text
state/resumes/resume.pdf
```

Do not commit real resumes or personal session state.

## Session Prep

Most failed apply runs are session problems, not code problems.

Run:

```bash
python src/main.py prepare-sessions --source jobright
python src/main.py prepare-sessions --source linkedin
```

Known patterns:

- Workday portals often show `Create Account` / `Sign In` before the form is reachable.
- BrassRing often requires manual portal login.
- Microsoft may require account/session review.
- LinkedIn may require a fresh authenticated browser profile or challenge completion.

## Verification Checklist

Use these before claiming the apply workflow is working:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/job-agent-pycache \
  .venv/bin/python -m py_compile \
  src/main.py src/orchestrator.py src/resume_helper.py \
  src/sources/jobright.py src/sources/linkedin.py src/state_manager.py \
  dashboard/main.py

git diff --check
python src/main.py preflight
.venv/bin/python src/main.py ops-check
python src/main.py apply --source jobright --company Pfizer --no-auto-submit
python src/main.py apply --source linkedin --limit 1 --no-auto-submit
```

For real submission testing, use a single known-safe job and only then run:

```bash
python src/main.py apply --job-id <approved_job_id> --auto-submit
```

## Current Known Blockers

- Approved jobs can be skipped by preflight if their ATS needs a refreshed session.
- A Workday job that lands on `Create Account` / `Sign In` is not ready to apply until session prep is completed.
- Jobright custom resume generation can take several minutes and sometimes returns a dropdown under `Download Resume`.
- If no tailored resume is downloaded and no fallback resume exists, upload prompts cannot be satisfied.

## Safety Rules For Developers

- Never commit `.env`, real credentials, session profiles, downloaded resumes, or `state/jobs.db`.
- Do not remove final-submit confirmation behavior unless the CLI flag is explicitly `--auto-submit`.
- Prefer targeted runs with `--job-id`, `--company`, `--source`, and `--limit`.
- Keep browser automation headed/non-headless for apply workflows so the user can see what is happening.
- Treat `applied` status as authoritative: only set it after an application is actually submitted.
