# Job Agent Plan: Raise Apply Success Rate (Success-First Ordering)

> **Reordered around one goal: increase the rate at which the agent actually submits applications.**
> Maintainability/refactor work is retained but demoted — functionality delivers the value.
> Every priority below is justified by the **measured baseline** from `state/jobs.db`, not theory.

---

## Progress (updated 2026-07-11)

| Item | Owner | Status | Branch / PR |
|---|---|---|---|
| **P1** Instrumentation & success-rate report | Claude | ✅ **Done** (merged) | PR #30 → `feat/success-rate` |
| **P2** Blocker classifier + circuit breaker | Claude | ✅ **Done** (merged) | PR #30 |
| **P6** Discovery via public ATS APIs (Greenhouse/Lever/Ashby) | Antigravity | ✅ **Done** (merged) | PR #29 |
| patchright spike + 5 handoff modules (answer bank, selectors, evidence, discovery hardening) | Antigravity | ✅ **Done** (merged) | PR #29 |
| **7a** Adapter contract + **real** GenericAtsAdapter/ExternalApplySession | Claude | ✅ **Done** (merged) | PR #30 |
| **P3** Session/auth preflight + re-auth | Claude | ✅ **Done** (merged) | PR #30 |
| **P4** Form robustness: submit selectors, `_click_ats_apply_button` (form_not_reached), patchright, openlit fix | Claude | ✅ **Done** (unit-tested) | **PR #31** |
| Go-live: flag-gated registry apply path (`USE_ADAPTER_REGISTRY`) | Claude | ✅ **Done** (default OFF) | PR #31 |
| **P5** Browser Use recovery adapter | Antigravity | 🚧 In progress | Antigravity branch |
| **P6** Vendor apply adapters (greenhouse/lever/ashby) | Antigravity | 🚧 In progress | Antigravity branch |

**Live findings from P1 (measured, not estimated):** baseline confirmed at **10.9%** (5/46). New: **245 wasted retries**, and the **`jobright` apply source is 0% (0/23)** — the single largest failure concentration, vs LinkedIn 25% (4/16). P2's circuit breaker would skip **42/46** previously-attempted jobs, ending the retry bleed.

> **⚠ Verification pending (Mac only):** all browser-dependent behavior (patchright, the registry apply path, jobright form fixes, reauth) is **unit-tested but not browser-verified** — the CI sandbox has no playwright/pypdf/openlit. Verify on the Mac: `patchright install chromium`, then `python src/main.py stats`, then `USE_ADAPTER_REGISTRY=1 python src/main.py apply …`, and compare `stats` before/after. The go-live flag defaults OFF so the proven path runs until you flip it.

---

## Measured Baseline (from `state/jobs.db`, 528 discovered jobs)

| Metric | Value |
|---|---|
| Discovered jobs | 528 |
| Reached "applied" status | **7 (1.3%)** |
| Apply attempts recorded | 46 |
| Attempts that succeeded | **5 → ~11% attempt success rate** |
| Attempts that failed | 41 |
| Max retries on a single job | **17** (no circuit breaker) |
| Attempts with analytics (`submitted`/`atsScore`) recorded | **0** (measurement is dead) |

**Failure breakdown (41 failures):**

