# Session Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make unattended apply runs automatically recover source-login sessions, re-arm affected jobs in the same run, and suppress duplicate human alerts across scheduler processes without weakening submission safety.

**Architecture:** Keep the existing `StateManager`, `ReauthManager`, session watchdog, and orchestrator boundaries. Add an automated-only reauth entry point, make the watchdog return structured preflight results, reuse `Orchestrator._unblock_session_jobs_after_reauth()` for source-own blockers, reload jobs from SQLite before reclassification, and persist notification dedupe timestamps in the existing `state/agent_status.json`. Do not import PR #120 wholesale and do not introduce blocker-intelligence/model retry behavior.

**Tech Stack:** Python 3.11/3.12, asyncio, SQLite-backed `StateManager`, JSON status/session files, pytest/pytest-asyncio, existing GitHub Actions full-suite job.

**Spec:** `docs/superpowers/specs/2026-09-13-job-agent-autonomy-design.md`

## Global Constraints

- Start implementation from the then-current `main`, not from PR #120 or `plan/job-agent-autonomy`.
- Use branch `feat/session-autonomy`.
- Security/submission correctness first; reuse existing interfaces second; simplicity third.
- TDD: every production behavior change starts with an observed failing test.
- Do not use `--no-verify`; normal pre-push hooks must pass.
- Do not touch `blocker_intelligence.py`, add model-backed blocker classification, or add adaptive retry caps in this PR.
- Do not change receipt verification, `SubmissionLedger`, or reconciliation semantics established by PR #121.
- Source-level reauth may clear only the source's own login blockers. External ATS portal blockers such as `workday_session_expired` must survive.
- Background preflight owns human escalation for its attempt. Automated reauth invoked by it must not independently notify.
- Durable notification dedupe uses existing `state/agent_status.json`; no new cloud service or database is introduced.
- PR #123 remains independent and must not be folded into this branch.

---

### Task 1: Make LinkedIn health depend on authentication cookies, not tracking cookies

**Files:**
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_session_watchdog.py`

**Interfaces:**
- Consumes: existing `check_session_health()` and `_parse_linkedin_expiry()`.
- Produces: `LINKEDIN_AUTH_COOKIE_NAMES: frozenset[str]`, `_linkedin_auth_cookie_state(path) -> tuple[bool, float | None]`, and a backward-compatible `_parse_linkedin_expiry(path) -> Optional[float]`.
- Later tasks rely on `check_session_health(["linkedin"])` returning `expired` when no authentication cookie is present even if the JSON file itself is recent.

- [ ] **Step 1: Add failing auth-cookie truth tests**

Append focused tests to `tests/test_session_watchdog.py` using a temporary `SESSIONS_DIR` and fixed `time.time()`:

```python

def _write_linkedin_session(path, cookies):
    path.write_text(json.dumps({"cookies": cookies}))


