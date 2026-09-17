# Exception-swallow audit of `src/sources/` top-level adapters — 2026-09-17

Audited against `main` @ `9888add` (the PR #128 merge). All line numbers reference that commit.

## Scope

This audit covers the **nine top-level legacy adapter modules** in `src/sources/`
(`jobright.py`, `linkedin.py`, `usajobs.py`, `base.py`, `indeed.py`, `themuse.py`,
`builtin.py`, `jobspy_scraper.py`, `jobspy_bridge.py`) — 265 `except` handlers in all.

It deliberately **excludes the `src/sources/adapters/` tree** (88 further handlers across
the newer ATS adapter framework). That tree was built with the outcome-contract
discipline this audit is pushing toward (`AtsApplyResult`, the idempotency ledger,
typed outcome codes), so its risk profile is different; it gets its own follow-up
audit (tracked as a separate child ticket under ACES-18) rather than being lumped in
with the legacy modules.

## Why

The nine audited modules contain 265 `except` handlers, most of them
`except Exception` with `pass`, a silent fallback value, or a console-only print. Most
are deliberate best-effort scraping, but at this density **silent failure is the
default**: a broken scraper, an invalid session, or a lost submission is frequently
indistinguishable from a quiet job market or a normal page variant.

PR #128 established the remediation model: a failure becomes a **distinct, persisted,
queryable outcome** (`score=None` + `SCORING_FAILED` flag + `scoring_failed` action)
instead of a silent fallback value. This audit classifies every handler by whether it
needs that treatment.

## Classification rubric

| Cat | Meaning |
|-----|---------|
| **A** | Genuinely-fine best-effort swallow: optional-UI probes, cosmetic enrichment, cleanup/teardown; absence is a normal case and cannot change a pipeline decision. |
| **B** | Should log at debug/warning with a structured token: the swallow hides operational signal (degraded data, failed click/parse with a sensible fallback) but doesn't corrupt a decision. |
| **C** | Masks a decision-affecting failure; deserves a distinct persisted outcome (SCORING_FAILED-style): job silently dropped, submission misreported, session validity misjudged, pagination silently truncated, employer-facing payload silently degraded, "unknown" converted to "success"/"false". |
| **N** | Not a problem: re-raises, converts to a typed exception, or already records a correct distinct outcome. |

## Counts

Handler counts are AST-verified (`ast.ExceptHandler` per file); the complete
per-handler classification is in the appendix.

| File | Handlers | A | B | C | N |
|------|---------:|--:|--:|--:|--:|
| `src/sources/jobright.py` | 121 | 64 | 17 | 12 | 28 |
| `src/sources/linkedin.py` | 53 | 26 | 11 | 7 | 9 |
| `src/sources/usajobs.py` | 30 | 13 | 7 | 5 | 5 |
| `src/sources/base.py` | 29 | 9 | 6 | 4 | 10 |
| `src/sources/indeed.py` | 15 | 6 | 1 | 5 | 3 |
| `src/sources/themuse.py` | 7 | 1 | 0 | 3 | 3 |
| `src/sources/builtin.py` | 7 | 0 | 2 | 3 | 2 |
| `src/sources/jobspy_scraper.py` | 2 | 0 | 0 | 2 | 0 |
| `src/sources/jobspy_bridge.py` | 1 | 0 | 0 | 0 | 1 |
| **Total** | **265** | **119** | **44** | **41** | **61** |

**45% of handlers are fine as-is (A), 23% already correct (N). The fix surface is
the 44 B-swallows needing structured logging and especially the 41 C-swallows that
corrupt decisions.**

## The five systemic C patterns

Most of the 41 C findings are instances of five recurring patterns. Fixing the pattern
fixes the instances.

### 1. Discovery truncation persisted as "fewer jobs" (15 findings)

A scrape/pagination/parse failure returns a partial or empty list that is persisted
identically to a complete run. A broken scraper looks like a quiet market — this is how
a source can die silently for weeks.

- `jobright.py:1259` — `scrape()` swallows any mid-scrape crash, returns partial `jobs`.
- `jobright.py:1253` — entire external-jobs page category silently dropped.
- `jobright.py:1415` / `jobright.py:1430` — pagination click errors end pagination
  exactly like end-of-list.
- `linkedin.py:111` — crash on query 3 of 10 returns partial list as complete.
- `linkedin.py:184` — saved-jobs import (user intent, force-approved) truncated silently.
- `linkedin.py:469`, `linkedin.py:609` — extraction/parse failures yield `[]`/dropped cards.
- `usajobs.py:101`, `usajobs.py:646` — same pattern (outer scrape + per-card drop).
- `indeed.py:109`, `indeed.py:186` — outer scrape swallow + `except Exception: break` in
  pagination, byte-identical to "last page reached".
- `themuse.py:95/102` — 429 or network error on page 0 → empty set persisted as "no jobs".
- `builtin.py:237` — `_safe_get` returns `None` → listing loop breaks, run looks complete.
- `builtin.py:108/120` — silent JSON-decode skips: markup drift can make **every** job
  vanish with zero signal.
- `jobspy_scraper.py:44/55` — subprocess spawn failure or unparseable bridge output →
  `return []` for three sub-sources at once.

**Fix shape:** a per-run, per-source persisted `SCRAPE_FAILED` / `scrape_partial` outcome
(source, phase, error, jobs-ingested-before-failure) — the direct analog of SCORING_FAILED
at ingest granularity. No such channel exists today; it is the shared prerequisite.

### 2. Ambiguous submission recorded as a definite outcome (4 findings)

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
- `linkedin.py:1009` — same post-click ambiguity: exception after submit-click is
  persisted as generic `linkedin_error`, inviting retry.

**Fix shape:** the contract for this state **already exists** — route these legacy paths
through it rather than inventing a new token: `ApplyOutcomeCode.SUBMISSION_UNVERIFIED`
(`src/apply_outcome.py:13`), constructed via `AtsApplyResult.unverified()`
(`src/sources/adapters/context.py:62-69`), with the idempotency ledger
(`src/sources/adapters/idempotency.py`) blocking blind resubmission pending
reconciliation. Post-click exceptions in these four sites should persist
`submission_unverified`, and the empty-form guard should fail **closed**.

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

### 4. Transient failure persisted under a permanent-looking token (2 findings)

- `indeed.py:383` — Playwright error reading the job page leaves `ext_url=""`, which is
  then persisted as `indeed_easy_apply_or_no_ats` — a structural verdict that suppresses
  retries forever.
- `themuse.py:222` — same: browser error persisted as `themuse_no_ats_url`.

**Fix shape:** split tokens — `*_resolve_error` (transient, retryable) vs `*_no_ats_url`
(structural) — exactly PR #128's "failure is not a value" distinction. Register the
transient token in `blocker_classifier.py` as `TRANSIENT`.

### 5. Silent degradation of the employer-facing payload (9 findings)

- `base.py:527` / `base.py:536` — `_safe_click`/`_safe_fill` swallow **browser death** and
  return False = "element absent", unlike their siblings `_safe_evaluate`/`_safe_goto`
  (549/564) which re-raise closed/detached/crashed. Every adapter inherits this: a crashed
  browser persists as `*_not_found` outcomes.
- `base.py:639` — resume-upload label lookup failure blanks the label, making the field
  look "generic" — defeating the guard (see comment at 680-684) that prevents uploading the
  resume into a labeled non-resume field ("Portfolio", "Transcript").
- `base.py:702` — document-upload loop failure is printed and ignored; callers discard the
  return value, so an application can be submitted resume-less and recorded as success.
  Use the **existing** `ApplyOutcomeCode.RESUME_UPLOAD_FAILED` (`src/apply_outcome.py:20`)
  and gate auto-submit on it.
- `usajobs.py:936` — whole questionnaire swallowed; auto-submit then files with
  missing/wrong eligibility answers, status "submitted".
- `jobright.py:455/1862/3366/3488` — the Claude ATS scoring/tailoring cluster: any failure
  (model, JSON parse, JD extraction) dim-logs and persists `atsScore: 0` + base resume with
  no distinct outcome — the in-file twin of the fallback-score pattern PR #128 removed.
  The **existing** `resume_tailor_error` status (`src/resume_tailor.py:57`, classified
  `TRANSIENT` in `src/blocker_classifier.py:81`) is the right vehicle where tailoring is
  what failed; only the ATS-scoring-skipped case needs a new token.
- `jobright.py:3595` — Claude PDF generation failure silently substitutes the base
  resume — changes what the employer receives; the existing `resume_render_failed`
  status (`src/resume_tailor.py:58`) fits.
- `linkedin.py:578` — Easy-Apply badge probe failure can flip `has_easy_apply=False`,
  silently rerouting the job to the external-apply path.

**Fix shape:** uniform browser-death re-raise guard extracted into one helper (it already
exists in ~8 places, inconsistently); reuse `resume_upload_failed` /
`resume_tailor_error` / `resume_render_failed`; new tokens only for questionnaire
failure and ATS-scoring-skipped, mapped in `blocker_classifier.py`.

## Representative B examples (structured-logging tier)

- `linkedin.py:1430/1473/1525` — application-form question fills fail silently per-field;
  no record of which question failed.
- `jobright.py:2311` — form-detect evaluate error → False; fail-closed, but transient
  errors are mislabeled `form_not_detected`.
- `jobright.py:3379` — pypdf resume-text extraction fails silently; ATS scoring quietly
  runs on profile.json instead of the actual resume.
- `usajobs.py:168/181` — login email/password steps fail fully silently; final verify
  catches the outcome but which step failed is lost.
- `base.py:198/271/394/511` — orphan-process kill / lock-release failures are silent; a
  stranded lock can stall every future launch with the root cause hidden.
- `builtin.py:311/337` — ATS URL re-fetch/extra_json fallbacks are reasonable but the
  degradation is only console-visible.

## Infrastructure gaps that shape the fix

1. **No stdlib logging in any audited module except base.py.** The others use
   `rich.Console` prints exclusively. Console output **is** captured under launchd (both
   plists persist stdout/stderr to `state/discover.log`/`state/apply.log` and `.err` —
   `launchd/com.jobagent.discover.plist:33-36`, `launchd/com.jobagent.apply.plist:33-36`),
   but it is unstructured and has no levels or tokens, so it cannot be queried, filtered,
   or alerted on. B-tier fixes should introduce a module logger + structured tokens, not
   more color prints.
2. **The apply path has an outcome channel; the scrape path has none.**
   `_set_apply_outcome(status, detail)` (base.py) is used well for apply outcomes
   (`linkedin_step_blocked`, `themuse_external_apply_error`, `usajobs_error`, …), but
   ingest/discovery has no equivalent — which is where most pattern-1 findings live.
3. **The browser-death re-raise guard** (`"closed"/"detached"/"crashed"` substring check)
   exists in ~8 call sites but is missing from `_safe_click`, `_safe_fill`,
   `_is_logged_in`-style checks, and several probe loops. Extract once, apply uniformly.
4. **Reuse existing outcome contracts before minting tokens.** Already canonical:
   `submission_unverified` (+ `AtsApplyResult.unverified()` + idempotency ledger),
   `resume_upload_failed`, `resume_tailor_error`, `resume_render_failed`,
   `form_empty_not_submitted`, `submission_cancelled`. New tokens should be limited to:
   per-source scrape outcomes (pattern 1), `SESSION_CHECK_FAILED` (pattern 3),
   `*_resolve_error` (pattern 4), questionnaire failure and ATS-scoring-skipped
   (pattern 5) — each registered in `blocker_classifier.py`.
5. **Good existing patterns to copy:** `_safe_evaluate`/`_safe_goto` (base.py:549/564),
   `PDFTextLayerError`/`KeywordCoverageError` propagation (jobright.py:509-537),
   `jobspy_bridge.py:41` (machine-readable stderr JSON + exit code), themuse/builtin
   `*_external_apply_error` outcome tokens.
6. **Dead code:** `jobright.py` `_parse_card` (~1500) and `_extract_from_links` (~1575)
   appear uncalled (live extraction uses `_js_extract`); delete rather than instrument
   (their two handlers are counted as B above but deletion supersedes logging).

## Proposed remediation split (per-adapter Jira children under ACES-18)

Ordered by risk:

1. **base.py** — uniform browser-death guard; `_safe_click`/`_safe_fill` re-raise;
   upload-label fail-closed; wire `resume_upload_failed` to gate auto-submit.
   (Fixes propagate to all adapters.)
2. **jobright.py** — submission-evidence fixes (3825, 3760 → `submission_unverified` /
   fail-closed guard), session check (2241), `SCRAPE_FAILED` outcome (1259/1253),
   pagination truncation (1415/1430), tailoring cluster (455/1862/3366/3488/3595 →
   `resume_tailor_error`/`resume_render_failed` + ATS-scoring-skipped token),
   B-tier logging, dead-code removal.
3. **linkedin.py** — `_needs_login` tri-state (1362), post-click ambiguity (1009 →
   `submission_unverified`), scrape/saved-jobs partial outcomes (111/184),
   extraction-failure signal (469/609), Easy-Apply badge routing (578),
   form-fill logging tier.
4. **usajobs.py** — post-click ambiguity (784 → `submission_unverified`), questionnaire
   gate (936), parse-drop signal (646), `_is_logged_in` tri-state (124), scrape
   outcome (101).
5. **indeed.py** — resolve-error vs no-ATS split (383), pagination truncation (186),
   hydration marker (319), session check (128), scrape outcome (109).
6. **themuse.py + builtin.py + jobspy_scraper.py** — `SCRAPE_FAILED` outcomes for
   pagination/parse/subprocess failures (themuse 95/102, builtin 108/120/237,
   jobspy 44/55); themuse resolve-error split (222).
7. **Follow-up audit: `src/sources/adapters/` tree** — the 88 handlers in the newer ATS
   adapter framework, audited against the same rubric (separate ticket; different risk
   profile since the outcome contract already exists there).

A shared prerequisite for items 2-6 is the **per-source scrape outcome record**
(pattern 1); it should land with the base.py ticket or as its own small schema change
first.

## Method

Full read of all nine modules (every handler classified, not sampled); handler
inventory cross-checked against the Python AST (`ast.ExceptHandler`), so the appendix
is provably complete for these files. Headline C findings verified line-by-line against
`main @ 9888add`. Six handlers were reclassified B→C during PR review
(jobright 1415/1430/3595, linkedin 578, builtin 108/120) because each affects a
pipeline decision or the employer-facing payload.

## Appendix — complete per-handler classification

Format: `line | except clause | disposition | category | rationale`.


### `src/sources/jobright.py` (121 handlers: A=64 B=17 C=12 N=28)

```
  113 | except Exception: | return-early | B | page-ping failure silently skips Jobright fallback search; tailoring silently degrades
  155 | except Exception as exc: | log+continue | N | logs cause; returns "" and apply falls back to configured resume
  211 | except Exception: | return-early | A | page closed during poll; stops polling, "no card" already logged after
  224 | except Exception: | return-early | A | goto retry during poll; page closed, gives up polling gracefully
  230 | except Exception as exc: | log+continue | N | logs cause; add-external-job is best-effort enrichment, returns None
  265 | except Exception as exc: | log+continue | N | logs cause; drawer download fails, later Orion path still tried
  359 | except Exception: | retry | A | probing Teamtailor form selectors in sequence; absence expected
  452 | except (ATSReadabilityError, ModelCascadeError): | reraise | N | explicit re-raise to outer handlers
  455 | except Exception as _ce: | log+continue | C | ATS scoring/tailoring block failure dim-logged; atsScore persists as 0, base resume submitted
  509 | except KeywordCoverageError as exc: | reraise | N | sets distinct status + metrics, logs, re-raises
  515 | except PDFTextLayerError as exc: | reraise | N | sets distinct status, re-raises to pause apply loop
  527 | except ModelCascadeError as exc: | return-early | N | distinct persisted outcome model_timeout
  530 | except PlaywrightTimeoutError as exc: | return-early | N | distinct persisted outcome browser_timeout
  533 | except PlaywrightError as exc: | return-early | N | classified, distinct persisted outcome
  537 | except Exception as exc: | return-early | N | classified, distinct persisted outcome
  597 | except Exception: | return-early | B | tool-card timeout logged vaguely; cannot distinguish absence from page crash
  612 | except Exception: | retry | A | selector probe loop for Improve button
  643 | except Exception: | retry | A | selector probe loop for Generate button
  675 | except Exception as exc: | log+continue | N | download failure logged with cause; "" fallback handled upstream
  715 | except Exception: | retry | A | textarea selector probe loop
  721 | except Exception: | fallback-value | A | fill() fails, recovers via keyboard.type
  741 | except Exception as exc: | log+continue | N | whole Orion-UI path logged with cause; base resume fallback
  848 | except Exception: | retry | A | evaluate retry loop inside timed click helper
  874 | except Exception: | retry | A | wizard entry-button probe loop
  896 | except Exception: | retry | A | Step-2 render selector probe loop
  973 | except Exception: | retry | A | Generate-button probe loop
 1021 | except Exception: | retry | A | Download-button probe loop
 1036 | except Exception: | fallback-value | N | download timeout recovers via dropdown + second attempt paths
 1040 | except Exception: | pass | A | best-effort click to open dropdown before fallback
 1053 | except Exception as exc: | log+continue | N | final attempt logged; falls to _latest_tailored_resume check
 1070 | except Exception as exc: | log+continue | N | dropdown download failure logged with cause
 1084 | except Exception: | retry | A | dropdown-visibility poll
 1102 | except Exception: | fallback-value | A | dropdown count -> 0; absence is normal
 1112 | except Exception: | pass | A | item inner_text optional; empty label acceptable
 1115 | except Exception: | retry | A | item visibility probe; skip item
 1237 | except Exception: | log+continue | N | card-wait timeout logged; extraction still attempted
 1253 | except Exception as exc: | log+continue | C | external-jobs page scrape drop; jobs silently missing from ingest, no persisted signal
 1257 | except AuthFailedError: | reraise | N | auth failure propagates
 1259 | except Exception as exc: | log+continue | C | scrape() swallows all errors, returns partial/empty list — looks like "no jobs"
 1286 | except Exception as exc: | retry | A | probe loop; re-raises fatal page-death errors
 1313 | except Exception as exc: | retry | A | email-field probe loop; fatal errors re-raised
 1351 | except Exception as exc: | retry | A | submit-button probe loop; fatal errors re-raised
 1379 | except Exception as exc: | log+continue | N | logs, returns False; caller raises AuthFailedError non-interactively
 1415 | except Exception: | retry | C | pagination click errors silent; failed click ends pagination like end-of-list (reclassified B->C: discovery truncation)
 1430 | except Exception: | retry | C | second pagination probe, same silent truncation (reclassified B->C: discovery truncation)
 1571 | except Exception as exc: | log+continue | B | per-card drop dim-logged; _parse_card appears uncalled in this file (dead code)
 1602 | except Exception as exc: | log+continue | B | link-fallback partial results dim-logged; appears uncalled (dead code)
 1713 | except Exception: | pass | A | urlparse validation is defensive; failure preserves prior behavior
 1746 | except Exception as _e: | log+continue | N | new-tab expectation failure logged; direct navigation fallback correct
 1768 | except Exception: | pass | A | dismissing "didn't apply" popup is cosmetic
 1796 | except Exception: | fallback-value | A | portal_url read; "" handled by caller branch
 1811 | except Exception: | fallback-value | A | current_portal read; "" handled
 1862 | except Exception as _ce: | log+continue | C | same ATS scoring block swallow as 455, Jobright-assisted path
 1891 | except Exception: | fallback-value | A | portal_url read for diagnostics
 1918 | except JobExpiredError: | reraise | N | propagates expiry
 1920 | except Exception as exc: | log+continue | N | logs traceback, notify_error, sets last_apply_status="error" — persisted outcome
 2035 | except Exception: | fallback-value | A | on_login=False; interactive human prompt still follows
 2159 | except Exception: | fallback-value | A | diagnostics snapshot -> []
 2208 | except Exception: | fallback-value | A | visibility helper -> False; downstream form-check still gates
 2214 | except Exception: | fallback-value | A | family -> "generic"; only affects outcome token naming
 2241 | except Exception: | fallback-value | C | login-wall check fails -> False -> prepare_session declares session authenticated/ready
 2311 | except Exception: | fallback-value | B | form-detect error -> False; fail-closed but transient errors mislabeled form_not_detected
 2370 | except Exception: | pass | A | popup ATS-URL capture is an optimization; extraction fallback exists
 2388 | except Exception: | retry | A | popup close-button probe loop
 2401 | except Exception: | pass | A | autofill-enable popup optional
 2457 | except Exception: | fallback-value | B | click error -> "no URL"; downstream missing_ats_url persisted but true cause hidden
 2467 | except Exception: | retry | A | new-page URL probe; closed pages expected
 2472 | except Exception: | pass | A | page.url probe fallback
 2480 | except Exception: | return-early | A | page invalid -> False; downstream checks fail-closed anyway
 2501 | except Exception: | fallback-value | B | login-detect evaluate error -> "no login needed"; auto-login silently skipped
 2539 | except Exception: | pass | A | Sign-In link probe optional
 2560 | except Exception: | retry | A | email selector probe loop
 2579 | except Exception: | retry | A | Next/Continue probe loop
 2590 | except Exception: | return-early | B | password-not-found logged, but login abort carries no status; SSO guess unverified
 2607 | except Exception: | retry | A | final Sign-In button probe loop
 2622 | except Exception as e: | log+continue | N | portal login failure logged; downstream form checks fail-closed
 2636 | except Exception: | retry | A | alt-login email probe loop
 2645 | except Exception: | retry | A | alt-login Next probe loop
 2653 | except Exception: | return-early | B | alt-login aborts fully silently (no log at all)
 2662 | except Exception: | retry | A | alt-login submit probe loop
 2665 | except Exception as e: | log+continue | N | alt login failure logged
 2697 | except Exception: | pass | A | LinkedIn-field selector probes
 2723 | except Exception: | pass | B | work-auth radio primary path silent; unanswered required question can block/mis-fill submit
 2732 | except Exception: | pass | A | work-auth fallback selector probe
 2751 | except Exception: | pass | B | sponsorship radio primary path silent; same risk as 2723
 2760 | except Exception: | pass | A | sponsorship fallback probe
 2780 | except Exception: | pass | A | optional phone/city/zip fills
 2820 | except Exception: | pass | B | Workday DOM-detection failure -> treated as generic ATS -> wrong apply strategy, silent
 2830 | except Exception as e: | log+continue | N | Workday nav failure logged; chooser handling still attempted
 2841 | except Exception: | fallback-value | A | page.url read -> ""
 2918 | except Exception: | retry | A | apply-selector probe loop (form-reached check gates success)
 2940 | except Exception: | pass | A | SmartRecruiters JS-click optional; generic fallback follows
 2981 | except Exception: | retry | A | Microsoft explicit-apply probe inside retry loop
 3074 | except Exception: | fallback-value | A | url read -> not-login; secondary body-text check follows
 3087 | except Exception: | fallback-value | A | body-text login check -> False; session_gate recheck follows later
 3113 | except (EOFError, KeyboardInterrupt): | return-early | N | notify_error sent; session-expired flag already set
 3127 | except Exception: | fallback-value | A | still_login=True conservative default
 3156 | except Exception: | pass | A | networkidle wait is timing best-effort
 3182 | except Exception: | fallback-value | B | session-gate evaluate error -> False; expired-session flag missed, wizard proceeds blind
 3213 | except Exception: | pass | A | review-URL early-exit probe
 3229 | except Exception: | retry | A | Next-button selector probe loop
 3239 | except Exception: | fallback-value | A | button text cosmetic
 3250 | except Exception as e: | log+continue | B | Next-click failure logged then break — truncated wizard printed as "navigation complete"
 3287 | except Exception: | retry | A | autofill trigger selector probes
 3314 | except Exception: | pass | A | shadow-DOM walk optional
 3334 | except Exception: | pass | A | auto-fill wait; False return triggers logged fallback in caller
 3362 | except Exception: | retry | A | JD selector probes
 3366 | except Exception: | fallback-value | C | JD body-text fallback -> ""; whole ATS scoring/tailoring silently skipped, no record
 3379 | except Exception: | pass | B | pypdf resume-text extraction fails silently -> scoring runs on profile.json instead
 3459 | except ModelCascadeError: | fallback-value | B | reviewer pass skipped deliberately but silently; ungrounded draft used with no token
 3486 | except ModelCascadeError: | reraise | N | cascade exhaustion propagates to model_timeout outcome
 3488 | except Exception as _e: | log+continue | C | JSON-parse/scoring failure dim-logged -> {}; SCORING_FAILED analog, no persisted outcome
 3595 | except Exception as _e: | log+continue | C | Claude PDF generation fails dim-logged; base resume silently substituted (reclassified B->C: changes employer-facing payload)
 3652 | except Exception as _e: | log+continue | A | pre-submit checklist is advisory console output only
 3705 | except Exception: | pass | B | vendor submit-selector import fails silently -> degraded submit detection, hidden regression
 3715 | except Exception: | retry | A | submit-selector probe loop
 3720 | except Exception: | fallback-value | A | portal_url -> "(unknown)" for display
 3760 | except Exception: | fallback-value | C | empty-form guard bypassed on error ("assume filled") -> can submit blank form as success
 3812 | except (EOFError, KeyboardInterrupt): | fallback-value | N | confirm="n" -> distinct submission_cancelled outcome
 3825 | except Exception: | fallback-value | C | JS click fails -> _safe_evaluate fallback can't raise -> still reports "submitted", returns True
 3867 | except (EOFError, KeyboardInterrupt): | return-early | N | conservative False on interrupted manual confirm
```

### `src/sources/linkedin.py` (53 handlers: A=26 B=11 C=7 N=9)

```
  109 | except AuthFailedError | reraise | N | auth failure propagates correctly to caller
  111 | except Exception as exc | log+continue | C | scrape() returns partial list; truncation looks like "fewer jobs exist"
  164 | except Exception as exc | pass | A | selector probe; fatal page errors re-raised, absence normal
  184 | except Exception as exc | log+continue | C | saved jobs (auto-approved intent) partially imported; looks complete
  248 | except Exception as exc | log+continue | B | hydration fails; saved job persists half-populated (title/company/description degraded)
  259 | except Exception as exc | pass | A | optional-text probe; fatal errors re-raised
  310 | except Exception as exc | pass | A | submit-button probe; Enter-press fallback follows, fatal re-raised
  319 | except Exception | return-early | N | logs red, returns False — auth failure correctly signaled
  338 | except Exception as exc | return-early | N | logs, returns False; caller raises AuthFailedError non-interactively
  353 | except Exception as exc | pass | A | fill probe over selector list; fatal re-raised, retried until deadline
  401 | except Exception | pass | A | scroll-container probe; falls back to window scroll
  421 | except Exception as exc | pass | A | card-selector probe; fatal re-raised, next selector tried
  469 | except Exception as exc | return-early | C | fallback extractor dies -> query yields 0 jobs, indistinguishable from empty
  578 | except Exception as exc | pass | C | badge probe failure can leave has_easy_apply=False, flipping apply routing (reclassified B->C: decision-affecting)
  609 | except Exception as exc | return-early | C | whole card silently dropped from ingestion; only dim console line
  654 | except Exception | pass | A | detail-panel render wait probe; absence expected
  678 | except Exception | fallback-value | B | corrupt persisted extra_json silently becomes {}; routing flag lost
  701 | except Exception | pass | A | detail-panel wait probe, same as 654
  731 | except Exception | fallback-value | A | inner_text failed; accepts button anyway — conservative fallback
  734 | except Exception | pass | A | first-pass button probe; two more passes + diagnosis follow
  770 | except Exception as exc | fallback-value | A | JS-handle probe; fatal re-raised, third pass follows
  775 | except Exception | pass | A | second-pass wrapper; distinct not-found outcome still reached
  791 | except Exception | pass | A | third-pass retry probe
  812 | except JobExpiredError | reraise | N | expired-job signal preserved through diagnosis block
  814 | except Exception | pass | B | diagnosis crash skips expired/login checks -> wrong outcome code possible
  852 | except Exception | pass | B | step-header read fails silently; stuck-detection disabled that step
  887 | except Exception as exc | pass | A | modal probe; fatal re-raised; no-modal handled below
  915 | except Exception as exc | pass | A | submit-button probe; fatal re-raised
  927 | except Exception as exc | pass | A | review-button probe; fatal re-raised
  943 | except Exception as exc | pass | A | next-button probe; fatal re-raised
  966 | except (EOFError, KeyboardInterrupt) | fallback-value | N | confirm="n" -> distinct submission_cancelled outcome persisted
 1002 | except JobExpiredError | reraise | N | propagates to orchestrator correctly
 1004 | except PDFTextLayerError | reraise | N | deliberately propagated for self-healing pause (commented)
 1009 | except Exception as exc | log+fallback | C | post-click exception marks real submission as failed -> duplicate-apply risk
 1041 | except Exception | pass | A | optional availability-flag import probe; proceeds to real attempt
 1051 | except Exception as exc | return-early | B | tailoring failure -> untailored resume used; console-only, not persisted
 1114 | except Exception | pass | A | apply-button selector probe; JS fallback follows
 1136 | except Exception as exc | fallback-value | A | JS-handle fallback; fatal re-raised; not-found outcome downstream
 1172 | except Exception | pass | A | interstitial continue-link probe; pass 4 follows
 1174 | except Exception | pass | A | no popup is an expected variant; same-page interstitial handled
 1184 | except Exception | pass | A | URL-check probe inside recovery path
 1239 | except Exception | retry | A | popup expectation fails -> plain click fallback
 1246 | except Exception as exc | pass | A | interstitial selector probe; fatal re-raised
 1260 | except Exception as exc | log+return | B | click-path crash reason lost; collapses into generic not-found outcome
 1284 | except PDFTextLayerError | reraise | N | deliberately propagated (commented rationale)
 1289 | except Exception as exc | fallback-value | N | distinct persisted outcome linkedin_external_apply_error set
 1315 | except Exception | return-early | A | button-click helper; caller sets linkedin_step_blocked outcome
 1362 | except Exception | fallback-value | C | eval failure -> "logged in"; unknown converted to session-valid
 1430 | except Exception as exc | pass | B | select fill failure silent; field left blank, no signal which question failed
 1473 | except Exception as exc | pass | B | radio fill failure silent; legally-consequential question skipped without trace
 1525 | except Exception as exc | pass | B | text-question fill failure silent; degrades form completion invisibly
 1537 | except Exception | fallback-value | B | profile load failure -> all answers ""; broken profile.json looks like empty profile
 1547 | except Exception | fallback-value | B | AnswerBank import bug indistinguishable from intentional fail-closed None
```

### `src/sources/usajobs.py` (30 handlers: A=13 B=7 C=5 N=5)

```
   99 | except AuthFailedError | reraise | N | explicit re-raise so orchestrator triggers ReauthManager
  101 | except Exception as exc | log+continue | C | mid-run scrape error returns partial job list as normal result; discovery silently truncated
  124 | except Exception | fallback-value | C | browser error becomes "not logged in"; unknown converted to definite session-validity verdict
  145 | except Exception as exc | retry | A | sign-in selector probe; re-raises browser death; direct login.gov goto is the fallback
  168 | except Exception | pass | B | email-step failure fully silent; final verify catches outcome but which step failed is lost
  181 | except Exception | pass | B | password-step failure fully silent; same signal loss as email step
  211 | except Exception as exc | fallback-value | N | logs, returns False; caller raises AuthFailedError or prompts human — correct signal
  237 | except Exception | retry | A | probing optional 2FA method-picker links; absence normal
  257 | except Exception | retry | A | probing optional auth-method radio candidates
  274 | except Exception | retry | A | OTP input locate probe; not-found handled via explicit return False
  300 | except Exception as exc | fallback-value | N | logs error, returns False; 2FA cascade continues, final login verified
  313 | except Exception | return-early | B | missing codes file is normal; corrupt JSON is silently identical — signal lost
  335 | except Exception | retry | A | probing backup-code link candidates
  355 | except Exception | retry | A | probing method-picker radio candidates
  372 | except Exception | retry | A | backup-code input probe; not-found logged and returned False
  434 | except Exception | retry | A | email-2FA code-input probe; not-found returns False explicitly
  506 | except Exception as exc | retry | B | re-raises browser death, but selector-drift errors silent -> zero cards looks like empty market
  646 | except Exception as exc | return-early | C | parse failure drops the job entirely; never persisted/scored; only a dim console line
  698 | except Exception | retry | B | apply-button probe; distinct outcome recorded downstream, but no browser-death re-raise mislabels crash as button-not-found
  766 | except (EOFError, KeyboardInterrupt) | fallback-value | A | no stdin -> treat as "no"; cancelled outcome persisted
  784 | except Exception as exc | retry | C | click may have dispatched submission; error keeps submitted=False -> possible duplicate apply
  820 | except (EOFError, KeyboardInterrupt) | return-early | A | manual-navigation prompt aborted; expected non-interactive case
  823 | except JobExpiredError | reraise | N | propagates distinct expired outcome
  825 | except Exception as exc | log+continue | N | persists distinct "usajobs_error" outcome — the PR #128 pattern done right
  882 | except Exception | pass | A | page-type probe; URL keyword check is the parallel detector
  901 | except Exception as exc | log+continue | B | resume-selection failure only dim-logged; wizard usually blocks, but signal is buried
  936 | except Exception as exc | log+continue | C | whole questionnaire swallowed; auto-submit then submits with wrong/missing eligibility answers, status "submitted"
  964 | except Exception as exc | pass | B | per-question failure silent (browser death re-raised); required-field validation usually blocks Continue
  978 | except Exception | pass | A | review-summary printout is cosmetic
 1000 | except Exception as exc | retry | A | Next-button probe; re-raises browser death; False mapped to distinct step_blocked outcome
```

### `src/sources/base.py` (29 handlers: A=9 B=6 C=4 N=10)

```
   76 | except ImportError | fallback-value | N | patchright->playwright fallback; engine name surfaced at launch
  172 | except Exception | fallback-value | A | pgrep probe feeds only a diagnostic hint string
  198 | except Exception | pass | B | pkill of orphaned Chrome fails silently; later launch failures lose their root cause
  205 | except Exception | pass | A | singleton lockfile unlink; launch retry loop surfaces persistent failure
  213 | except Exception | pass | A | DB lockfile unlink; same downstream detection
  257 | except BaseException | reraise | N | cleans up partial launch then re-raises original
  265 | except BaseException | pass | A | best-effort teardown inside failure path; original exception still re-raised
  271 | except Exception | pass | B | failed lock release silent; stranded lock later blocks/refuses launches with cause hidden
  289 | except (json.JSONDecodeError, OSError) | log+continue | N | logs, deletes corrupt export, falls back — correct recovery
  383 | except Exception as launch_exc | retry | N | logs each attempt, re-raises on final attempt
  394 | except Exception | pass | B | chrome_launch lock release failure silent; can stall every future launch up to 180s
  406 | except Exception | pass | B | same release-in-finally silence
  418 | except ImportError | fallback-value | N | stealth 2.x->1.x API fallback
  422 | except ImportError | log+continue | N | warns clearly that stealth is OFF
  427 | except Exception as exc | log+continue | N | stealth apply failure warned; degraded mode is explicit
  447 | except Exception as exc | log+continue | N | export failure warned; stale export later fails as AuthFailedError, which is handled
  451 | except Exception | pass | A | tmp-file cleanup
  483 | except Exception | pass | A | context.close teardown; orphan Chrome later reaped by _clear_profile_locks
  488 | except Exception | pass | A | browser.close teardown
  493 | except Exception | pass | A | playwright.stop teardown
  511 | except Exception | pass | B | profile-lock release failure silent; own comment: stranded lock "names a live PID forever"
  527 | except Exception | fallback-value | C | _safe_click: browser death becomes False="element absent"; no re-raise unlike _safe_evaluate; feeds apply decisions
  536 | except Exception | fallback-value | C | _safe_fill: crash -> False; caller may proceed and submit form with field unfilled
  549 | except Exception as exc | fallback-value | N | re-raises browser death, warns, returns default — the correct reference pattern
  564 | except Exception as exc | fallback-value | N | same pattern for goto
  639 | except Exception | fallback-value | C | label lookup failure -> label="" -> is_generic -> resume uploaded into an unidentified field
  702 | except Exception as exc | log+continue | C | upload loop failure printed then ignored; apply proceeds and can submit resume-less, status "submitted"
  717 | except Exception as exc | fallback-value | A | parent-closest probe; re-raises browser death; parent=None fallback continues
  726 | except Exception | pass | B | any label-resolution error -> ""; silently degrades form-answer selection quality
```

### `src/sources/indeed.py` (15 handlers: A=6 B=1 C=5 N=3)

```
  107 | except AuthFailedError: | reraise | N | deliberate passthrough so orchestrator sees auth failure
  109 | except Exception as exc: | log+continue | C | scrape crash returns partial/empty list; source breakage persisted as "few/no jobs"
  128 | except Exception: | fallback-value | C | unknown page state becomes "not login wall"; prepare_session then reports session fresh
  154 | except Exception as exc: | log+continue | B | failed search means default feed scraped instead of keywords; only dim console note
  186 | except Exception: | return-early | C | pagination click failure silently truncates results; looks like "no more jobs"
  319 | except Exception as exc: | log+continue | C | hydration failure leaves stub description that feeds scoring/dedup; only dim log
  377 | except Exception: | pass | A | probing possibly-closed popup tabs; per-page url read failure is expected
  380 | except JobExpiredError: | reraise | N | cleanup then propagate; orchestrator persists expiry correctly
  383 | except Exception as exc: | log+continue | C | browser error persisted as "indeed_easy_apply_or_no_ats"; transient failure misclassified as Easy Apply
  525 | except Exception: | pass | A | probing alternative email-input selectors; timeout per selector is normal
  540 | except Exception: | pass | A | probing Continue-button selector variants; miss is expected
  550 | except Exception: | pass | A | probing password-input variants; absence handled by returning False
  564 | except Exception: | pass | A | probing Sign-In button variants; miss is expected
  570 | except Exception: | pass | A | load-state wait is cosmetic; URL polling loop below verifies outcome
  585 | except Exception as exc: | fallback-value | N | login failure logged and correctly signalled False; callers handle both paths
```

### `src/sources/themuse.py` (7 handlers: A=1 B=0 C=3 N=3)

```
   95 | except httpx.HTTPStatusError as exc: | return-early | C | HTTP/429 truncates pagination; partial ingest indistinguishable from complete run
  102 | except Exception as exc: | return-early | C | network/JSON failure silently truncates discovery; persisted as fewer jobs
  214 | except Exception: | pass | A | probing possibly-closed popup tabs for redirect URL; expected misses
  219 | except JobExpiredError: | reraise | N | cleanup then propagate; expiry handled upstream
  222 | except Exception as exc: | log+continue | C | browser error persisted as "themuse_no_ats_url"; transient failure conflated with genuine no-ATS
  268 | except PDFTextLayerError: | reraise | N | deliberate passthrough to pause apply loop for self-healing
  273 | except Exception as exc: | fallback-value | N | distinct persisted outcome token "themuse_external_apply_error"; correct signal
```

### `src/sources/builtin.py` (7 handlers: A=0 B=2 C=3 N=2)

```
  108 | except json.JSONDecodeError: | fallback-value | C | malformed jobPostInit blob silent; markup drift can vanish every job (reclassified B->C: discovery truncation)
  120 | except json.JSONDecodeError: | continue | C | bad ld+json blob skipped silently; if all fail, job dropped without signal (reclassified B->C: discovery truncation)
  237 | except Exception as exc: | fallback-value | C | request failure returns None; listing loop breaks — pagination/jobs silently truncated
  311 | except Exception as exc: | fallback-value | B | re-fetch failure falls back to stashed ATS URL; logged, sensible degradation
  337 | except json.JSONDecodeError: | fallback-value | B | corrupt extra_json silently drops stashed ats_url; top-level fallback usually covers
  366 | except PDFTextLayerError: | reraise | N | deliberate passthrough for self-healing pause
  368 | except Exception as exc: | fallback-value | N | distinct persisted outcome token "builtin_external_apply_error"; correct signal
```

### `src/sources/jobspy_scraper.py` (2 handlers: A=0 B=0 C=2 N=0)

```
   44 | except Exception as e: | return-early | C | spawn failure returns empty list; source outage persisted as zero jobs
   55 | except Exception as e: | return-early | C | unparseable bridge output becomes zero jobs; whole-source failure looks like empty feed
```

### `src/sources/jobspy_bridge.py` (1 handlers: A=0 B=0 C=0 N=1)

```
   41 | except Exception as e: | log+exit(1) | N | correct machine-readable failure signal; parent checks returncode and reports
```
