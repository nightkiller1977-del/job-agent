# Runbook — Mac verification & `USE_ADAPTER_REGISTRY` flag flip

> Run this ON THE MAC, at a terminal, with the residential connection — the whole
> reliability stack (PRs #44–#53) is merged but **dormant**: the flag defaults OFF and
> nothing changes until you flip it. Rollback at any point = unset the env var.
>
> Time: ~30–45 min for stages 0–3; stage 4 runs overnight.

---

## What the flag does

`USE_ADAPTER_REGISTRY=1` (also `true|yes|on`) reroutes `apply_external_ats_job` — the
single funnel used by LinkedIn, Indeed, and `external` jobs — through the adapter
registry (`ExternalApplySession` → Workday/Microsoft/BrassRing/SmartRecruiters/
Teamtailor/Greenhouse/Lever/Ashby adapters → generic fallback) instead of the legacy
3,800-line path. With it come the merged reliability guarantees:

- `applied` **only** on a verified receipt; a bare submit click records
  `submission_unverified` and blocks blind retries until reconciled
- duplicate-submit protection (`state/apply_ledger.json`)
- policy gate: nothing (including LLM recovery) submits without authorization
- per-run event log (`state/runs/<run_id>.jsonl`), notifications, auth→re-auth routing
- all-owners profile lock (a live second Chrome is refused, never pkill'ed)

**Scope note:** the flag is a **global boolean** — it flips all vendors at once. The
"per-vendor canary" from the plan is achieved operationally with `apply --job-id /
--source / --company / --limit`, not by the flag.

**Known limitation going in:** the four CTA vendors' field selectors are heuristic and
un-tuned; expect `review_ready` / `submit_not_found` there at first. That is correct
behavior (form reached, submit withheld) — tune selectors from what you observe.

---

## Stage 0 — Preflight (5 min, no browser)

```bash
cd /Users/alarkins/Dev/Projects/job-agent
git checkout main && git pull
source .venv/bin/activate
python -m pytest -q --continue-on-collection-errors   # expect ~480+ passed
python src/main.py stats                              # BASELINE — save this output
python src/main.py preflight                          # what's ready vs blocked
python src/main.py session-status                     # session health per source
```

- [ ] Record the baseline `stats` numbers (attempts, success rate, failure clusters).
- [ ] If `session-status` shows expired/missing for a source you'll test:
      `python src/main.py prepare-sessions --source <source>` first.
- [ ] Close any manually-opened Chrome on the agent profiles (the new lock will
      *refuse* to run if a live process holds `state/sessions/jobright_profile` —
      that refusal is working-as-intended, not a bug).

## Stage 1 — Fill-only dry run (no submits possible)

`--no-auto-submit` + the policy gate means the adapter path may reach and fill forms
but **cannot submit** — the safest first contact with real DOMs.

```bash
USE_ADAPTER_REGISTRY=1 python src/main.py apply --no-auto-submit --limit 3
```

Verify, per attempt:

- [ ] Console shows `routing via adapter registry (USE_ADAPTER_REGISTRY=1)` and
      `adapter=<vendor>` — the **right adapter** was selected for the URL.
- [ ] Outcomes are `review_ready` (reached + filled + withheld) or an honest blocker
      (`<vendor>_login_required`, `workday_session_expired`, `form_not_reached`,
      `captcha`) — **never** `applied`.
- [ ] Event log exists and is coherent:
      `tail -50 state/runs/$(ls -t state/runs | head -1)`
      → one `attempt_started` … `attempt_finished` pair per job, host-only URLs,
      no PII/emails in any line.
- [ ] Auth walls produced a `reauth_directive` in the result analytics and a
      human-action notification arrived (Telegram/macOS). To actually refresh the
      portal session, target THAT job (prepare-sessions filters on the job's
      discovery source, so `--source jobright` won't open a LinkedIn-origin job's
      portal — the directive's remediation hint targets this correctly):
      `python src/main.py prepare-sessions --source <origin-source> --company <company>`
      then sign in to the ATS portal in the opened persistent profile.
- [ ] `state/apply_ledger.json` has **no** `receipt_verified`/`submission_unverified`
      entries (nothing was clicked).

If an adapter mis-selects or a fill is obviously wrong, stop here and tune
`src/adapters_patterns/ats_selectors.py` — everything below builds on this stage.

## Stage 2 — Single-job supervised canary (first real submit)

Pick ONE approved job on the **best-covered vendor first** (Greenhouse or Lever;
Workday second). Watch the browser the whole time.

```bash
USE_ADAPTER_REGISTRY=1 python src/main.py apply --auto-submit --job-id <JOB_ID>
```

- [ ] The submit was clicked only after fill completed (watch it happen).
- [ ] **Receipt truth check (the critical one):** confirm in the ATS / your email
      that the application actually went through, then compare with what the agent
      recorded:
      - agent says `applied` **and** ATS confirms → correct ✅
      - agent says `submission_unverified` **but** ATS confirms → receipt heuristics
        missed this vendor's confirmation page. The application is fine; add the
        vendor's confirmation URL/copy pattern to `src/sources/adapters/receipt.py`,
        and clear the block for this posting (see *Reconciliation* below).
      - agent says `applied` **but** ATS shows nothing → **stop, do not proceed** —
        a false-positive receipt is the exact failure this stack exists to prevent.
        Capture the post-submit URL/page text and tighten `receipt.py`.
- [ ] Dedup proof: a verified canary flips the job's status to `applied`, so a plain
      re-run simply selects nothing (status-level protection). To exercise the
      **ledger** layer, re-approve the same job (dashboard, or set its status back to
      `approved`) and re-run → `duplicate_application_prevented`, browser never
      launches.
- [ ] While an apply is running, start a second one in another terminal → the second
      exits with `profile_locked` and the first's Chrome survives. (Lock proof.)

## Stage 3 — Small per-vendor batches

**Note:** `--source` filters by *discovery source* (linkedin/indeed/jobright), NOT by
ATS vendor — a LinkedIn batch can mix Greenhouse, Lever, Workday, and Easy Apply jobs.
To canary a specific ATS vendor, pick jobs whose URLs are on that vendor (check
`preflight` output / the dashboard) and target them with `--job-id` or `--company`:

```bash
# per-ATS-vendor canary (preferred): explicit jobs on the target vendor
USE_ADAPTER_REGISTRY=1 python src/main.py apply --auto-submit --job-id <ID_ON_VENDOR>

# or small mixed batches per discovery source, then split results per vendor
# using the `vendor` field in the run's JSONL events:
USE_ADAPTER_REGISTRY=1 python src/main.py apply --auto-submit --source linkedin --limit 5
python src/main.py stats        # after each batch
```

- [ ] For each vendor: spot-check one receipt against the ATS, skim the run's JSONL
      for anomalies, and confirm failure statuses are *specific*
      (`workday_session_expired`, not `error`).
- [ ] `stats` failure clusters should shift from `external_ats_error` noise toward
      actionable categories; wasted retries should stay near zero (circuit breaker +
      ledger).
- [ ] Any `submit_unverified_unresolved` blocks → reconcile (below) before rerunning
      that job.

**Go / no-go:** proceed to Stage 4 only if the adapter path's submit rate on these
batches is **≥ the legacy baseline** from Stage 0 and there were **zero** duplicate
submissions and **zero** false `applied`.

## Stage 4 — Flip for scheduled runs

The launchd night run gets its env from `scripts/night_run.sh`, so add the flag there
(both invocations inherit it):

```bash
# scripts/night_run.sh — after the PY/LOG definitions:
export USE_ADAPTER_REGISTRY=1
```

- [ ] Next morning: `tail -100 state/agent.night.log`, `python src/main.py stats`,
      and skim the newest `state/runs/*.jsonl`.
- [ ] Watch the first 2–3 nights. Notifications now dedupe per posting, so a quiet
      night is a good night; `HUMAN_ACTION_REQUIRED` pings are your work queue.

## Rollback (any stage)

**FIRST, reconcile the ledger** — the legacy path does NOT consult
`state/apply_ledger.json`, so any posting left in `submission_unverified` or
`submit_in_progress` loses its double-submit protection the moment you unset the flag
and could be blindly re-applied by the legacy path:

```bash
grep -E "submission_unverified|submit_in_progress" state/apply_ledger.json
# resolve each via the Reconciliation procedure below BEFORE unsetting the flag
```

Then:

```bash
unset USE_ADAPTER_REGISTRY                 # interactive
# and/or remove the export line from scripts/night_run.sh
```

The legacy path resumes immediately. The event logs are additive — nothing else to
undo.

---

## Reconciliation — clearing a `submit_unverified_unresolved` block

A posting is blocked because a submit was clicked but no receipt was observed.
**Never just delete the entry without checking** — that re-arms a possible duplicate.

1. Check the ATS account / email for a confirmation of that application.
2. **It went through** → mark the job applied and keep the dedupe:
   in `state/apply_ledger.json`, change that key's `"phase"` to `"receipt_verified"`,
   and set the job's status: the agent will treat it as applied and never resubmit.
   (Also: improve `receipt.py` so the next one self-verifies.)
3. **It did not go through** → delete that key from `state/apply_ledger.json`; the
   job becomes attemptable again.

## Quick triage table

| Symptom | Meaning | Action |
|---|---|---|
| `profile_locked` | another live process owns the profile | close it / let it finish; it is **not** killed by design |
| `<vendor>_login_required`, `workday_session_expired` | portal session expired | `python src/main.py prepare-sessions --source <origin-source> --company <company>` (targets that job's portal), sign in in the opened profile |
| `review_ready` everywhere on a CTA vendor | form reached, selectors too weak to fill/submit | tune `ats_selectors.py` for that vendor |
| `submission_unverified` but ATS confirms | receipt heuristics miss this vendor | extend `receipt.py` patterns; reconcile the ledger entry |
| `submit_in_progress` | a prior attempt crashed mid-submit | check ATS as in reconciliation; stale (>6h) markers are reported as stale |
| second apply refuses instantly | all-owners lock working | run applies sequentially |

## Artifacts to keep from the verification

- Stage 0 baseline `stats` output vs Stage 3/4 `stats` (the before/after)
- One JSONL run file per vendor demonstrating a clean lifecycle
- Notes on any `receipt.py` / `ats_selectors.py` tunings made — commit them as PRs
