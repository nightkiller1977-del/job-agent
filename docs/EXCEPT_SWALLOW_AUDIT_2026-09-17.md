# Exception-swallow audit of `src/sources/` — 2026-09-17

Audited against `main` @ `9888add` (the PR #128 merge). All line numbers reference that commit.

## Why

The source adapters contain 263 `except` handlers, most of them `except Exception` with
`pass`, a silent fallback value, or a console-only print. Most are deliberate best-effort
scraping, but at this density **silent failure is the default**: a broken scraper, an
invalid session, or a lost submission is frequently indistinguishable from a quiet job
market or a normal page variant.

PR #128 established the remediation model: a failure becomes a **distinct, persisted,
queryable outcome** (`score=None` + `SCORING_FAILED` flag + `scoring_failed` action)
instead of a silent fallback value. This audit classifies every handler by whether it
needs that treatment.

## Classification rubric

| Cat | Meaning |
|-----|---------|
| **A** | Genuinely-fine best-effort swallow: optional-UI probes, cosmetic enrichment, cleanup/teardown; absence is a normal case and cannot change a pipeline decision. |
| **B** | Should log at debug/warning with a structured token: the swallow hides operational signal (degraded data, failed click/parse with a sensible fallback) but doesn't corrupt a decision. |
| **C** | Masks a decision-affecting failure; deserves a distinct persisted outcome (SCORING_FAILED-style): job silently dropped, submission misreported, session validity misjudged, pagination silently truncated, "unknown" converted to "success"/"false". |
| **N** | Not a problem: re-raises, converts to a typed exception, or already records a correct distinct outcome. |

## Counts

| File | Handlers | A | B | C | N |
|------|---------:|--:|--:|--:|--:|
| `src/sources/jobright.py` | 119 | 63 | 22 | 9 | 25 |
| `src/sources/linkedin.py` | 53 | 26 | 12 | 6 | 9 |
| `src/sources/usajobs.py` | 30 | 13 | 7 | 5 | 5 |
| `src/sources/base.py` | 29 | 9 | 6 | 4 | 10 |
| `src/sources/indeed.py` | 15 | 6 | 1 | 5 | 3 |
| `src/sources/themuse.py` | 7 | 1 | 0 | 3 | 3 |
| `src/sources/builtin.py` | 7 | 0 | 4 | 1 | 2 |
| `src/sources/jobspy_scraper.py` | 2 | 0 | 0 | 2 | 0 |
| `src/sources/jobspy_bridge.py` | 1 | 0 | 0 | 0 | 1 |
| **Total** | **263** | **118** | **52** | **35** | **58** |

(Raw grep counts are slightly higher than handler counts because a few hits are comments
or identifiers, e.g. `_classify_external_ats_exception` call sites in jobright.py.)

**45% of handlers are fine as-is (A), 22% already correct (N). The fix surface is the
52 B-swallows needing structured logging and especially the 35 C-swallows that corrupt
decisions.**

## The five systemic C patterns

Most of the 35 C findings are instances of five recurring patterns. Fixing the pattern
fixes the instances.

### 1. Discovery truncation persisted as "fewer jobs" (11 findings)

A scrape/pagination failure returns a partial or empty list that is persisted identically
to a complete run. A broken scraper looks like a quiet market — this is how a source can
die silently for weeks.

- `jobright.py:1259` — `scrape()` swallows any mid-scrape crash, returns partial `jobs`.
- `jobright.py:1253` — entire external-jobs page category silently dropped.
- `linkedin.py:111` — crash on query 3 of 10 returns partial list as complete.
- `linkedin.py:184` — saved-jobs import (user intent, force-approved) truncated silently.
- `linkedin.py:469`, `linkedin.py:609` — extraction/parse failures yield `[]`/dropped cards.
- `usajobs.py:101`, `usajobs.py:646` — same pattern (outer scrape + per-card drop).
- `indeed.py:109`, `indeed.py:186` — outer scrape swallow + `except Exception: break` in
  pagination, byte-identical to "last page reached".
- `themuse.py:95/102` — 429 or network error on page 0 → empty set persisted as "no jobs".
- `builtin.py:237` — `_safe_get` returns `None` → listing loop breaks, run looks complete.
- `jobspy_scraper.py:44/55` — subprocess spawn failure or unparseable bridge output →
  `return []` for three sub-sources at once.

**Fix shape:** a per-run, per-source persisted `SCRAPE_FAILED` / `scrape_partial` outcome
(source, phase, error, jobs-ingested-before-failure) — the direct analog of SCORING_FAILED
at ingest granularity.

### 2. Ambiguous submission recorded as a definite outcome (3 findings)

AGENTS.md: "Do not claim a submission succeeded without durable evidence. Reconcile an
ambiguous outcome before retrying."

- `jobright.py:3825` — if the JS click fails, the fallback runs via `_safe_evaluate(...,
  default=None)` which **cannot raise** for non-fatal errors; the code then unconditionally
  records `_apply_analytics = {"submitted": True, ...}` and returns True. A submission with
  zero click evidence is reported as success.
- `jobright.py:3760` — the empty-form guard fails **open**: `except Exception:
  has_filled_fields = True  # assume filled if we can't check`. An evaluate failure converts
  "unknown" into "safe to submit".
- `usajobs.py:784` — an error after `btn.click()` (which may have dispatched the POST)
  keeps `submitted=False` → orchestrator retry → duplicate-application risk.
- (`linkedin.py:1009` is the same post-click ambiguity: exception after submit-click is
  persisted as generic `linkedin_error`, inviting retry.)

**Fix shape:** distinct `SUBMISSION_AMBIGUOUS` outcome that blocks auto-retry and forces
reconciliation; fail the empty-form guard **closed**.

### 3. Session validity misjudged from a swallowed error (4 findings)

- `linkedin.py:1362` — `_needs_login`: `except Exception: return False` = any page error
  becomes "logged in"; scrape proceeds against an authwall (0 jobs) and the apply login
  gate is bypassed.
- `jobright.py:2241` — `_looks_like_login_wall`: same fail-open; `prepare_session` then
  declares the session authenticated.
- `indeed.py:128` — login-wall check error → "not a login wall" → session reported fresh.
- `usajobs.py:124` — `_is_logged_in`: fail-**closed** variant; a transient error burns a
  ReauthManager cycle / fails non-interactive session prep for a valid session.

**Fix shape:** tri-state (or raise-on-browser-death) session checks with a distinct
`SESSION_CHECK_FAILED` signal instead of a boolean guess in either direction.

### 4. Transient failure persisted under a permanent-looking token (2 findings + cluster)

- `indeed.py:383` — Playwright error reading the job page leaves `ext_url=""`, which is
  then persisted as `indeed_easy_apply_or_no_ats` — a structural verdict that suppresses
  retries forever.
- `themuse.py:222` — same: browser error persisted as `themuse_no_ats_url`.

**Fix shape:** split tokens — `*_resolve_error` (transient, retryable) vs `*_no_ats_url`
(structural) — exactly PR #128's "failure is not a value" distinction.

### 5. Silent degradation of what gets submitted to employers (6 findings)

- `base.py:527` / `base.py:536` — `_safe_click`/`_safe_fill` swallow **browser death** and
  return False = "element absent", unlike their siblings `_safe_evaluate`/`_safe_goto`
  (549/564) which re-raise closed/detached/crashed. Every adapter inherits this: a crashed
  browser persists as `*_not_found` outcomes.
- `base.py:639` — resume-upload label lookup failure blanks the label, making the field
  look "generic" — defeating the guard (see comment at 680-684) that prevents uploading the
  resume into a labeled non-resume field ("Portfolio", "Transcript").
- `base.py:702` — document-upload loop failure is printed and ignored; callers discard the
  return value, so an application can be submitted resume-less and recorded as success.
- `usajobs.py:936` — whole questionnaire swallowed; auto-submit then files with
  missing/wrong eligibility answers, status "submitted".
- `jobright.py:455/1862/3366/3488` — the Claude ATS scoring/tailoring cluster: any failure
  (model, JSON parse, JD extraction) dim-logs and persists `atsScore: 0` + base resume with
  no distinct outcome — the in-file twin of the fallback-score pattern PR #128 removed.

**Fix shape:** uniform browser-death re-raise guard extracted into one helper (it already
exists in ~8 places, inconsistently); `UPLOAD_FAILED` / `TAILORING_FAILED` /
`QUESTIONNAIRE_FAILED` persisted outcomes that gate auto-submit.

## Representative B examples (structured-logging tier)

- `linkedin.py:1430/1473/1525` — application-form question fills fail silently per-field;
  no record of which question failed.
- `linkedin.py:578` — Easy-Apply badge probe failure can flip `has_easy_apply=False`,
  changing apply routing with no trace.
- `jobright.py:1415/1430` — pagination click errors end pagination silently (borderline C).
- `jobright.py:3595` — Claude PDF generation failure silently substitutes the base resume.
- `usajobs.py:168/181` — login email/password steps fail fully silently; final verify
  catches the outcome but which step failed is lost.
- `base.py:198/271/394/511` — orphan-process kill / lock-release failures are silent; a
  stranded lock can stall every future launch with the root cause hidden.
- `builtin.py:108/120` — markup drift makes every job vanish via silent JSON-decode skips.

## Infrastructure gaps that shape the fix

1. **No stdlib logging in any adapter except base.py.** jobright.py, linkedin.py,
   usajobs.py, indeed.py, themuse.py, builtin.py, jobspy_scraper.py use `rich.Console`
   prints exclusively — unstructured, unqueryable, and invisible under background/launchd
   runs. B-tier fixes should introduce a module logger + structured tokens, not more
   color prints.
2. **The apply path has an outcome channel; the scrape path has none.**
   `_set_apply_outcome(status, detail)` (base.py) is used well for apply outcomes
   (`linkedin_step_blocked`, `themuse_external_apply_error`, `usajobs_error`, …), but
   ingest/discovery has no equivalent — which is where most pattern-1 findings live.
3. **The browser-death re-raise guard** (`"closed"/"detached"/"crashed"` substring check)
   exists in ~8 call sites but is missing from `_safe_click`, `_safe_fill`,
   `_is_logged_in`-style checks, and several probe loops. Extract once, apply uniformly.
4. **Good existing patterns to copy:** `_safe_evaluate`/`_safe_goto` (base.py:549/564),
   `PDFTextLayerError`/`KeywordCoverageError` propagation (jobright.py:509-537),
   `jobspy_bridge.py:41` (machine-readable stderr JSON + exit code), themuse/builtin
   `*_external_apply_error` outcome tokens.
5. **Dead code:** `jobright.py` `_parse_card` (~1500) and `_extract_from_links` (~1575)
   appear uncalled (live extraction uses `_js_extract`); delete rather than instrument.

## Proposed remediation split (per-adapter Jira children under ACES-18)

Ordered by risk:

1. **base.py** — uniform browser-death guard; `_safe_click`/`_safe_fill` re-raise;
   upload-label fail-closed; `UPLOAD_FAILED` outcome. (Fixes propagate to all adapters.)
2. **jobright.py** — submission-evidence fixes (3825, 3760), session check (2241),
   `SCRAPE_FAILED` outcome (1259/1253), `TAILORING_FAILED` cluster (455/1862/3366/3488),
   B-tier logging, dead-code removal.
3. **linkedin.py** — `_needs_login` tri-state (1362), post-click ambiguity (1009),
   scrape/saved-jobs partial outcomes (111/184), extraction-failure signal (469/609),
   form-fill logging tier.
4. **usajobs.py** — post-click ambiguity (784), questionnaire gate (936), parse-drop
   signal (646), `_is_logged_in` tri-state (124), scrape outcome (101).
5. **indeed.py** — resolve-error vs no-ATS split (383), pagination truncation (186),
   hydration marker (319), session check (128), scrape outcome (109).
6. **themuse.py + builtin.py + jobspy_scraper.py** — `SCRAPE_FAILED` outcomes for
   pagination/subprocess failures (themuse 95/102, builtin 237, jobspy 44/55);
   themuse resolve-error split (222); builtin JSON-drift logging (108/120).

A shared prerequisite for items 2-6 is the **per-source scrape outcome record**
(pattern 1); it should land with the base.py ticket or as its own small schema change
first.

## Method

Full read of all nine files (every handler classified, not sampled); classifications
produced per-file and the headline C findings verified line-by-line against
`main @ 9888add`. Handler counts exclude grep false-positives (comments/identifiers).
