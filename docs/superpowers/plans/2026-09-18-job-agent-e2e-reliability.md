# Job Agent E2E Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make operational failures durable, actionable, and safely classified without broadening employer-submission retry behavior.

**Architecture:** Extend the existing `RunLog` JSONL audit stream with bounded redacted boundary-failure records. Cloud sync, host readiness, provider, and session code consume the shared record. Classification is not retry authorization; the latter remains explicitly allowlisted and excludes submissions and `/api/action`.

**Tech Stack:** Python, asyncio, httpx, urllib, pytest, systemd, SQLite, JSONL.

**Spec:** `docs/superpowers/specs/2026-09-18-job-agent-e2e-reliability-design.md`

## Global Constraints

- ACES-387 is under ACES-18; ACES-284 owns breaker decay/re-arm policy.
- Preserve isolated browser profiles and never access the main Chrome profile.
- Never retry employer submissions or unresolved `submission_unverified`. Never transport-retry an ambiguous `/api/action` call; only a durable status obligation carrying the same server-deduplicated idempotency key may be replayed by a later run.
- Never log secrets, credentials, URLs, query strings, headers, response bodies, personal data, or raw prompts.
- Preserve dashboard payloads/headers, database schema, result tokens, and ledger ownership.
- No live submissions, CAPTCHA/2FA bypasses, production resets, or destructive operations.

### Task 1: Safe failure description and durable journal

**Files:** Create `src/operational_failure.py`, `tests/test_operational_failure.py`; modify `src/events.py`, `tests/test_events.py`.

**Interfaces:** `describe_failure(operation: str, endpoint_class: str, exc: BaseException) -> dict[str, object]`; `is_retry_authorized(operation: str, *, idempotent: bool, state_changing: bool, submission_state: str | None, attempts: int, max_attempts: int, breaker_allows: bool) -> bool`; `RunLog.emit("boundary_failure", **record)`.

- [ ] **Step 1: Write failing contract tests.**

```python
def test_empty_timeout_has_safe_nonblank_record():
    rec = describe_failure("cloud_pull_approved", "dashboard_read", TimeoutError(""))
    assert rec["kind"] == "timeout"
    assert rec["message"] == "timeout"

def test_nested_cause_redacts_url_and_secret():
    rec = describe_failure("cloud_sync_jobs", "dashboard_sync", RuntimeError("https://x/?token=secret"))
    assert "https://" not in rec["message"]
    assert "secret" not in rec["message"].lower()

def test_action_is_never_retry_authorized():
    assert not is_retry_authorized("cloud_action", idempotent=False, state_changing=True, submission_state=None, attempts=1, max_attempts=2, breaker_allows=True)
```

- [ ] **Step 2: Verify RED.** Run `.venv/bin/python3 -m pytest tests/test_operational_failure.py -q`; expect missing-module failure.

- [ ] **Step 3: Implement the minimal allowlisted contract.** Use `frozenset` allowlists for operations (`cloud_pull_approved`, `cloud_sync_jobs`, `cloud_action`, `desktop_notification`) and endpoint classes (`dashboard_read`, `dashboard_sync`, `dashboard_action`, `local_notification`). Classify known timeout/DNS/connect/TLS/auth/rate-limit/quota errors, scrub the full cause chain, return the classification name for blank messages, and emit only the safe result via the existing `RunLog` JSONL sink. Do not add a database field.

- [ ] **Step 4: Verify GREEN.** Run `.venv/bin/python3 -m pytest tests/test_operational_failure.py tests/test_events.py -q`; expect PASS.

- [ ] **Step 5: Commit.** Run `git add src/operational_failure.py src/events.py tests/test_operational_failure.py tests/test_events.py` then `git commit -m "feat: add safe operational failure records"`.

### Task 2: Cloud sync retry authorization

**Files:** Modify `src/orchestrator.py:1589-1679`; create `tests/test_cloud_sync_reliability.py`; test `tests/test_credentials.py`, `tests/test_dashboard_sync_regressions.py`.

**Interfaces:** Consume Task 1; perform one `cloud_action` attempt and bounded attempts only for explicitly authorized idempotent operations.

- [ ] **Step 1: Write failing local fake-transport tests.**

