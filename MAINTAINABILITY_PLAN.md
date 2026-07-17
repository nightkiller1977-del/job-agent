# Job Agent — Maintainability & Reliability Plan (Track M) — v2

> Companion to `ATS_ADAPTER_PLAN.md` (success-rate first). v2 incorporates an external
> review (2026-07-17) that correctly elevated **submission truth, idempotency, policy
> containment, and profile lifecycle** above the source-catalog cleanup. Mapped against
> live code on `fix/job-agent-success-rate`. **Proposal for review — no code changed.**
>
> Change log v1→v2 at the end (§6), including three review premises corrected against code.

---

## 0. Two reframes that set the order

**Reframe A — the flag flip is the end, not the start (unchanged from v1).**
The adapter path is not at parity with the 3,862-line legacy body. It lacks Workday
navigation (no WorkdayAdapter registered), Microsoft/BrassRing/SmartRecruiters/Teamtailor
entry flows, resume tailoring, ATS scoring, extension autofill, company-portal login, the
empty-form guard, and the rich analytics shape. Flipping `USE_ADAPTER_REGISTRY` today
regresses the ~11% apply rate. "Kill the god module" = **split** it (external apply →
adapters; tailoring/scoring → shared services; native scrape → slim `JobrightScraper`).

**Reframe B — a click is not an application (new, from the review; the biggest gap).**
The adapter path treats a successful *submit click* as success, which can inflate the very
metric Track S exists to raise:
- `GenericAtsAdapter.apply` returns `AtsApplyResult.ok(...)` immediately after
  `self._submit(...)` returns truthy, with **no receipt/confirmation verification**
  (`generic.py:147-153`).
- `BrowserUseRecovery` returns the contradictory state `AtsApplyResult(submitted=True,
  status="review_ready", ...)` at `recovery_browseruse.py:50` and `:122` — `submitted=True`
  while the status literally says "ready for review," i.e. **not** submitted.
- `BrowserUseRecovery` references `ctx.policy`/`confirm_submit` **nowhere**, yet
  `ExternalApplySession.apply` invokes it on form-completion failure (`session.py:106-112`)
  — so an LLM path can (claim to) submit **outside the policy gate**.

Therefore reliability controls come **before** the cosmetic refactor. If a higher apply
rate is really a higher *false-success* or *duplicate-submit* rate, the refactor made
things worse, not better.

---

## 1. Guiding constraints (from the repo)
Success rate stays the priority; Track M must not destabilize Track S and should feed it.
Parity behind characterization tests before any deletion. Honor `CLAUDE.md` (no main-profile
repoint; no 2nd Chrome on `jobright_profile`; no push to main; no `--no-verify`). Validate
locally first (residential IP). **`applied` must mean receipt-verified — never "clicked."**

---

## 2. Phases

### PHASE 0 — Reliability substrate + foundations
*Low-to-medium risk · highest value · do first · ordered so truth/containment precede cleanup*

**0.0 — Confirm the branch is current with main, then baseline.**
As of 2026-07-17 the branch is **8 commits ahead, 0 behind `origin/main`** (the review's
"2 behind" is stale). Do not establish characterization baselines on a soon-to-diverge tree:
merge/rebase onto `origin/main` if it advances, then freeze the baseline. (Note: another
editor may be touching this repo — the secret module was renamed `secrets.py`→`secret_store.py`
and `.claude/worktrees/agent-…` shows dirty; coordinate before large edits.)

**0.1 — Application-attempt state machine (before any status enum work).**
Define one explicit phase progression and make it the spine of persistence and events:
```
started → form_reached → fields_filled → submit_authorized → submit_clicked → receipt_verified
                                                                            ↘ failed
                                                                            ↘ unknown
```
Rules: `submit_clicked` is distinct from `receipt_verified`; **`applied`/success = `receipt_verified`
only**; any ambiguous post-click state is `unknown`, never success. Add a real
`_verify_receipt(page)` step (confirmation URL/text, application-ID capture, or ATS API check)
that both `GenericAtsAdapter` and `BrowserUseRecovery` must pass through before returning ok.
Kill the `submitted=True, status="review_ready"` contradiction — `review_ready` is a
*readiness/blocked* result, not a submitted one.