| Cluster | Statuses | Count | % of failures |
|---|---|---|---|
| **Form completion** (reached page, couldn't finish) | `external_ats_error` 15, `form_not_reached` 7, `submit_not_found` 5 | **27** | **66%** |
| **Auth / session** | `workday_session_expired` 4, `brassring_login_required` 3 | 7 | 17% |
| **Vendor handoff / reach** | `microsoft_apply_not_reached` 2, `linkedin_external_apply_not_found` 2, `linkedin_stuck_on_required_field` 1 | 5 | 12% |
| Misc | `bad_ats_url` 1, `error` 1 | 2 | 5% |

**Conclusions that drive the ordering:**
1. The dominant failure is **form completion on already-reached vendors (66%)** — *not* missing vendor coverage. Adding Greenhouse/Lever barely moves an 11% rate.
2. **You cannot see what's happening** — analytics are `None` for every attempt; failures store only a status string. Fix measurement first or you're tuning blind.
3. **Retries are wasted** — up to 17 blind re-attempts per job with no learning. Convert that effort into signal.

---

## Two Tracks (priority order)

- **Track S — Success Rate (Priorities 1–5).** Do these first. Most are *orthogonal to the refactor* and can be built on the current code for fast value.
- **Track M — Maintainability & Stability (Priorities 6–7).** The adapter refactor from the prior plan. Retained, corrected, but demoted. Do a *thin slice* early only where it unblocks Track S; full extraction later.

> Coupling note: Priorities 1–3 live in `orchestrator.py` / `state_manager.py` / `reauth.py` and are independent of the god-class refactor — cheap now. Priorities 4–5 touch the apply internals in `jobright.py`; they're cleaner after the thin refactor slice (6a) but do not require the full extraction.

---

## Open-Source Tooling (evaluated against the failure clusters, July 2026)

The stack is **Python + async Playwright**. Anti-bot effectiveness depends heavily on **where it runs** — see [Deployment Target](#deployment-target-local-vs-render) below. On the user's Mac (residential IP), Cloudflare's 2026 model mostly passes the traffic and stealth tooling is effective; on Render (datacenter IP) the IP itself is flagged regardless of fingerprint, which changes the calculus.

| Tool | License | Attacks | Fit | Integration cost | Verdict |
|---|---|---|---|---|---|
| **patchright** (Python) | permissive (verify) | `external_ats_error` from bot-detection | drop-in replacement for `playwright.async_api` | **very low** | **Adopt (P4)** — swap import, patches the `Runtime.enable`/WebDriver leaks Cloudflare watches |
| **Browser Use** (Python) | **MIT** | `submit_not_found`, `form_not_reached`, unexpected steps (the 66%) | native Python, Ollama-capable, self-healing, saves per-site "domain skills" (selectors/flows) | medium | **Adopt (P5)** — bounded recovery layer; MIT = safe to ship |
| **Skyvern** (Python) | **AGPL-3.0** | form-filling via vision+LLM (the 66%, hardest cases) | strong but heavy; AGPL is a distribution constraint | high | **Optional/isolate** — only as a separate API service to fence the AGPL boundary; skip unless P4+P5 leave a gap |
| **Stagehand** | Apache-2.0 | same as Browser Use | **TS-first**; Python is a secondary wrapper | high (Node sidecar) | **Skip** — Browser Use covers this in-language |
| **playwright-stealth** (Python) | MIT | light bot-detection | actively maintained (v2.x) | low | **Fallback** to patchright if a drop-in swap is undesirable |
| **Greenhouse / Lever / Ashby public JSON APIs** | public, no OAuth | `bad_ats_url`, reach failures, coverage | pull structured postings + clean apply URLs | low | **Adopt (P6)** — separate discovery from apply |
| ApplyPilot | AGPL-3.0 | — | — | — | **Avoid code** — reference architecture only |

Local-LLM note: Browser Use and Skyvern both support Ollama, which fits the project's existing model cascade (Ollama → Claude) — recovery can run on local inference first before spending cloud tokens. (Caveat: local Ollama inference is only free on the Mac; on Render you'd pay for GPU or fall back to cloud tokens.)

---

## Deployment Target: Local vs Render

Deploying the full agent to Render is viable and has real upside, but it **reverses the most favorable assumption behind P4/P5** (residential IP). Treat this as a decision that gates, not blocks, the success-rate work.

**The dominant variable — egress IP reputation:**
- **Local Mac (residential IP):** stealth tooling (patchright) works; Cloudflare-fronted ATS mostly pass. This is the assumption P4/P5 are written under.
- **Render (datacenter IP):** Cloudflare's 2026 model flags cloud-provider IP ranges *regardless of browser fingerprint*. Many ATS sit behind Cloudflare → this can **reverse P4's anti-bot gains** and worsen the 66% completion + reach clusters. patchright alone won't save it. **Required mitigation:** route browser egress through a **residential/mobile proxy** (Bright Data / Oxylabs / etc.) — added cost, latency, reliability, ToS weight.

**Headless-server hazards:**
- **Ephemeral FS:** mount a **Render persistent disk** at `state/` or sessions/cookies/tailored resumes wipe on every redeploy (`state/sessions/`, `state/profile.json`, `state/jobs.db`).
- **Jobright extension + logged-in profiles** need headed Chrome via **xvfb**; initial interactive login/2FA is hard headless → seed authenticated cookies/profile from a local login, and expect **re-auth (P3) to be harder → the 17% auth cluster risk goes UP**.

**What Render genuinely improves:**
- **24/7 autonomous operation** — applies + discovery run without the Mac awake (the agent actually runs continuously).
- **Observability** — P1 metrics pipe straight into the existing **aicc-observability** Render stack (already owned).
- **Human approval** — SMS/cloud approval + review queue work remotely; cloud push/pull already wired (`_push_apply_attempt_to_cloud`, `_pull_approved_from_cloud`).

**Recommendation:** validate **P1–P5 locally first** (residential IP, clean numbers, no proxy variable confounding the results). Move to Render only once the agent works locally **and** you've budgeted residential-proxy egress + persistent disk + cookie-seeding. Cheapest viable **hybrid:** orchestration / scheduling / observability / approval on Render, browser egress through a residential proxy (or a home-network runner that keeps a residential IP). Do not lift an 11% agent onto a paid server + hostile IP at the same time.

---

# Priority 1 — Instrumentation & Success-Rate Visibility · Impact: ★★★★★ (prerequisite)

**Why first:** analytics are `None` for all 46 attempts; failure data is a bare status string. You can't improve, or prove you improved, a number you can't see. Cheapest high-leverage work in the plan.

### 1a. Record analytics on EVERY attempt, not just success
Today `record_application_analytics` runs only inside the `if result:` success branch (`orchestrator.py:646-651`). Move it to fire on **all** outcomes (success, block, error), capturing: `status`, `applicationMethod`, ATS vendor, `atsScore`, `resumeVersion`, step reached, blocker reason, duration, attempt#. `record_apply_attempt` already persists status/detail/attempt_count (`state_manager.py:273`) — extend the same call site.

### 1b. Success-rate + funnel report (`stats` command)
Add a `main.py` sub-command / `orchestrator` method that computes and prints:
- Funnel: discovered → scored/approved → attempted → **submitted**.
- Attempt success rate, and rate **per ATS vendor** and **per source** (LinkedIn/Indeed/Jobright).
- Failure histogram by status cluster (the table above, live).
- Wasted-retry count (attempts on jobs that never succeed).

This turns `extra_json` into a dashboard and makes every later priority measurable.

**Maintenance win folded in:** fixes the silent analytics gap and gives regression visibility for all later work.

---

# Priority 2 — Blocker Classification + Circuit Breaker · Impact: ★★★★★

**Why:** jobs are re-attempted up to **17×** with no gate (`orchestrator.py:629` loop). That effort produces nothing and buries real signal. Classifying failures and stopping unwinnable retries both **saves runtime** and **surfaces the actual blockers** to act on.

### 2a. Classify every failure
Map each `apply_last_status` to a **class**: `transient` (network/timeout/ATS 5xx → retry worth it), `auth_required` (`workday_session_expired`, `brassring_login_required` → route to Priority 3), `permanent` (`bad_ats_url`, unsupported vendor → stop), `needs_human` (`submit_not_found`, `linkedin_stuck_on_required_field` → review queue). Store the class in `extra_json`.

### 2b. Circuit breaker in the apply loop
Before attempting, skip jobs whose class is `permanent`/`auth_required`(until re-auth) or whose `apply_attempt_count` exceeds a per-class cap (e.g. transient=3, needs_human=1). Route skipped jobs to the existing `review_queue.py` with the blocker reason instead of silently re-failing.

**Result:** attempt budget is spent on winnable jobs; the review queue becomes the list of "here's exactly why these 41 failed," which directly feeds Priorities 3–5.

---

# Priority 3 — Session / Auth Preflight + Re-auth · Impact: ★★★★☆

**Why:** 17% of failures are auth/session (`workday_session_expired` 4, `brassring_login_required` 3). The infra already exists — `reauth.py`, `orchestrator.prepare_sessions` (`orchestrator.py:488`), `preflight_approved` (`:421`) — but it isn't catching these before the apply attempt.

### 3a. Pre-apply session check
Before driving the form, detect the login/session state (reuse `_looks_like_login_wall`, `jobright.py:2054`; the Workday expired-flag path, `jobright.py:300`). If unauthenticated, do **not** burn an apply attempt — mark `auth_required` and hand to re-auth.

### 3b. Route to re-auth instead of failing
Wire `auth_required` jobs into `reauth.py` / `prepare_sessions` so the session is refreshed, then re-queue the apply. Recovers most of the 7 auth failures that currently dead-end.

---

# Priority 4 — Form-Completion Robustness (existing vendors) · Impact: ★★★★★ (biggest single lever)

**Why:** this is the **66%** cluster — `external_ats_error`, `form_not_reached`, `submit_not_found`. These are jobs the agent *reached* and *couldn't finish*. Fixing completion on vendors already in use is the highest measured lift available.

Targeted, data-driven fixes (each guarded by a characterization test, see 6a):
- **`submit_not_found` (5):** harden submit detection in `_confirm_and_submit` (`jobright.py:3545`) — broaden button/role/aria patterns, handle multi-step "Review → Submit", scroll-into-view, disabled-until-valid states.
- **`form_not_reached` (7):** harden the apply-entry path `_click_ats_apply_button` (`jobright.py:2618`) and post-chooser handling (`_workday_handle_post_chooser`, `:2878`) — cover the "Apply" → intermediate chooser → form transitions that currently stall.
- **`external_ats_error` (15):** instrument *where* in the flow these throw (Priority 1 gives you the step), then fix the top 2–3 concrete causes. This is a bucket that needs the data from P1 to decompose — do it *after* P1 is live. **OSS quick win:** a share of these are likely bot-detection (Cloudflare / WebDriver leak) rather than logic bugs — swap the Playwright import for **patchright** (`pip install patchright`, drop-in `patchright.async_api`, async-compatible, Chromium). Near-zero code change, and effective here because the agent runs on a residential IP. Measure the `external_ats_error` count before/after via P1's report to confirm the lift.
- **`linkedin_stuck_on_required_field` (1) / required-field gaps:** improve `ResumeFieldFixer.fix_fields` (`resume_helper.py:109`) coverage of required custom fields.

**Sequencing:** P1 must land first so `external_ats_error` (the biggest single bucket) can be broken into fixable causes rather than guessed at.

**Reliability bugfix folded in (was "maintenance," it's actually functional):** `ResumeFieldFixer.__init__` defaults to a **relative** `state/profile.json` (`resume_helper.py:92`), resolved against CWD — a wrong CWD yields empty fields and a failed apply. Fix path resolution here (or via the Profile Service, 7c).

---

# Priority 5 — AI Recovery Fallback (bounded) · Impact: ★★★★☆

**Why elevated from a "spike" to a real feature:** the residual form-completion failures (broken selector, unexpected question, new step) in the 66% cluster are exactly what an AI recovery layer catches when deterministic selectors fall through. This is the second-biggest lever after P4, and P4's deterministic fixes + P5's fallback are complementary.

- **Tool: Browser Use (Python, MIT).** Chosen over Stagehand (TS-first; Python is a secondary wrapper needing a Node sidecar) and Skyvern (AGPL-3.0 — a distribution constraint for a shipped agent). Browser Use is in-language, tops the 2026 browser-agent benchmarks, supports **Ollama** (runs on the existing local-first model cascade before cloud), and — most relevant — has a **self-healing / "domain skills"** mechanism that captures working selectors and flows per site, so a Workday or Greenhouse recovery it discovers once is replayed deterministically next time (turns a one-off LLM fix into durable coverage).
- **Bounded invocation:** only fires when a deterministic adapter returns "couldn't complete" — never drives the whole apply. Strict domain allowlist, minimal credential access, and **must** pass through the same submission gate (Priority 7b policy) — no autonomous submits.
- **Escalation option (only if a gap remains):** **Skyvern** for the hardest vision-based form-fills, run as a **separate API service** to fence the AGPL boundary. Do not adopt unless P4 + Browser Use leave measurable form-completion failures.
- **Cleaner with the thin refactor slice (6a):** the "deterministic failed → invoke recovery" seam is trivial once apply goes through the adapter interface; bolt-on is possible but messier.

---

# Priority 6 — New Vendor Adapters (coverage) · Impact: ★★★☆☆

**Why lower than it looks:** coverage raises *absolute* volume, but the measured bottleneck is completion, not reach. Worth doing once P1–P5 lift the base rate — otherwise you're adding more 11%-success surface.

- **OSS quick win — separate discovery from apply via public JSON APIs (no OAuth):** Greenhouse `GET api.greenhouse.io/v1/boards/{client}/jobs?content=true`, Lever `GET api.lever.co/v0/postings/{client}?mode=json` (supports team/location/level filters), Ashby feed (`includeCompensation=true`). Pulling postings + **canonical apply URLs** from these directly attacks `bad_ats_url` and reach failures, and gives clean, structured job data instead of scraping. This is the cheapest coverage win and improves apply reliability, not just volume.
- `greenhouse.py`, `lever.py`, `ashby.py` apply adapters — deterministic selectors first, generic fallback second.
- **Pattern reuse, not code reuse:** harvest selectors + screening-question patterns from neonwatty / AkbarDevop (200+ real applications) and re-implement in Python. Avoid ApplyPilot (AGPL-3.0).
- Use P1's per-vendor success data to pick which vendor to add next (add where you see the most *reached-but-unsupported* jobs).

---

# Priority 7 — Maintainability & Stability Substrate (the refactor) · Impact: ★★☆☆☆ direct, ★★★★☆ enabling

**Why demoted but not dropped:** parity refactor = zero direct success-rate gain, but it's the substrate that makes P5/P6 clean and stops the 3,739-line `JobrightScraper` (`jobright.py:33-3739`) from making every future fix risky. Do the **thin slice (7a)** early; defer the rest.

### 7a. Thin slice (do early — unblocks P5/P6)
Registry + `AtsApplyContext`/`AtsApplyResult` + `GenericAtsAdapter` + `ExternalApplySession`, at parity. This gives P4/P5 a clean "fill / recover / submit" seam and P6 a place to register vendors — without the full extraction.

### 7b. Full extraction (defer behind Track S)
Workday/Teamtailor/Brassring/SmartRecruiters adapters, Policy gate, Evidence store — as detailed below. Each behind a characterization test.

### Retained architecture (corrected — from prior review)

**Three-layer decomposition** (browser vs vendor de-coupling):
```
Site Scraper (LinkedIn/Indeed/Jobright)
   └─> ExternalApplySession  (binds "jobright" profile, sets extension load, launches, navigates)
          └─> AtsAdapterRegistry.pick(ctx WITH live page) ─> AtsAdapter.apply(ctx)
```
- **`ExternalApplySession`** (`session.py`, subclasses `BaseScraper`, `name="jobright"`): owns browser lifecycle; creates `AtsApplyContext` with the live `page`. Adapters never launch/close the browser.
- **`AtsAdapter`** interface: `can_handle(ctx) -> float`, `apply(ctx) -> AtsApplyResult`. Submission gate via `ctx.policy.confirm_submit(...)`.
- **`AtsApplyContext`** carries: `page`, `job`, `profile` (Profile Service, 7c), `resume_path`, `cover_letter_path`, `auto_submit`, `policy`.

**Three corrections that must be honored (verified against code):**
1. **Extension toggle is a *pre-launch* decision.** `jobright.py:258-268` sets `load_extensions=not _is_teamtailor` from a URL check *before* `_start_browser`. So `ExternalApplySession` needs a **two-phase** detect: cheap URL pre-check (sets extension on/off, Teamtailor→off) *before* launch, then full `registry.pick` after navigation.
2. **Preserve the direct-nav-with-extension path.** The migrated call sites (`linkedin.py:1265`, `indeed.py:397`, `SOURCE_MAP["external"]`) use `apply_external_ats_job` (`jobright.py:237`), i.e. direct navigation with the extension loaded — **not** the Jobright-card autofill handoff (`jobright.py:1534/1602`). Replicate `:237`; leave the card-handoff flow alone.
3. **Profile-lock concurrency.** `ExternalApplySession(name="jobright")` shares `state/sessions/jobright_profile` with `JobrightScraper`. A concurrent Jobright browser (e.g. `orchestrator.py:988` hydration) will cause the `ProcessSingleton`/`database is locked` failure CLAUDE.md warns about. Reuse the live jobright context or serialize access; never launch a second Chrome on that profile. (`_clear_profile_locks` only clears *stale* locks.)

### 7c. Profile Service (reliability + maintenance)
`src/profile_service.py` wrapping `state/profile.json` (keys: `personal_info, social_links, summary, education, certifications, work_history, skills, disclosures`). Centralize readers: `resume_helper.py:109`; `jobright.py:346/2501/3269/3384`; `linkedin.py:1521/1523`; `gap_analyzer.py:12`; `profile_enricher.py`. Owns path resolution (kills the CWD bug from P4).

### Six `JobrightScraper` call sites (migrate/keep)
1. `linkedin.py:1265` — external ATS apply → **migrate** (via `ExternalApplySession`)
2. `indeed.py:397` — external ATS apply → **migrate**
3. `SOURCE_MAP["external"]` (`orchestrator.py:37`) → **migrate**
4. `linkedin.py:1047` — resume tailoring → keep
5. `orchestrator.py:461` — browser/extension setup → keep
6. `orchestrator.py:988` — hydration → keep

---

## Priority Summary (ordered by measured success-rate impact)

| # | Priority | Track | Success Impact | Attacks | Coupling |
|---|---|---|---|---|---|
| 1 | Instrumentation & visibility | S | ★★★★★ (prereq) | can't-measure | independent |
| 2 | Blocker class + circuit breaker | S | ★★★★★ | 17× wasted retries | independent |
| 3 | Session/auth preflight + re-auth | S | ★★★★☆ | 17% auth failures | independent |
| 4 | Form-completion robustness | S | ★★★★★ | 66% completion failures | jobright internals |
| 5 | AI recovery fallback (bounded) | S | ★★★★☆ | residual completion | cleaner post-6a |
| 6 | New vendor adapters | S/M | ★★★☆☆ | coverage/volume | needs registry (7a) |
| 7 | Refactor substrate (thin→full) | M | ★★☆☆☆ direct | risk/velocity | — |

**Recommended sequence:** 1 → 2 → 3 → (7a thin slice) → 4 → 5 → 6 → 7b. P1 first is non-negotiable: it decomposes the 15 `external_ats_error` failures into fixable causes and proves whether every later change actually moved the 11%.

---

## Work Split: Claude Code vs Antigravity

**Principle:** split by **file ownership**, not by priority — the two agents must never edit the same files. Claude Code owns the coupled core (the 3,739-line monolith + shared orchestrator/state) and the **contracts**; Antigravity owns **net-new isolated modules** that only *import* those contracts. This keeps merge conflicts near-zero.

### Claude Code owns — coupled core + contracts (needs deep monolith context)
| Priority | Files (exclusive) |
|---|---|
| **P1** Instrumentation | `state_manager.py`, `orchestrator.py`, new `stats` command in `main.py` |
| **P2** Blocker classifier + circuit breaker | `orchestrator.py` apply loop, new `src/blocker_classifier.py`, `state_manager.py` |
| **P3** Session/auth preflight | `reauth.py`, `orchestrator.py`, auth helpers in `jobright.py` |
| **7a** Thin refactor slice (**the contract**) | new `src/sources/adapters/{base,context,registry,session,generic}.py` |
| **P4** Form robustness | `jobright.py` internals (`_confirm_and_submit`, `_click_ats_apply_button`, `_workday_handle_post_chooser`), `resume_helper.py` |

### Antigravity owns — greenfield, isolated modules (builds against Claude's contracts)
| Priority | Files (all new — no overlap) | Dependency |
|---|---|---|
| **P6** ATS discovery via public APIs | new `src/discovery/ats_api.py` (Greenhouse/Lever/Ashby JSON clients + normalizer) | ✅ done (PR #29) |
| patchright spike | `src/discovery/patchright_spike.py` | ✅ done (PR #29) |
| **P5** Browser Use recovery | new `src/sources/adapters/recovery_browseruse.py` | 🔒 gated on 7a contract |
| **P6** Vendor apply adapters | new `src/sources/adapters/{greenhouse,lever,ashby}.py` | 🔒 gated on 7a contract |

### NEW Antigravity handoffs — unblocked now (isolated, no core-file edits)
Antigravity is idle after PR #29 but its next big items (P5, P6-apply) are gated on Claude's 7a contract. These are net-new modules it can build **immediately** in parallel, all feeding P4/P5 form-completion (the 66% cluster + the jobright 0% path):

| Task | New file(s) | Why it raises success rate |
|---|---|---|
| **Screening-question answer bank** | `src/answers/question_bank.py` | Maps common ATS screening questions → profile-derived answers (work auth, sponsorship, salary, EEO). Directly attacks `linkedin_stuck_on_required_field` + unfilled required fields. Reads profile read-only; no core edits. |
| **Selector/pattern library** | `src/adapters_patterns/ats_selectors.py` (data-only) | Harvest submit/apply/field selectors + question patterns from neonwatty / AkbarDevop (200+ real apps), as a pure Python data module the P4 fixes and P6 adapters consume. |
| **patchright benchmark vs real domains** | extend `patchright_spike.py` | Run the spike against the actual domains behind the 15 `external_ats_error` failures (Claude will supply the domain list from P1 data) → quantifies expected P4 lift before wiring. |
| **Discovery → apply-URL hardening** | extend `src/discovery/ats_api.py` | Emit canonical apply URLs + dedupe, so the pipeline stops hitting `bad_ats_url`/reach failures. Add fixture-based tests against recorded API responses. |
| **Evidence store (P5b) module** | new `src/adapters/evidence.py` (pure builder; Claude wires the `state_manager` write) | Structured per-attempt evidence (vendor, resume hash, field summary, blocker). Antigravity builds the record builder; Claude owns the DB write. |

**Handoff contract for these:** all are new files that only *import* existing read-only helpers (profile, config). None may edit `orchestrator.py` / `state_manager.py` / `jobright.py` / `reauth.py` / `base.py`. Where a task needs a core write (evidence persistence), Antigravity delivers a pure function and Claude wires the single call site.

### Coordination rules
- **Antigravity must not edit** `orchestrator.py`, `state_manager.py`, `jobright.py`, `reauth.py`, `base.py`, or any file in Claude's table. If it needs a change there, it requests a stub/interface from Claude — Claude is the single writer of the core.
- **Claude lands the contract first.** Sequence adjusts for parallelism: Claude does **P1 + 7a contract** first and merges to the integration branch (tag "contract-v1"); Antigravity's gated work rebases onto that. Antigravity's day-1 work (discovery APIs, patchright) needs no gate.
- After contract-v1: Claude runs P2 → P3 → P4 while Antigravity runs P5 + P6 vendor adapters — fully parallel, disjoint files.

---

## Branching & Coordination (avoid clobbering)

Two agents in the **same working directory will clobber each other** — branch switches discard the other's uncommitted edits. Use **separate git worktrees** (separate directories, one shared repo) and commit early. Honors CLAUDE.md: never push to `main`, always a feature branch, never `--no-verify`.

**Setup (run once):**
```bash
cd /path/to/job-agent
git checkout main && git pull
git checkout -b feat/success-rate           # integration branch
git push -u origin feat/success-rate
```

**Claude Code worktree:**
```bash
git worktree add ../job-agent-core -b feat/sr-core feat/success-rate
# work in ../job-agent-core ONLY
```

**Antigravity worktree** (create after Claude merges contract-v1 for gated work; day-1 work can start immediately off feat/success-rate):
```bash
git worktree add ../job-agent-modules -b feat/sr-modules feat/success-rate
# work in ../job-agent-modules ONLY
```

**Flow:**
1. Each agent commits early/often on its own branch, in its own worktree.
2. Claude: `feat/sr-core` → PR → merge into `feat/success-rate` (contract-v1 first).
3. Antigravity: rebase `feat/sr-modules` on updated `feat/success-rate` after each core merge; PR → merge into `feat/success-rate`.
4. When `feat/success-rate` is green (all tests pass), PR into `main`.

**Guardrails:**
- Never two agents in the same directory. Each stays in its own worktree path.
- Never edit a file the other owns (see ownership tables). Core files = Claude only.
- Never push to `main`; never `--no-verify`; commit before switching branches.
- Rebase-on-merge (not merge commits) keeps `feat/success-rate` linear and conflicts visible early.

---

## Test Suites
- `tests/test_apply_stats.py` — funnel/success-rate computation from `extra_json` (P1).
- `tests/test_blocker_classifier.py` — status → class mapping; circuit-breaker skip logic (P2).
- `tests/test_session_preflight.py` — auth detection routes to re-auth, not a burned attempt (P3).
- `tests/test_ats_characterization.py` — parity net before any P4/P7 change (submit detection, Workday expiry, Teamtailor, Brassring, SmartRecruiters, empty-form guard).
- `tests/test_ats_registry.py`, `test_ats_generic_adapter.py`, `test_ats_workday_adapter.py` — refactor (7).