```python
@pytest.mark.asyncio
async def test_pull_retries_timeout_once_then_succeeds(orchestrator, client):
    client.get = AsyncMock(side_effect=[httpx.ReadTimeout(""), response(200, [])])
    await orchestrator._pull_approved_from_cloud()
    assert client.get.await_count == 2

@pytest.mark.asyncio
async def test_server_committed_client_timeout_action_runs_once(orchestrator, client):
    client.post = AsyncMock(side_effect=httpx.ReadTimeout(""))
    await orchestrator._push_status_to_cloud("job-1", "expired")
    client.post.assert_awaited_once()
```

- [ ] **Step 2: Verify RED.** Run `.venv/bin/python3 -m pytest tests/test_cloud_sync_reliability.py -q`; expect policy/event failure.

- [ ] **Step 3: Implement the operation map.** Route GET `/api/jobs/approved` as `cloud_pull_approved` / `dashboard_read`; POST `/api/sync` as `cloud_sync_jobs` / `dashboard_sync`; POST `/api/action` as `cloud_action` / `dashboard_action`. Preserve current headers. Verify server-side `/api/sync` deduplication before authorizing any POST retry; if absent, set it non-retryable and create a linked ACES-18 dashboard-contract child ticket. `/api/action` remains one transport attempt per call; durable status replay requires a stable key atomically recorded by the dashboard.

- [ ] **Step 4: Verify GREEN and commit.** Run `.venv/bin/python3 -m pytest tests/test_cloud_sync_reliability.py tests/test_credentials.py tests/test_dashboard_sync_regressions.py -q`; expect PASS. Then add modified source/tests and commit `fix: harden cloud sync diagnostics and retry safety`.

### Task 3: Timestamped scheduler observation and notification capability

**Files:** Modify `src/main.py`, `src/notifier.py:225-310`, `src/session_watchdog.py:210-380`, `tests/test_session_watchdog.py`; create `tests/test_operational_status.py`.

**Interfaces:** Produce timestamped read-only host status and `{primary_failure, secondary_conditions}` records without replacing source failure.

- [ ] **Step 1: Write failing Linux and status tests.**

```python
def test_linux_notification_never_invokes_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with patch("src.session_watchdog.subprocess.run") as run:
        outcome = _stage_prepare_sessions("linkedin")
    run.assert_not_called()
    assert outcome.secondary_conditions[0]["kind"] == "notification_unavailable"

def test_status_includes_observation_timestamp(capsys):
    show_operational_status()
    assert "observed_at" in capsys.readouterr().out
```

- [ ] **Step 2: Verify RED.** Run `.venv/bin/python3 -m pytest tests/test_session_watchdog.py tests/test_operational_status.py -q`; expect failure before capability gate/status command exists.

- [ ] **Step 3: Implement.** Gate `osascript` with `sys.platform == "darwin"`; reuse notifier dedupe for a stable `notification_unavailable` secondary key. Preserve CAPTCHA, email-code timeout, and session failure as primary. Add a read-only status command exposing `observed_at`, pinned/current branch, venv presence, config/profile presence, and timer/service result.

- [ ] **Step 4: Verify GREEN and commit.** Run `.venv/bin/python3 -m pytest tests/test_session_watchdog.py tests/test_notifications.py tests/test_operational_status.py tests/test_run_scheduled_guard.py -q`; expect PASS. Commit `fix: preserve source failures across notification gaps`.

### Task 4: Provider/session taxonomy and ordered bounded history

**Files:** Modify `src/model_client.py:860-970`, `src/reauth.py`; modify `tests/test_model_client.py`, `tests/test_provider_preflight_reconcile.py`, `tests/test_reauth_unit.py`.

**Interfaces:** Emit ordered, bounded provider history entries classified as authentication, quota, rate-limit, timeout, DNS, TLS, unavailable, malformed-output, or safe unknown.

- [ ] **Step 1: Write failing taxonomy tests.**

```python
def test_provider_history_preserves_order_and_bound(client):
    client._record_provider_failure("openrouter", socket.gaierror(-3, "dns"))
    for _ in range(client.MAX_PROVIDER_ATTEMPTS + 1):
        history = client._record_provider_failure("ollama", TimeoutError(""))
    assert history[0]["provider"] == "openrouter"
    assert len(history) == client.MAX_PROVIDER_ATTEMPTS

def test_email_timeout_remains_primary_after_notification_failure():
    out = classify_session_failure(TimeoutError("mail timeout"), notification_error=OSError("missing"))
    assert out.primary_failure["kind"] == "email_code_timeout"
    assert out.secondary_conditions[0]["kind"] == "notification_unavailable"
```