**0.2 — Idempotent submission.**
Prevent retries/recovery/crash from double-applying: per-attempt `attempt_id`, a canonical
`(vendor, canonical_ats_url)` dedupe key, a pre-submit duplicate check against prior
`receipt_verified`/`submit_in_progress` records, and a persisted **"submit in progress"**
marker written *before* the click and reconciled after. A crash between click and receipt
must resolve to `unknown` + a dup-guard on the next run, not a blind re-submit.

**0.3 — Policy gate as a non-bypassable session responsibility.**
Move the submission gate out of adapter convention and into `ExternalApplySession`: no adapter
or recovery route may submit without passing `policy.confirm_submit`. Concretely, `session.py`
enforces the gate around **both** the primary adapter and the BrowserUse recovery call
(`session.py:106-112`), and `ctx.policy` is guaranteed non-`None` (today it defaults to `None`
at `context.py`, and `generic.py:140` would `AttributeError` if it were ever exercised without
one). A no-op policy is still a *policy*, chosen explicitly, not an absence.

**0.4 — Profile lock + browser lifecycle in `ExternalApplySession`.**
`session.py` starts a browser on the shared `jobright` profile (`:86`) but has **no
`close`/`finally`/lock** — the "never a second Chrome on `jobright_profile`" rule
(`CLAUDE.md`) is documentation-only today (`session.py:17-19`). Add a per-profile async/file
lock and a `finally` that closes the browser (or provably reuses the live Jobright context).
Enforce the rule in code.

**0.5 — `src/source_catalog.py` (NOT the secret module).**
Collapse the **7** duplicated "source → env var" definitions (`main.py:73`,
`orchestrator.py:1128`+`:1168`, `blocker_classifier.py:20`, the `reauth.py:96` f-string,
`commander.py:20`+`:240`, and the flat `secret_store.py:47` `CANONICAL_KEYS`) into one
`SOURCE_CATALOG` in a **new** `src/source_catalog.py`: `{email_env, password_env, extra_envs,
login_mode, reauth_recovery, display, cli_choices, scraper_class}`. Derive the seven maps, the
AUTOMATED/HUMAN sets, the six argparse `choices=` lists, and heartbeat/reauth rules from it.
Source behavior + CLI + auth policy is **application config**, not secret storage —
`secret_store.py` stays narrowly responsible for secret *resolution*. Remove the
`USAJOBS_USERNAME` fossil in `test_commander_unit.py:59,:450`.
- Shadow guard: the stdlib-`secrets` shadow the review cites is **already resolved** — the
  module is `secret_store.py`, not `secrets.py`. Add a cheap regression test that (a) fails if
  an `src/secrets.py` reappears and (b) asserts `import secrets` and `pyotp` (`usajobs.py:218`)
  resolve to stdlib with `src/` first on `sys.path`.

**0.6 — Status vocabulary → separate domains + migration.**
Not one broad enum — **separate domains**: `JobStatus`, `AttemptPhase` (0.1), `AttemptOutcome`,
`SessionHealth`, `ReauthMode`, `ReauthOutcome`, `AlertLevel`. `review_ready` is *readiness*, not
an outcome. Have `_STATUS_TO_CLASS` (`blocker_classifier.py:38`) and `_FAILURE_CLUSTERS`
(`state_manager.py:425`) consume them. Unify `needs-session`/`needs_session_prep`; drop dead
`needs-portal-login` and unused `reviewed`. Fix readiness leaking into `apply_last_status`
(`orchestrator.py:651`). **Keep persisted wire values as strings** for SQLite/JSONL compat.
Add **migration/versioning**: a `schema_version`, an idempotent migration that reads legacy
status values, writes canonical ones, and backfills once.

**0.7 — Retire generated tests → fixtures + failure-injection.**
Remove `ReauthManager._write_regression_test` (`reauth.py:257-304`), `git rm` the 123-function /
~128 KB `test_reauth_regressions.py` (8 commits/30 days; it asserts mocks, not behavior),
gitignore any generated artifact. Replace with fixture-driven tests **plus a failure-injection
matrix**: page death, navigation timeout, receipt-not-found-after-click, required-field
validation error after click, session-save failure, DB failure, policy denial, notifier
failure, and duplicate-prevention.

**Exit:** `applied`=receipt-verified; submission is idempotent; no route bypasses policy;
profile lock + cleanup enforced; catalog + status domains single-sourced with a migration;
churn gone; failure-injection suite green.

---

