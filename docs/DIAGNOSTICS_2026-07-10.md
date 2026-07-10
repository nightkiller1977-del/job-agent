# Diagnostics — 2026-07-10

Investigation into two reported failures: (1) the agent stopped adding new jobs from
sources, and (2) the agent breaks during the apply phase. Both were reproduced against
the live SQLite state (`state/jobs.db`), the reauth event log (`state/agent_status.json`),
and the scheduled-run log (`state/agent.log`).

At time of investigation: **70 discovered / 44 approved / 7 applied / 269 skipped.**

---

## Problem 1 — Not adding jobs from sources

### Root cause A (primary): scheduled runs abort at a credential preflight

`src/main.py` gates every run behind `preflight_env_check()`. For `discover`/`apply`
**without** `--source`, it validates **all four** sources' credentials. The required
key set for usajobs was:

```python
"usajobs": ["USAJOBS_USERNAME", "USAJOBS_PASSWORD"]   # WRONG
```

But the usajobs scraper (`src/sources/usajobs.py`) actually reads `USAJOBS_EMAIL` /
`USAJOBS_PASSWORD`, and `.env` only defines those. `USAJOBS_USERNAME` never exists, so
the preflight fails and calls `sys.exit(1)` **before any source is scraped**:

```
[PREFLIGHT FAIL] Missing credentials for usajobs: USAJOBS_USERNAME is not set.
[PREFLIGHT FAIL] 1 credential(s) missing. Fix them in .env and re-run.
```

This is exactly what the scheduled 7 AM launchd job (`com.jobagent.daily`) logged on
every run — so the automated discover never scraped anything. Interactive runs that
passed `--source jobright`/`--source linkedin` worked only because they validated a
single source and bypassed the usajobs check.

**Fix:** align the required keys to what the scraper reads — `USAJOBS_EMAIL` /
`USAJOBS_PASSWORD` — in both `src/main.py` (`_SOURCE_CREDS`) and `src/commander.py`
(`CRED_KEYS`). Verified: all-source preflight now passes.

### Root cause B (secondary): session expiry + unreliable automated reauth

When a source session expires, background/cron discover raises `AuthFailedError`, and
`ReauthManager._reauth_automated()` retries `_auto_login()`. The reauth log showed a
storm of **100 consecutive `jobright automated failed — _auto_login returned False`**
events between 2026-06-29 23:30 and 2026-06-30 01:03 — meaning jobright could not
self-heal and returned 0 jobs for that whole window. When sessions are valid (verified
by live headed runs on 2026-07-10: jobright +8, linkedin finding results), discovery
works normally.

_Not changed in this PR_ (higher-risk, follows the Ollama→Claude→Codex cascade policy).
Tracked as follow-up. Mitigations to consider: cap reauth retries with backoff, and
detect the "`_auto_login` returned True but session still redirects to /login" false
positive.

### Contributing factor: dedup shrink

`already_seen()` filters previously-seen jobs, so repeated runs against the same
listings yield progressively fewer "new" jobs (observed 7 → 7 → 1 → 1 across LinkedIn
queries). Expected behavior, but it compounds the perception of "not adding jobs" when
sources return stale listings.

---

## Problem 2 — Breaks on applying

### Root cause A (primary, code defect): one bad resume aborts the entire apply batch

`JobrightScraper.apply_external_ats_job()` calls `check_ats_readability(pdf, keywords)`
where `keywords = profile.skills + tailored.missing_keywords`. `missing_keywords` are, by
definition, terms the ATS analysis found **absent** from the résumé — so a strict
"all keywords must be present" check fails on nearly every job. On failure it raised
`ATSReadabilityError`, jobright re-raised it, and `orchestrator.apply_approved()`
re-raised it **again** (with no outer handler) — crashing the whole command. Every
remaining approved job in the queue went unattempted.

The comment claimed this propagation would "trigger self-healing," but nothing catches
it to self-heal; it simply terminated the run.

**Status:** `main` already carries the corrected handler (merged separately): the apply
loop now inspects the error and, on a **keyword mismatch**, records a per-job
`ats_failure` and **continues** to the next job; only a genuinely **unreadable PDF**
(no extractable text / parse failure) pauses the loop. `check_ats_readability` still
raises so the orchestrator can make that per-job decision. This PR keeps that behavior
and adds the missing regression coverage.

**This PR adds:** `tests/test_apply_ats_nonfatal.py` — asserts a two-job batch where
job 1 raises a keyword-mismatch `ATSReadabilityError` still attempts and applies job 2
(i.e. the batch is not aborted).

Note: `apply` without `--source` also hit the usajobs preflight `sys.exit(1)` (Problem 1
Root cause A), so the credential fix repairs the scheduled apply run as well.

### Root cause B (environmental/robustness): browser launch + expired sessions

Dominant recorded apply-attempt reasons in `jobs.db`:

| Reason | Meaning |
|---|---|
| `BrowserType.launch_persistent_context: Timeout 180000ms exceeded` | Chrome could not launch within 180s (profile lock / main Chrome running) |
| `Target page, context or browser has been closed` | Chrome crashed/closed mid-flow |
| `Connection closed while reading from the driver` | Playwright driver died |
| `linkedin_login_required` | LinkedIn session expired → needs `prepare-sessions --source linkedin` |
| `missing_ats_url` | jobright posting exposed no company ATS URL |

These are session/environment issues (the user's main Chrome was running — 14 processes
observed — and each approved job launches a fresh persistent context). Operational
mitigations: run `prepare-sessions --source linkedin` to refresh the authwall'd session;
prefer running apply with the personal Chrome closed. Deeper robustness (serial launch
guard, retry-with-backoff on launch timeout) is tracked as follow-up.

---

## Changes in this PR

- `src/main.py`, `src/commander.py` — fix usajobs credential key names (`USAJOBS_EMAIL`).
  **This is the primary fix** — it unblocks every scheduled all-source run.
- `tests/test_apply_ats_nonfatal.py` — regression test proving a keyword-mismatch
  `ATSReadabilityError` no longer aborts the apply batch (guards main's handler).
- `scripts/night_run.sh`, `scripts/com.jobagent.night.plist.template` — automatic nightly
  discover + apply --auto-submit at 11:00 PM (installed as launchd agent
  `com.jobagent.night`).
- `src/sources/jobright.py` — clarifying comment only; behavior matches `main`.

## Recommended follow-ups (not in this PR)

- Reauth reliability: retry cap + backoff; fix `_auto_login`-returns-True-but-still-logged-out.
- Browser launch robustness: guard against concurrent/stale persistent-context launches.
- Refresh the LinkedIn session (`prepare-sessions --source linkedin`).