def test_linkedin_tracking_cookie_expiry_does_not_expire_valid_auth_session(tmp_path, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(sw.time, "time", lambda: now)
    monkeypatch.setattr(sw, "SESSIONS_DIR", tmp_path)
    session = tmp_path / "linkedin_chromium.json"
    _write_linkedin_session(session, [
        {"name": "li_at", "domain": ".linkedin.com", "expires": now + 30 * 86400},
        {"name": "lidc", "domain": ".linkedin.com", "expires": now - 3600},
        {"name": "UserMatchHistory", "domain": ".linkedin.com", "expires": now - 60},
    ])
    os.utime(session, (now, now))

    [health] = sw.check_session_health(["linkedin"])

    assert health.status == "healthy"
    assert "expired" not in health.detail.lower()


def test_linkedin_recent_session_without_auth_cookie_fails_closed(tmp_path, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(sw.time, "time", lambda: now)
    monkeypatch.setattr(sw, "SESSIONS_DIR", tmp_path)
    session = tmp_path / "linkedin_chromium.json"
    _write_linkedin_session(session, [
        {"name": "lidc", "domain": ".linkedin.com", "expires": now + 86400},
        {"name": "UserMatchHistory", "domain": ".linkedin.com", "expires": now + 3600},
    ])
    os.utime(session, (now, now))

    [health] = sw.check_session_health(["linkedin"])

    assert health.status == "expired"
    assert "auth" in health.detail.lower()
```

Also retain existing lone-`li_at` healthy/expired tests so compatibility is explicit.

- [ ] **Step 2: Run the new tests and observe RED**

Run:

```bash
pytest tests/test_session_watchdog.py -k "tracking_cookie_expiry or without_auth_cookie" -vv
```

Expected baseline failures:
- valid `li_at` is incorrectly expired because an older tracking cookie wins `min(expiries)`;
- tracking-only JSON is incorrectly treated as healthy when the file is recent.

- [ ] **Step 3: Implement auth-cookie-aware parsing**

In `src/session_watchdog.py`, add:

```python
LINKEDIN_AUTH_COOKIE_NAMES = frozenset({"li_at", "liap", "li_rm"})


def _linkedin_auth_cookie_state(session_path: Path) -> tuple[bool, Optional[float]]:
    try:
        data = json.loads(session_path.read_text())
        cookies = [
            c for c in data.get("cookies", [])
            if "linkedin" in str(c.get("domain", "")).lower()
            and str(c.get("name", "")) in LINKEDIN_AUTH_COOKIE_NAMES
        ]
        if not cookies:
            return False, None
        expiries = [float(c["expires"]) for c in cookies if float(c.get("expires", -1) or -1) > 0]
        if not expiries:
            return True, None
        return True, (min(expiries) - time.time()) / 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, None


def _parse_linkedin_expiry(session_path: Path) -> Optional[float]:
    _has_auth, expiry = _linkedin_auth_cookie_state(session_path)
    return expiry
```

Update `check_session_health()` for LinkedIn to use both values:

```python
if src == "linkedin":
    has_auth_cookie, cookie_expiry_hours = _linkedin_auth_cookie_state(found)
    if not has_auth_cookie:
        results.append(SessionHealth(
            source=src,
            status="expired",
            age_hours=age_hours,
            session_path=found,
            detail="LinkedIn session has no recognized authentication cookie.",
        ))
        continue
else:
    cookie_expiry_hours = None
```

Do not change age-based stale/expired behavior for other sources.

- [ ] **Step 4: Run the session watchdog suite GREEN**

Run:

```bash
pytest tests/test_session_watchdog.py -q --tb=short --asyncio-mode=auto
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/session_watchdog.py tests/test_session_watchdog.py
git commit -m "fix(session): use LinkedIn auth cookies for health"
```

---

### Task 2: Persist notification dedupe across scheduled Python processes

**Files:**
- Modify: `src/notifier.py`
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_notifier_hygiene.py`
- Modify: `tests/test_session_watchdog.py`

**Interfaces:**
- Produces: `notification_dedupe_active(key: str, dedupe_seconds: int, *, now: float | None = None) -> bool` and `record_notification_dedupe(key: str, *, now: float | None = None) -> None` in `src/notifier.py`.
- Extends: `notify_warning(..., dedupe_key: str | None = None, dedupe_seconds: int = 900)`.
- `session_watchdog._send_deep_link_notification()` uses read-then-record semantics so failed Terminal staging does not consume the dedupe window.
- PR B will later handle true concurrent-process serialization; this task proves durability across sequential scheduled processes/restarts.

- [ ] **Step 1: Add failing durable-dedupe tests**

In `tests/test_notifier_hygiene.py`, add a direct persistence test and a fresh-interpreter test. Use `JOBAGENT_STATUS_FILE` in the child so it points to the same temporary JSON file:

```python

def test_warning_dedupe_survives_fresh_python_process(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *a, **k: None)
    notifier._last_notification_times.clear()

    notifier.notify_warning(
        "Apply run: nothing submitted",
        "first process",
        dedupe_key="apply_nothing_submitted",
        dedupe_seconds=21600,
        desktop=False,
    )
    assert len(sent) == 1
    assert json.loads(status_file.read_text())["notification_dedupe"]

    child = subprocess.run(
        [sys.executable, "-c", (
            "import os; from pathlib import Path; import src.notifier as n; "
            "n.STATUS_FILE=Path(os.environ['JOBAGENT_STATUS_FILE']); "
            "n._send_telegram=lambda m: print('SENT'); "
            "n._desktop_notify=lambda *a, **k: None; "
            "n.notify_warning('Apply run: nothing submitted','second process',"
            "dedupe_key='apply_nothing_submitted',dedupe_seconds=21600,desktop=False)"
        )],
        capture_output=True,
        text=True,
        env={**os.environ, "JOBAGENT_STATUS_FILE": str(status_file), "PYTHONPATH": str(REPO_ROOT)},
        check=False,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert "SENT" not in child.stdout
```

Add a deep-link regression to `tests/test_session_watchdog.py` that calls `_send_deep_link_notification()` twice after clearing the in-memory cache and proves the second call is suppressed by the persisted timestamp. Preserve the existing staging-failure test: when `_stage_prepare_sessions()` returns `False`, no durable timestamp may be recorded.

- [ ] **Step 2: Run the new tests and observe RED**

Run:

```bash
pytest tests/test_notifier_hygiene.py tests/test_session_watchdog.py \
  -k "dedupe or rate_limit or staging_failure" -vv
```

Expected: `notify_warning` does not accept the new stable-key arguments and/or a fresh interpreter sends again because only `_last_notification_times` exists.

- [ ] **Step 3: Add durable dedupe helpers**

In `src/notifier.py`, add:

```python

def notification_dedupe_active(
    key: str, dedupe_seconds: int, *, now: float | None = None
) -> bool:
    ts = time.time() if now is None else now
    cache_key = f"notification:{key}"
    last_memory = float(_last_notification_times.get(cache_key, 0) or 0)
    if ts - last_memory < dedupe_seconds:
        return True
    status = _load_status()
    last_durable = float(status.get("notification_dedupe", {}).get(cache_key, 0) or 0)
    if ts - last_durable < dedupe_seconds:
        _last_notification_times[cache_key] = last_durable
        return True
    return False


def record_notification_dedupe(key: str, *, now: float | None = None) -> None:
    ts = time.time() if now is None else now
    cache_key = f"notification:{key}"
    status = _load_status()
    dedupe = status.setdefault("notification_dedupe", {})
    dedupe[cache_key] = ts
    # Bound the map so obsolete keys cannot grow forever.
    if len(dedupe) > 200:
        newest = sorted(dedupe.items(), key=lambda item: float(item[1] or 0), reverse=True)[:200]
        status["notification_dedupe"] = dict(newest)
    _save_status(status)
    _last_notification_times[cache_key] = ts
```

Extend `notify_warning`:

```python

def notify_warning(
    title: str,
    detail: str = "",
    *,
    desktop: bool = True,
    dedupe_key: str | None = None,
    dedupe_seconds: int = 900,
) -> None:
    ...
    key = f"warn:{dedupe_key}" if dedupe_key else f"warn:{title}:{detail}"
    if not notification_dedupe_active(key, dedupe_seconds):
        _send_telegram(f"⚠️ [Job Agent WARNING] {title}\nDetail: {detail}")
        record_notification_dedupe(key)
    ...
```

Keep alert-history writes independent from Telegram dedupe: `_add_alert()` still records each run's warning for observability.

- [ ] **Step 4: Make deep-link dedupe durable without suppressing staging retries**

Change `_send_deep_link_notification()` to import the two helpers. Use:

```python
key = f"deep_link:{source}"
if notification_dedupe_active(key, 12 * 3600):
    return
if not _stage_prepare_sessions(source):
    return
_send_telegram(full_msg)
_desktop_notify(...)
record_notification_dedupe(key)
```

This order is mandatory: a staging failure must not record the key, because the next watchdog pass needs another chance to stage the session flow.

- [ ] **Step 5: Run notification/watchdog suites GREEN**

Run:

```bash
pytest tests/test_notifier_hygiene.py tests/test_session_watchdog.py -q --tb=short --asyncio-mode=auto
```

- [ ] **Step 6: Commit**

```bash
git add src/notifier.py src/session_watchdog.py tests/test_notifier_hygiene.py tests/test_session_watchdog.py
git commit -m "fix(notify): persist scheduler warning dedupe"
```

---

### Task 3: Add an automated-only reauth API and structured reauth-aware preflight

**Files:**
- Modify: `src/reauth.py`
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_reauth_unit.py`
- Create: `tests/test_session_autonomy_preflight.py`

**Interfaces:**
- Produces: `ReauthManager.attempt_automated(source: str) -> bool`.
- Produces:

```python
@dataclass(frozen=True)
class ReauthPreflightResult:
    health: dict[str, SessionHealth]
    refreshed_sources: frozenset[str]
    notified_sources: frozenset[str]
```

- Produces:

```python
async def preflight_session_check_with_reauth(
    sources: list[str],
    config: dict | None = None,
    *,
    force_reauth: set[str] | None = None,
) -> ReauthPreflightResult:
```

- The function owns human escalation for this preflight. It must invoke automated reauth with escalation disabled.

- [ ] **Step 1: Add failing `attempt_automated` contract tests**

In `tests/test_reauth_unit.py`:

```python
@pytest.mark.asyncio
async def test_attempt_automated_never_uses_human_fallback(monkeypatch):
    from src.reauth import ReauthManager
    mgr = ReauthManager({})
    with patch.object(mgr, "_reauth_automated", new_callable=AsyncMock, return_value=False) as auto, \
         patch.object(mgr, "_reauth_human", new_callable=AsyncMock) as human:
        assert await mgr.attempt_automated("usajobs") is False
    auto.assert_awaited_once_with("usajobs", escalate=False)
    human.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_automated_unknown_source_is_false():
    from src.reauth import ReauthManager
    assert await ReauthManager({}).attempt_automated("unknown") is False
```

- [ ] **Step 2: Add failing preflight behavior tests**

Create `tests/test_session_autonomy_preflight.py` with fully synthetic health/reauth functions. Required cases:

```python
@pytest.mark.asyncio
async def test_successful_automated_reauth_reports_refreshed_without_human_notification(monkeypatch):
    ...
    result = await sw.preflight_session_check_with_reauth(["linkedin"], {})
    assert result.refreshed_sources == frozenset({"linkedin"})
    assert result.notified_sources == frozenset()


@pytest.mark.asyncio
async def test_failed_reauth_emits_exactly_one_human_escalation(monkeypatch):
    ...
    result = await sw.preflight_session_check_with_reauth(["linkedin"], {})
    assert result.refreshed_sources == frozenset()
    assert result.notified_sources == frozenset({"linkedin"})
    assert notifications == ["linkedin"]


@pytest.mark.asyncio
async def test_force_reauth_attempts_healthy_file_when_job_has_own_source_auth_block(monkeypatch):
    ...
    result = await sw.preflight_session_check_with_reauth(
        ["linkedin"], {}, force_reauth={"linkedin"}
    )
    assert attempts == ["linkedin"]
    assert result.refreshed_sources == frozenset({"linkedin"})
```

Also prove each source is attempted at most once even if it appears both in unhealthy health and `force_reauth`.

- [ ] **Step 3: Run the tests and observe RED**

```bash
pytest tests/test_reauth_unit.py tests/test_session_autonomy_preflight.py -vv
```

Expected: missing `attempt_automated`, `ReauthPreflightResult`, and `preflight_session_check_with_reauth`.

- [ ] **Step 4: Implement the automated-only public wrapper**

In `src/reauth.py`:

```python
    async def attempt_automated(self, source: str) -> bool:
        """Attempt only the approved stored-credential path; never notify or wait for a human."""
        if source not in AUTOMATED_SOURCES:
            return False
        return await self._reauth_automated(source, escalate=False)
```

Do not change `handle()` semantics in this task.

- [ ] **Step 5: Implement structured reauth-aware preflight**

In `src/session_watchdog.py` add the frozen result dataclass and function. The implementation must:

```python
async def preflight_session_check_with_reauth(
    sources: list[str],
    config: dict | None = None,
    *,
    force_reauth: set[str] | None = None,
) -> ReauthPreflightResult:
    from .reauth import AUTOMATED_SOURCES, ReauthManager

    ordered_sources = list(dict.fromkeys(s for s in sources if s))
    forced = set(force_reauth or ())
    health = {h.source: h for h in check_session_health(ordered_sources)}
    candidates = {
        src for src in ordered_sources
        if src in AUTOMATED_SOURCES
        and (src in forced or health.get(src) is None or health[src].status in {"expired", "missing"})
    }
    mgr = ReauthManager(config or {})
    refreshed: set[str] = set()
    failed_forced: set[str] = set()

    for src in ordered_sources:
        if src not in candidates:
            continue
        ok = False
        try:
            ok = await mgr.attempt_automated(src)
        except Exception as exc:
            _log.warning("preflight.reauth.error source=%s error=%s", src, exc)
        if ok:
            refreshed.add(src)
        elif src in forced:
            failed_forced.add(src)

    if candidates:
        health = {h.source: h for h in check_session_health(ordered_sources)}

    notified: set[str] = set()
    for src in ordered_sources:
        h = health.get(src)
        needs_human = src in failed_forced or h is None or h.status in {"expired", "missing"}
        if not needs_human:
            continue
        _send_deep_link_notification(
            src,
            f"[Job Agent] {src.capitalize()} session unavailable after automated recovery. Tap to fix:",
        )
        notified.add(src)

    return ReauthPreflightResult(
        health=health,
        refreshed_sources=frozenset(refreshed),
        notified_sources=frozenset(notified),
    )
```

Important: missing credentials are handled inside `_reauth_automated(..., escalate=False)` and therefore cannot produce a second notification before this function's one escalation path.

- [ ] **Step 6: Run the focused suites GREEN**

```bash
pytest tests/test_reauth_unit.py tests/test_session_watchdog.py tests/test_session_autonomy_preflight.py \
  -q --tb=short --asyncio-mode=auto
```

- [ ] **Step 7: Commit**

```bash
git add src/reauth.py src/session_watchdog.py tests/test_reauth_unit.py tests/test_session_autonomy_preflight.py
git commit -m "feat(session): add reauth-aware unattended preflight"
```

---

### Task 4: Re-arm source-login-blocked jobs and reclassify fresh DB rows in the same run

**Files:**
- Modify: `src/orchestrator.py`
- Modify: `tests/test_reauth_feature.py`
- Modify: `tests/test_reauth_unblock.py` only if an additional source-own status needs direct coverage; do not rewrite existing tests.

**Interfaces:**
- Consumes: `preflight_session_check_with_reauth()` from Task 3.
- Consumes: existing `Orchestrator._unblock_session_jobs_after_reauth(source)`.
- Consumes: existing `StateManager.get_job(job_id)` after the unblock helper mutates durable `extra_json`.
- Produces: same-run promotion from blocked -> ready only when the freshly reloaded row actually classifies ready.

- [ ] **Step 1: Add a failing same-run recovery integration test**

In `tests/test_reauth_feature.py`, add a test around real `StateManager` semantics rather than mutating the stale in-memory dict. The core assertions must be:

```python
@pytest.mark.asyncio
async def test_background_preflight_reauth_reloads_unblocked_job_and_attempts_same_run(...):
    # Persist approved LinkedIn job with linkedin_authwall in StateManager.
    # Capture the stale row before preflight.
    # Mock preflight result refreshed_sources={"linkedin"}.
    # Let real _unblock_session_jobs_after_reauth() stamp session_prepared_at.
    # Scraper.apply returns a controlled non-live result.
    await orchestrator.apply_approved(auto_submit=True)

    assert scraper.apply.await_count == 1
    assert state.get_job("li-1") is not stale_row
```

Use an observed effect rather than Python object identity for the last assertion; for example assert the row passed into the scraper contains `session_prepared_at` in parsed `extra_json`.

- [ ] **Step 2: Add a failing external-portal preservation test**

Persist two LinkedIn-origin jobs:
- `linkedin_authwall` (own-source blocker), and
- `workday_session_expired` with an `ats_url` (external portal blocker).

After a mocked successful LinkedIn preflight, assert:

```python
assert linkedin_job_was_attempted is True
assert workday_job_was_attempted is False
assert "session_prepared_at" not in parse_extra_json(state.get_job("workday")["extra_json"])
```

This test must use the real `_unblock_session_jobs_after_reauth()` behavior already covered in `test_reauth_unblock.py`.

- [ ] **Step 3: Add a single-notification-owner regression**

Mock `preflight_session_check_with_reauth()` to return a still-blocked source and patch old `preflight_session_check()`. Assert the old synchronous path is **not** called after a successful async preflight execution. Add a second case where the async helper itself raises before returning; only then may the old synchronous function run once as a fallback.

- [ ] **Step 4: Run the new orchestrator tests and observe RED**

```bash
pytest tests/test_reauth_feature.py tests/test_reauth_unblock.py -k "preflight or unblock or external_portal" -vv
```

Expected: current `main` never invokes the async preflight and stale rows remain blocked.

- [ ] **Step 5: Wire async preflight into `apply_approved()`**

Update the import:

```python
from .session_watchdog import preflight_session_check, preflight_session_check_with_reauth
```

Before invoking preflight, derive `force_reauth_sources` **only from source-own blocker statuses**, not from generic `BlockerClass.AUTH_REQUIRED`:

```python
force_reauth_sources: set[str] = set()
for bj, _readiness, _reason in blocked:
    src = str(bj.get("source") or "")
    extra = parse_extra_json(bj.get("extra_json"))
    last_status = str(extra.get("apply_last_status") or "")
    own_statuses = _OWN_SESSION_STATUSES_ANY | _OWN_SESSION_STATUSES.get(src, set())
    if src and last_status in own_statuses:
        force_reauth_sources.add(src)
```

For non-interactive blocked runs:

```python
preflight_completed = False
try:
    result = await preflight_session_check_with_reauth(
        list(blocked_sources),
        self.config,
        force_reauth=force_reauth_sources,
    )
    preflight_completed = True
    for src in result.refreshed_sources:
        self._unblock_session_jobs_after_reauth(src)
except Exception as exc:
    _log.warning("apply.preflight_reauth_error error=%s", exc)
    preflight_session_check(list(blocked_sources))
```

Then rebuild `blocked` using **fresh durable rows**:

```python
still_blocked: list[tuple] = []
for old_job, old_readiness, old_reason in blocked:
    fresh_job = self.state.get_job(old_job["job_id"]) or old_job
    new_readiness, new_reason = self._classify_apply_readiness(fresh_job)
    if new_readiness in BLOCKED_READINESS:
        still_blocked.append((fresh_job, new_readiness, new_reason))
    else:
        ready.append(fresh_job)
blocked = still_blocked
```

After this block, do not call `preflight_session_check()` again when `preflight_completed` is true. This is the single-escalation-owner rule.

- [ ] **Step 6: Run all auth/routing integration tests GREEN**

```bash
pytest tests/test_reauth_feature.py tests/test_reauth_unblock.py \
       tests/test_auth_routing.py tests/test_session_preflight.py \
       tests/test_session_autonomy_preflight.py \
       -q --tb=short --asyncio-mode=auto
```

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator.py tests/test_reauth_feature.py tests/test_reauth_unblock.py
git commit -m "fix(session): reclassify jobs after same-run reauth"
```

---

### Task 5: Checkpoint known-good LinkedIn session state before later work can fail

**Files:**
- Modify: `src/sources/linkedin.py`
- Create: `tests/test_linkedin_session_checkpoint.py`

**Interfaces:**
- Consumes: existing `BaseScraper._save_session()` / `_export_session_json()` behavior.
- Produces: routine LinkedIn scrape/saved-job/apply paths persist current cookies immediately after the authentication gate proves the page is logged in.
- Does not alter login credentials, Easy Apply answers, submission clicks, or receipt truth.

- [ ] **Step 1: Add failing checkpoint-order tests**

Create `tests/test_linkedin_session_checkpoint.py` with mocked browser/page boundaries. Prove the checkpoint happens **before** downstream work that can fail:

```python
@pytest.mark.asyncio
async def test_scrape_checkpoints_already_authenticated_session_before_search_failure(monkeypatch):
    scraper = LinkedInScraper({"target_roles": ["Director Engineering"]})
    page = AsyncMock()
    page.goto = AsyncMock()
    monkeypatch.setattr(scraper, "_start_browser", AsyncMock(return_value=page))
    monkeypatch.setattr(scraper, "_close_browser", AsyncMock())
    monkeypatch.setattr(scraper, "_delay", AsyncMock())
    monkeypatch.setattr(scraper, "_needs_login", AsyncMock(return_value=False))
    save = AsyncMock()
    monkeypatch.setattr(scraper, "_save_session", save)
    monkeypatch.setattr(scraper, "_search_jobs", AsyncMock(side_effect=RuntimeError("after-auth failure")))

    await scraper.scrape()

    save.assert_awaited()
```

Add equivalent coverage for `scrape_saved()`. For `apply()`, use the real auth check point if one exists at execution time; if current `apply()` lacks an auth gate, add a test that a login wall produces `linkedin_authwall`/existing source-auth status and that an already-authenticated page calls `_save_session()` before Easy Apply/external-ATS work.

- [ ] **Step 2: Run the new tests and observe RED**

```bash
pytest tests/test_linkedin_session_checkpoint.py -vv
```

Expected: already-authenticated scrape/saved-job paths do not save before downstream work.

- [ ] **Step 3: Add one small checkpoint helper**

Avoid scattered duplicate save calls by adding:

```python
    async def _checkpoint_authenticated_session(self) -> None:
        try:
            await self._save_session()
        except Exception as exc:
            console.print(f"[dim]LinkedIn session checkpoint failed: {exc}[/dim]")
```

Call it once after the authentication decision is known good in `scrape()` and `scrape_saved()`. For `apply()`, call it after its source-auth gate proves the current LinkedIn page is authenticated and before form/external-ATS work.

Do not treat a checkpoint write failure as authentication success or application success; it is best-effort persistence of already-proven auth state.

- [ ] **Step 4: Run LinkedIn auth/apply-focused tests GREEN**

```bash
pytest tests/test_linkedin_session_checkpoint.py \
       tests/test_linkedin_screening_answers.py \
       tests/test_apply_functional.py -q --tb=short --asyncio-mode=auto
```

If live-marked tests are skipped by repository configuration, that skip is acceptable; no live employer submission is allowed for this PR.

- [ ] **Step 5: Commit**

```bash
git add src/sources/linkedin.py tests/test_linkedin_session_checkpoint.py
git commit -m "fix(linkedin): checkpoint known-good sessions early"
```

---

### Task 6: Stabilize user-facing scheduled warnings, enroll focused CI coverage, and verify the PR

**Files:**
- Modify: `src/orchestrator.py`
- Modify: `.github/workflows/ci.yml`
- Modify: tests from Tasks 1-5 only when required by an observed defect; do not broaden scope.

**Interfaces:**
- `notify_warning()` from Task 2 supports stable cross-process keys.
- CI must explicitly expose Session Autonomy failures in the ordinary Python matrix in addition to the existing full regression job.

- [ ] **Step 1: Add a failing warning-key regression**

In `tests/test_reauth_feature.py` or a small focused notifier/orchestrator test, assert the unattended “nothing submitted” warning uses a stable key independent of changing job counts:

```python
notify_warning_mock.assert_called_with(
    "Apply run: nothing submitted",
    ANY,
    dedupe_key="apply_nothing_submitted",
    dedupe_seconds=6 * 3600,
    desktop=False,
)
```

Match the actual title used by current `main`; do not rename unrelated notification copy simply to satisfy this plan.

- [ ] **Step 2: Implement the stable warning key**

At the existing “nothing submitted” warning call in `src/orchestrator.py`, pass:

```python
dedupe_key="apply_nothing_submitted",
dedupe_seconds=6 * 3600,
desktop=False,
```

The detail may continue to include changing counts because the stable key now owns dedupe identity.

- [ ] **Step 3: Enroll session-autonomy suites in the Python matrix**

Extend the ordinary `.github/workflows/ci.yml` test list with:

```yaml
tests/test_session_watchdog.py \
tests/test_session_preflight.py \
tests/test_session_autonomy_preflight.py \
tests/test_reauth_unit.py \
tests/test_reauth_unblock.py \
tests/test_notifier_hygiene.py \
tests/test_linkedin_session_checkpoint.py \
```

Keep the existing `receipt-truthfulness` full `pytest tests/` regression unchanged. Do not install Chromium a second time in the 3.11/3.12 matrix.

- [ ] **Step 4: Run the focused Session Autonomy verification**

```bash
pytest tests/test_session_watchdog.py \
       tests/test_session_preflight.py \
       tests/test_session_autonomy_preflight.py \
       tests/test_reauth_unit.py \
       tests/test_reauth_feature.py \
       tests/test_reauth_unblock.py \
       tests/test_auth_routing.py \
       tests/test_notifier_hygiene.py \
       tests/test_linkedin_session_checkpoint.py \
       -q --tb=short --asyncio-mode=auto
```

Expected: 0 failures.

- [ ] **Step 5: Run submission-safety adjacent tests**

Session work must not alter the PR #121 safety contract:

```bash
pytest tests/test_generic_submit_dispatch_truth.py \
       tests/test_recovery_submit_dispatch_truth.py \
       tests/test_possible_submit_preservation.py \
       tests/test_receipt_freshness_edge_cases.py \
       -q --tb=short --asyncio-mode=auto
```

Expected: 0 failures.

- [ ] **Step 6: Run the full repository suite**

Use the same environment as CI:

```bash
pip install -r requirements-dev.txt -r dashboard/requirements.txt
python -m playwright install chromium
xvfb-run -a pytest tests/ -q --tb=short --asyncio-mode=auto
```

Expected: 0 failures. Existing documented skips/warnings are acceptable only if unchanged from `main` or explained in the PR.

- [ ] **Step 7: Run static/security checks**

```bash
ruff check src/ --select=E,W,F --ignore=E501,W291,E402,E741,F841,F541
gitleaks detect --source . --no-banner
```

If local gitleaks is unavailable, rely on the required GitHub Actions `secrets` job rather than installing unreviewed tooling during implementation.

- [ ] **Step 8: Commit CI/warning changes**

```bash
git add src/orchestrator.py .github/workflows/ci.yml tests/
git commit -m "ci(session): gate autonomous session recovery"
```

- [ ] **Step 9: Push normally and open a draft PR**

```bash
git push -u origin feat/session-autonomy
```

Open a draft PR against `main` titled:

```text
feat: autonomous session self-healing for scheduled apply runs
```

The PR body must state:
- built from post-#121 `main`;
- PR #120 was selectively ported, not merged;
- no blocker-intelligence/adaptive-cap code included;
- exact RED tests added and GREEN evidence;
- same-run source reauth behavior;
- external ATS blocker preservation;
- durable notification dedupe across fresh processes;
- no live employer submissions used for verification.

- [ ] **Step 10: Request fresh code + security review and freeze scope**

Request Codex/Copilot review on the final PR head. Fix only confirmed defects in the Session Autonomy contract. Move non-blocking cleanup or unrelated findings into follow-up PRs. Do not merge until all required CI jobs are green and there are no unresolved P0/P1 production-safety findings.

---

## Plan Self-Review Checklist

Before execution begins, verify:

- Every PR A requirement in the approved spec maps to at least one task above.
- `blocker_intelligence.py` and adaptive retry changes from PR #120 are absent.
- No task weakens PR #121 receipt/ledger safety.
- Source reauth cannot clear external ATS portal blockers.
- Human escalation has one owner per background preflight.
- Durable dedupe does not suppress retries when Terminal staging fails.
- Fresh DB rows are reloaded after `clear_session_block()` before reclassification.
- No live application is required to verify PR A.