### PHASE 1 — Deduplicate login + browser-death (revised)
*Medium risk · after Phase 0*

**1a. Share the machinery, not the scripts.** `_auto_login` is quadruplicated (`jobright.py:1248`,
`indeed.py:482`, `linkedin.py:275`, `usajobs.py:127`) but the flows are *materially* different
(Workday-style redirect, Indeed email-first, LinkedIn challenge-bail, USAJobs 2FA cascade). Share
only: **browser lifecycle, credential lookup (via the catalog), error normalization, and session
validation.** A `LoginSpec` carries per-source `login_action` and an **`authenticated_probe`**
(navigate a lightweight authenticated page and confirm) — not a one-size login script. Preserve the
generic call at `reauth.py:111`. Fix Indeed's swallowed browser-death and missing `_save_session()`.

**1b. Browser-death normalization at the boundary only.** The check appears 27× in 3 drifted
variants. Add one `is_browser_dead(exc) -> bool` used to set a `browser_dead=True` flag while
**preserving the original exception type and message** — do not collapse everything to a generic
"browser died," or Track S loses the diagnostic reason. Normalize at the boundary, not by mutating
exceptions in flight.

**1c. Indeed reauth.** Keep escalating `_auto_login=False` directly to human-assisted reauth; add
closed-browser/page detection; validate a refreshed session with the `authenticated_probe`, not
cookie names/mtime alone.

---

### PHASE 2 — Observability: SQLite source-of-truth + JSONL audit + safe notifier (revised)
*Low risk · feeds Track S*

**2a. Two roles, not one.** **SQLite stays the queryable source of truth for application state.**
JSONL (`state/runs/{run_id}.jsonl`) is an **append-only audit stream**, built on the existing
`telemetry.py`/Loki seam. Every event carries `schema_version, run_id, attempt_id, job_id, vendor,
phase (AttemptPhase), outcome, duration`. **Redaction is mandatory:** never write resumes, phone
numbers, screening answers, cookies, or raw model prompts to events. Console output becomes a thin
formatter over the stream (rich for humans; JSONL for machines; `stats`/P1 reads SQLite).

**2b. One notifier, fail-open.** Collapse the 5 channels (status-file, macOS, Telegram, iMessage,
Telegram deep-link, across 3 modules) behind one `dispatch(level, event)` that is **fail-open**
(a notification failure must never change an application result), **deduplicated by event/attempt
ID**, and distinguishes three classes: **FYI**, **human-action-required**, **submission-blocked**.

---

### PHASE 3 — Kill the god module (revised gate & rollout)
*High risk · gated behind Track S + delta closure*

1. **Characterize** legacy behavior (submit detection, Workday chooser/expiry, Teamtailor,
   BrassRing, SmartRecruiters, empty-form guard) on the frozen 0.0 baseline.
2. **Extract shared services** (tailoring, scoring) used by both the native path and adapters; fix
   the `ResumeFieldFixer` relative-path CWD bug (`resume_helper.py:92`).
3. **Close the vendor delta, biggest-gap-first:** real **WorkdayAdapter** (nav+chooser+wizard+
   expiry), then Microsoft/BrassRing/SmartRecruiters/Teamtailor; **register `BrowserUseRecovery`**
   in the registry (manual today) behind the 0.3 policy gate; extend `detect_vendor` (`generic.py:33`).
   Registry `can_handle` exceptions are silently scored `0.0` (`registry.py`) — **emit an
   `adapter_selection_error` event** so a failing adapter doesn't quietly route to generic fallback.