- [ ] **Step 2: Verify RED, implement mappings, verify GREEN.** Run `.venv/bin/python3 -m pytest tests/test_model_client.py tests/test_provider_preflight_reconcile.py tests/test_reauth_unit.py -q`; expect the new API to fail. Implement deterministic exception/status mappings only, retain safe unknowns, and write history to run journals/in-memory reports rather than job rows. Re-run the same command; expect PASS.

- [ ] **Step 3: Commit.** Add source/tests and commit `fix: classify provider and session failures`.

### Task 5: Fixture-gated adapter truthfulness and restart safety

**Files:** Modify only demonstrated vendor files under `src/sources/`, `src/sources/adapters/session.py`, their targeted tests, and `tests/test_possible_submit_preservation.py`.

**Interfaces:** Consume existing `AtsApplyResult` and ledger; preserve `submission_unverified` through restart while distinguishing invalid/missing URL, DNS, browser navigation, and missing-submit outcomes.

- [ ] **Step 1: Create one ACES-18 child ticket per demonstrated vendor issue.** Each ticket cites source line, failing fixture, current token, target token, and submission impact; link it to ACES-387. Never open a ticket from an unverified log alone.

- [ ] **Step 2: Write the failing fixture and restart proof.**

```python
@pytest.mark.asyncio
async def test_restart_with_unverified_record_never_clicks_submit(session, ledger, page):
    ledger.complete("job-1", "attempt-1", verified=False)
    result = await session.apply(job("job-1"), page)
    assert result.status == "submission_unverified"
    page.locator("button[type=submit]").click.assert_not_awaited()
```

- [ ] **Step 3: Verify RED, minimally fix, verify GREEN.** Run `.venv/bin/python3 -m pytest tests/test_possible_submit_preservation.py tests/test_generic_submit_dispatch_truth.py tests/test_recovery_submit_dispatch_truth.py -q` plus the confirmed adapter fixture. Before the edit the new test must fail; after it must pass. Do not modify ACES-284 breaker behavior.

- [ ] **Step 4: Commit each vendor separately.** Add confirmed vendor source/tests and commit `fix: preserve <vendor> failure truthfulness`.

### Task 6: E2E-safe validation, Jira reconciliation, and PR

**Files:** Modify `docs/EXCEPT_SWALLOW_AUDIT_2026-09-17.md` only to link confirmed work.

- [ ] **Step 1: Run targeted suite.**

```bash
.venv/bin/python3 -m pytest tests/test_operational_failure.py tests/test_cloud_sync_reliability.py tests/test_events.py tests/test_credentials.py tests/test_dashboard_sync_regressions.py tests/test_session_watchdog.py tests/test_notifications.py tests/test_operational_status.py tests/test_model_client.py tests/test_provider_preflight_reconcile.py tests/test_reauth_unit.py tests/test_possible_submit_preservation.py tests/test_generic_submit_dispatch_truth.py tests/test_recovery_submit_dispatch_truth.py tests/test_receipt_dom_truthfulness.py tests/test_state_transitions.py -q
```

Expected: PASS.

- [ ] **Step 2: Run read-only host and broad validation.** Run `.venv/bin/python3 src/main.py autopilot-status`, `.venv/bin/python3 src/main.py session-status`, the new status command, then `.venv/bin/python3 -m pytest -q --continue-on-collection-errors`. Do not invoke discovery or apply.

- [ ] **Step 3: Reconcile Jira and open PR.** Attach commits/test evidence to ACES-387; link ACES-284 and vendor children; transition to `PR Open`; open `ACES-387: harden Job Agent E2E reliability` referencing ACES-387 and ACES-18. Document external remaining risks: dashboard availability, host DNS/network, valid credentials, human 2FA, and CAPTCHA.

## Plan self-review

- Tasks 1-2 cover durable safe records and explicit retry authorization; Task 3 covers scheduler/notification precedence; Task 4 covers provider/session attribution; Task 5 protects adapters and restart safety; Task 6 verifies and delivers.
- Every retry decision requires operation allowlist, idempotency, non-state-change, submission state, attempt cap, and ACES-284 breaker decision. Transport classification alone never authorizes a retry.
- Adapter edits are explicitly fixture- and ticket-gated so no speculative selector changes enter the foundation PR.