4. **Compare without double-submitting.** Do **not** run legacy and adapter paths against the same
   live job (dup-application + profile-collision risk). First: **side-effect-free preflight/form-fill
   comparison** (fill, diff, don't submit). Then a **per-vendor canary cohort** where exactly **one**
   path is authorized to submit.
5. **Roll out by vendor allowlist, not a global boolean.** `USE_ADAPTER_REGISTRY` becomes a
   per-vendor allowlist with **immediate per-vendor rollback**.
6. **Split & delete** the legacy body; carve out the slim native `JobrightScraper`.

**Flag-flip gate (all must hold):**
- All legacy status values can be read; new writes are canonical (0.6 migration).
- No adapter or recovery route can bypass policy (0.3).
- `applied` requires receipt verification; ambiguous post-click state is `unknown`, never success (0.1).
- Profile lock and browser cleanup verified (0.4).
- Per-vendor characterization + canary metrics show no material regression.
- Every attempt has event coverage and idempotency protection (0.2, 2a).
- Track S has been browser-validated locally on the Mac.

---

## 3. Sequencing & effort

| Phase | Focus | Value | Risk | Effort | Depends on |
|---|---|---|---|---|---|
| 0.0–0.4 | Reliability (truth, idempotency, policy, lifecycle) | ★★★★★ | Low–Med | M | — |
| 0.5–0.7 | Foundations (catalog, status domains+migration, tests) | ★★★★★ | Low | S–M | 0.1 (state machine) |
| 1 | Dedup (login machinery, death normalization) | ★★★☆ | Med | M | 0.5 (catalog) |
| 2 | Observability (SQLite+JSONL, safe notifier) | ★★★★ | Low | M | 0.1, 0.6 |
| 3 | Kill god module + per-vendor flip | ★★★★★ (eventual) | High | L | 1, 2, **Track S** |

**Do first:** 0.0→0.4. Reliability controls keep a higher apply rate from becoming a higher
false-success / duplicate-submit rate — they gate everything else.

---

## 4. Decision resolved by data — LinkedIn auth mode
`reauth.py:35` says AUTOMATED; `session_watchdog.py:50` says HUMAN. Data (2026-07-17): 989 LinkedIn
jobs, 5 applied; failures are external-ATS/form (only 3/989 show any checkpoint/challenge signal);
**0** of the last 100 reauth events are LinkedIn while automated reauth is failing 100% elsewhere
(jobright 58/58, indeed 14/14). **Resolution — split the field in the catalog:** `login_mode =
automated` (in-run login; already escalates on challenge at `linkedin.py:341`), `reauth_recovery =
human` (don't trust the empirically-0% auto-reauth path). Caveat per the review: **absence of recent
LinkedIn reauth events is not proof automated reauth is healthy** — it's proof LinkedIn rarely enters
that path. The split stands on the failure-distribution + the challenge-bail behavior, not on the
empty event log.

---

## 5. Testing additions (from the review)
Failure-injection tests for: page death, navigation timeout, receipt-not-found after click,
required-field validation after click, session-save failure, DB failure, policy denial, notifier
failure, duplicate-prevention. The generated reauth file is removed (repeated routing assertions,
not regression coverage) and replaced with fixtures.

---

## 6. Change log v1 → v2 (and review premises corrected against code)

**Adopted from the review:** submission state machine + receipt verification (0.1); idempotency
(0.2); policy gate as session responsibility (0.3); profile lock + lifecycle (0.4);
`source_catalog.py` split from the secret module (0.5); separate status domains + string wire
values + migration/versioning (0.6); failure-injection matrix (0.7, §5); share login machinery not
scripts + `authenticated_probe` (1a/1c); boundary-only death normalization preserving type/message
(1b); SQLite-as-truth + JSONL audit + redaction (2a); fail-open, deduped, 3-class notifier (2b);
preflight/canary comparison + per-vendor allowlist rollout (3); `adapter_selection_error` event (3);
revised 7-point flip gate; "absent events ≠ healthy reauth" caveat (§4).

**Three review premises corrected against current code:**
1. **Branch state.** Review said "7 ahead, 2 behind main." Verified: **8 ahead, 0 behind
   `origin/main`** — not behind. 0.0 keeps the *intent* (freeze the baseline before parity tests)
   without the stale premise.
2. **`secrets.py` shadowing.** Already resolved: the module is **`secret_store.py`** (no
   `src/secrets.py` exists; imported at `main.py:55`/`orchestrator.py:67`). The fix the review asks
   for is done; 0.5 keeps a guard test to prevent regression.
3. **Reauth "accepts only changed mtime."** Current code already does deep cookie-expiry JSON
   inspection as the *primary* check with mtime as a *fallback* (`reauth.py:243-252`). Still worth
   upgrading to an `authenticated_probe` (1c), but it is not mtime-only today.

**Verified-true review claims (now first-class in the plan):** generic returns ok on click without
receipt (`generic.py:147-153`); BrowserUse `submitted=True/status=review_ready` contradiction
(`recovery_browseruse.py:50,122`); BrowserUse ignores the policy gate (no `confirm_submit`);
`session.py` has no browser close/lock; registry silently scores exceptions `0.0`.
