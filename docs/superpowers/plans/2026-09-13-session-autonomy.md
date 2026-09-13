# Session Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make unattended apply runs automatically recover source-login sessions, re-arm affected jobs in the same run, and suppress duplicate human alerts across scheduler processes without weakening submission safety.

**Architecture:** Keep the existing `StateManager`, `ReauthManager`, session watchdog, and orchestrator boundaries. Add an automated-only reauth entry point, return structured preflight results from the watchdog, reuse `Orchestrator._unblock_session_jobs_after_reauth()` for source-own blockers, reload jobs from SQLite before reclassification, and persist notification dedupe timestamps in `state/agent_status.json`. PR #120 is a reference only; no blocker-intelligence or adaptive-retry code is ported.

**Tech Stack:** Python 3.11/3.12, asyncio, SQLite-backed `StateManager`, JSON status/session files, pytest/pytest-asyncio, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-13-job-agent-autonomy-design.md`

## Global Constraints

- Start implementation from the then-current `main`, not from PR #120 or `plan/job-agent-autonomy`.
- Use branch `feat/session-autonomy`.
- Security/submission correctness first; reuse existing interfaces second; simplicity third.
- Every production behavior change begins with an observed failing test.
- Do not use `--no-verify`; the normal pre-push hook must pass.
- Do not touch `blocker_intelligence.py`, add model-backed blocker classification, or add adaptive retry caps.
- Do not change receipt verification, `SubmissionLedger`, or reconciliation semantics established by PR #121.
- Source-level reauth may clear only the source's own login blockers. External ATS portal blockers such as `workday_session_expired` must survive.
- Background preflight owns human escalation. Its automated reauth call must not independently notify.
- Use existing durable state; add no cloud service or new database.
- Keep PR #123 independent.

---

### Task 1: Make LinkedIn health depend on authentication cookies

**Files:**
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_session_watchdog.py`

**Interfaces:**
- Produce `LINKEDIN_AUTH_COOKIE_NAMES: frozenset[str]`.
- Produce `_linkedin_auth_cookie_state(session_path: Path) -> tuple[bool, Optional[float]]`.
- Preserve `_parse_linkedin_expiry(session_path: Path) -> Optional[float]` for existing callers/tests.

- [ ] **Step 1: Write the failing regression tests**

Add imports `json`, `os`, and `src.session_watchdog as sw` if not already present. Add:

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
    assert "authentication cookie" in health.detail.lower()
```

Keep the existing single-`li_at` healthy/expired tests.

- [ ] **Step 2: Verify RED**

Run:

```bash
pytest tests/test_session_watchdog.py -k "tracking_cookie_expiry or without_auth_cookie" -vv
```

Expected: the tracking-cookie case is falsely expired and the tracking-only case is falsely healthy.

- [ ] **Step 3: Implement the minimal parser**

Add in `src/session_watchdog.py`:

```python
LINKEDIN_AUTH_COOKIE_NAMES = frozenset({"li_at", "liap", "li_rm"})


def _linkedin_auth_cookie_state(session_path: Path) -> tuple[bool, Optional[float]]:
    try:
        data = json.loads(session_path.read_text())
        auth_cookies = [
            cookie
            for cookie in data.get("cookies", [])
            if "linkedin" in str(cookie.get("domain", "")).lower()
            and str(cookie.get("name", "")) in LINKEDIN_AUTH_COOKIE_NAMES
        ]
        if not auth_cookies:
            return False, None
        expiries = [
            float(cookie["expires"])
            for cookie in auth_cookies
            if float(cookie.get("expires", -1) or -1) > 0
        ]
        if not expiries:
            return True, None
        return True, (min(expiries) - time.time()) / 3600
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, None


def _parse_linkedin_expiry(session_path: Path) -> Optional[float]:
    _has_auth_cookie, expiry_hours = _linkedin_auth_cookie_state(session_path)
    return expiry_hours
```

In `check_session_health()`, immediately after calculating `age_hours`, use:

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

Leave the existing age/cookie-expiry stale/expired thresholds unchanged.

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/test_session_watchdog.py -q --tb=short --asyncio-mode=auto
```

- [ ] **Step 5: Commit**

```bash
git add src/session_watchdog.py tests/test_session_watchdog.py
git commit -m "fix(session): use LinkedIn auth cookies for health"
```

---

### Task 2: Persist notification dedupe across scheduler processes

**Files:**
- Modify: `src/notifier.py`
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_notifier_hygiene.py`
- Modify: `tests/test_session_watchdog.py`

**Interfaces:**
- Produce `notification_dedupe_active(key: str, dedupe_seconds: int, *, now: float | None = None) -> bool`.
- Produce `record_notification_dedupe(key: str, *, now: float | None = None) -> None`.
- Extend `notify_warning()` with `dedupe_key` and `dedupe_seconds` keyword-only arguments.
- Keep `_add_alert()` independent from Telegram dedupe so each run is still observable.
- True concurrent-process serialization belongs to PR B; this task proves sequential process/restart durability.

- [ ] **Step 1: Write a fresh-process RED test**

Add imports `os`, `subprocess`, `sys`, and `Path` to `tests/test_notifier_hygiene.py`. Define:

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
```

Add:

```python
def test_warning_dedupe_survives_fresh_python_process(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    notifier._last_notification_times.clear()

    notifier.notify_warning(
        "Apply run: nothing submitted",
        "first process",
        dedupe_key="apply_nothing_submitted",
        dedupe_seconds=21600,
        desktop=False,
    )
    assert len(sent) == 1

    code = "\n".join([
        "import os",
        "from pathlib import Path",
        "import src.notifier as n",
        "n.STATUS_FILE = Path(os.environ['JOBAGENT_STATUS_FILE'])",
        "n._send_telegram = lambda message: print('SENT')",
        "n._desktop_notify = lambda *args, **kwargs: None",
        "n.notify_warning('Apply run: nothing submitted', 'second process', dedupe_key='apply_nothing_submitted', dedupe_seconds=21600, desktop=False)",
    ])
    child = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JOBAGENT_STATUS_FILE": str(status_file),
            "PYTHONPATH": str(REPO_ROOT),
        },
        check=False,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert "SENT" not in child.stdout
```

- [ ] **Step 2: Write deep-link durability and staging-failure tests**

Add to `tests/test_session_watchdog.py`:

```python
def test_deep_link_dedupe_survives_memory_reset(tmp_path, monkeypatch):
    from src import notifier
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sw, "_stage_prepare_sessions", lambda source: True)
    monkeypatch.setattr(sw, "_novnc_link", lambda: None)
    notifier._last_notification_times.clear()

    sw._send_deep_link_notification("linkedin", "fix session")
    notifier._last_notification_times.clear()
    sw._send_deep_link_notification("linkedin", "fix session again")

    assert len(sent) == 1


def test_deep_link_staging_failure_does_not_consume_dedupe_window(tmp_path, monkeypatch):
    from src import notifier
    status_file = tmp_path / "status.json"
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: None)
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sw, "_stage_prepare_sessions", lambda source: False)
    monkeypatch.setattr(sw, "_novnc_link", lambda: None)
    notifier._last_notification_times.clear()

    sw._send_deep_link_notification("linkedin", "fix session")

    data = json.loads(status_file.read_text()) if status_file.exists() else {}
    assert "notification_dedupe" not in data
```

- [ ] **Step 3: Verify RED**

```bash
pytest tests/test_notifier_hygiene.py tests/test_session_watchdog.py \
  -k "dedupe or staging_failure" -vv
```

- [ ] **Step 4: Implement durable helper functions**

Add to `src/notifier.py`:

```python
def notification_dedupe_active(
    key: str,
    dedupe_seconds: int,
    *,
    now: float | None = None,
) -> bool:
    timestamp = time.time() if now is None else now
    cache_key = f"notification:{key}"
    memory_value = float(_last_notification_times.get(cache_key, 0) or 0)
    if timestamp - memory_value < dedupe_seconds:
        return True
    status = _load_status()
    durable_value = float(status.get("notification_dedupe", {}).get(cache_key, 0) or 0)
    if timestamp - durable_value < dedupe_seconds:
        _last_notification_times[cache_key] = durable_value
        return True
    return False


def record_notification_dedupe(key: str, *, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    cache_key = f"notification:{key}"
    status = _load_status()
    dedupe = status.setdefault("notification_dedupe", {})
    dedupe[cache_key] = timestamp
    if len(dedupe) > 200:
        newest = sorted(
            dedupe.items(),
            key=lambda item: float(item[1] or 0),
            reverse=True,
        )[:200]
        status["notification_dedupe"] = dict(newest)
    _save_status(status)
    _last_notification_times[cache_key] = timestamp
```

Replace the Telegram throttle portion of `notify_warning()` with:

```python
key = f"warn:{dedupe_key}" if dedupe_key else f"warn:{title}:{detail}"
if not notification_dedupe_active(key, dedupe_seconds):
    _send_telegram(f"⚠️ [Job Agent WARNING] {title}\nDetail: {detail}")
    record_notification_dedupe(key)
```

The signature becomes:

```python
def notify_warning(
    title: str,
    detail: str = "",
    *,
    desktop: bool = True,
    dedupe_key: str | None = None,
    dedupe_seconds: int = 900,
) -> None:
```

- [ ] **Step 5: Convert deep-link throttle to durable read-then-record**

In `_send_deep_link_notification()`, import `notification_dedupe_active` and `record_notification_dedupe`. The control order must be:

```python
key = f"deep_link:{source}"
if notification_dedupe_active(key, 12 * 3600):
    return
if not _stage_prepare_sessions(source):
    return
_send_telegram(full_msg)
_desktop_notify(
    f"🔐 {source.capitalize()} session needs attention",
    message,
    subtitle="Job Agent",
)
record_notification_dedupe(key)
```

A staging failure must return before the dedupe timestamp is written.

- [ ] **Step 6: Verify GREEN and commit**

```bash
pytest tests/test_notifier_hygiene.py tests/test_session_watchdog.py -q --tb=short --asyncio-mode=auto
git add src/notifier.py src/session_watchdog.py tests/test_notifier_hygiene.py tests/test_session_watchdog.py
git commit -m "fix(notify): persist scheduler warning dedupe"
```

---

### Task 3: Add automated-only reauth and structured unattended preflight

**Files:**
- Modify: `src/reauth.py`
- Modify: `src/session_watchdog.py`
- Modify: `tests/test_reauth_unit.py`
- Create: `tests/test_session_autonomy_preflight.py`

**Interfaces:**
- Produce `ReauthManager.attempt_automated(source: str) -> bool`.
- Produce frozen `ReauthPreflightResult` with `health`, `refreshed_sources`, and `notified_sources`.
- Produce `preflight_session_check_with_reauth(sources, config=None, *, force_reauth=None)`.

- [ ] **Step 1: Write failing `ReauthManager` contract tests**

Add to `tests/test_reauth_unit.py`:

```python
@pytest.mark.asyncio
async def test_attempt_automated_never_uses_human_fallback():
    mgr = ReauthManager({})
    with patch.object(
        mgr,
        "_reauth_automated",
        new_callable=AsyncMock,
        return_value=False,
    ) as automated, patch.object(
        mgr,
        "_reauth_human",
        new_callable=AsyncMock,
    ) as human:
        result = await mgr.attempt_automated("usajobs")

    assert result is False
    automated.assert_awaited_once_with("usajobs", escalate=False)
    human.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_automated_unknown_source_returns_false():
    assert await ReauthManager({}).attempt_automated("unknown") is False
```

- [ ] **Step 2: Write failing structured preflight tests**

Create `tests/test_session_autonomy_preflight.py`:

```python
from unittest.mock import AsyncMock

import pytest

import src.reauth as reauth_mod
import src.session_watchdog as sw


def _health(source, status):
    return sw.SessionHealth(
        source=source,
        status=status,
        age_hours=1.0,
        session_path=sw.SESSIONS_DIR / f"{source}_chromium.json",
        detail=status,
    )


@pytest.mark.asyncio
async def test_successful_reauth_reports_refreshed_without_human_notification(monkeypatch):
    health_calls = iter([
        [_health("linkedin", "expired")],
        [_health("linkedin", "healthy")],
    ])
    monkeypatch.setattr(sw, "check_session_health", lambda sources: next(health_calls))
    attempt = AsyncMock(return_value=True)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    notifications = []
    monkeypatch.setattr(sw, "_send_deep_link_notification", lambda source, message: notifications.append(source))

    result = await sw.preflight_session_check_with_reauth(["linkedin"], {})

    assert result.refreshed_sources == frozenset({"linkedin"})
    assert result.notified_sources == frozenset()
    assert notifications == []
    attempt.assert_awaited_once_with("linkedin")


@pytest.mark.asyncio
async def test_failed_forced_reauth_notifies_exactly_once(monkeypatch):
    monkeypatch.setattr(sw, "check_session_health", lambda sources: [_health("linkedin", "healthy")])
    attempt = AsyncMock(return_value=False)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    notifications = []
    monkeypatch.setattr(sw, "_send_deep_link_notification", lambda source, message: notifications.append(source))

    result = await sw.preflight_session_check_with_reauth(
        ["linkedin"],
        {},
        force_reauth={"linkedin"},
    )

    assert result.refreshed_sources == frozenset()
    assert result.notified_sources == frozenset({"linkedin"})
    assert notifications == ["linkedin"]
    attempt.assert_awaited_once_with("linkedin")


@pytest.mark.asyncio
async def test_source_is_attempted_once_when_unhealthy_and_forced(monkeypatch):
    monkeypatch.setattr(sw, "check_session_health", lambda sources: [_health("linkedin", "expired")])
    attempt = AsyncMock(return_value=False)

    class FakeManager:
        def __init__(self, config):
            self.config = config

        async def attempt_automated(self, source):
            return await attempt(source)

    monkeypatch.setattr(reauth_mod, "ReauthManager", FakeManager)
    monkeypatch.setattr(sw, "_send_deep_link_notification", lambda source, message: None)

    await sw.preflight_session_check_with_reauth(
        ["linkedin", "linkedin"],
        {},
        force_reauth={"linkedin"},
    )

    attempt.assert_awaited_once_with("linkedin")
```

- [ ] **Step 3: Verify RED**

```bash
pytest tests/test_reauth_unit.py tests/test_session_autonomy_preflight.py -vv
```

- [ ] **Step 4: Implement the automated-only public API**

Add to `ReauthManager`:

```python
async def attempt_automated(self, source: str) -> bool:
    """Try only stored-credential recovery; never notify or wait for a human."""
    if source not in AUTOMATED_SOURCES:
        return False
    return await self._reauth_automated(source, escalate=False)
```

Do not alter `handle()`.

- [ ] **Step 5: Implement structured preflight**

Add in `src/session_watchdog.py`:

```python
@dataclass(frozen=True)
class ReauthPreflightResult:
    health: dict[str, SessionHealth]
    refreshed_sources: frozenset[str]
    notified_sources: frozenset[str]


async def preflight_session_check_with_reauth(
    sources: list[str],
    config: dict | None = None,
    *,
    force_reauth: set[str] | None = None,
) -> ReauthPreflightResult:
    from .reauth import AUTOMATED_SOURCES, ReauthManager

    ordered_sources = list(dict.fromkeys(source for source in sources if source))
    forced = set(force_reauth or ())
    health = {item.source: item for item in check_session_health(ordered_sources)}
    candidates = {
        source
        for source in ordered_sources
        if source in AUTOMATED_SOURCES
        and (
            source in forced
            or source not in health
            or health[source].status in {"expired", "missing"}
        )
    }
    manager = ReauthManager(config or {})
    refreshed: set[str] = set()
    failed_forced: set[str] = set()

    for source in ordered_sources:
        if source not in candidates:
            continue
        success = False
        try:
            success = await manager.attempt_automated(source)
        except Exception as exc:
            _log.warning("preflight.reauth.error source=%s error=%s", source, exc)
        if success:
            refreshed.add(source)
        elif source in forced:
            failed_forced.add(source)

    if candidates:
        health = {item.source: item for item in check_session_health(ordered_sources)}

    notified: set[str] = set()
    for source in ordered_sources:
        item = health.get(source)
        needs_human = (
            source in failed_forced
            or item is None
            or item.status in {"expired", "missing"}
        )
        if not needs_human:
            continue
        _send_deep_link_notification(
            source,
            f"[Job Agent] {source.capitalize()} session unavailable after automated recovery. Tap to fix:",
        )
        notified.add(source)

    return ReauthPreflightResult(
        health=health,
        refreshed_sources=frozenset(refreshed),
        notified_sources=frozenset(notified),
    )
```

- [ ] **Step 6: Verify GREEN and commit**

```bash
pytest tests/test_reauth_unit.py tests/test_session_watchdog.py tests/test_session_autonomy_preflight.py \
  -q --tb=short --asyncio-mode=auto
git add src/reauth.py src/session_watchdog.py tests/test_reauth_unit.py tests/test_session_autonomy_preflight.py
git commit -m "feat(session): add reauth-aware unattended preflight"
```

---

### Task 4: Re-arm and reload source-login-blocked jobs in the same run

**Files:**
- Modify: `src/orchestrator.py`
- Modify: `tests/test_reauth_feature.py`

**Interfaces:**
- Consume `preflight_session_check_with_reauth()`.
- Reuse existing `_unblock_session_jobs_after_reauth(source)`.
- Reload each blocked job with `StateManager.get_job(job_id)` before reclassification.
- Derive forced reauth only from `_OWN_SESSION_STATUSES_ANY` and `_OWN_SESSION_STATUSES`; never from generic `AUTH_REQUIRED` classification.

- [ ] **Step 1: Add same-run recovery RED test**

Inside `TestApplyReauth` in `tests/test_reauth_feature.py`, add imports `parse_extra_json`, `ReauthPreflightResult`, and `SessionHealth`, then add:

```python
@pytest.mark.asyncio
async def test_background_preflight_reauth_unblocks_and_applies_in_same_run(self, orchestrator, tmp_status):
    job = _approved_job("li-own-auth", "linkedin")
    job["url"] = "https://www.linkedin.com/jobs/view/123"
    self._seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt("li-own-auth", "linkedin_authwall", "login required")

    scraper = AsyncMock()
    scraper.apply = AsyncMock(return_value=True)
    scraper._apply_analytics = None
    scraper_cls = MagicMock(return_value=scraper)
    preflight = ReauthPreflightResult(
        health={},
        refreshed_sources=frozenset({"linkedin"}),
        notified_sources=frozenset(),
    )

    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.preflight_session_check_with_reauth", new=AsyncMock(return_value=preflight)), \
         patch("src.orchestrator.preflight_session_check") as legacy_preflight, \
         patch("src.orchestrator.Orchestrator._sync_to_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._push_status_to_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock):
        await orchestrator.apply_approved(auto_submit=True)

    scraper.apply.assert_awaited_once()
    legacy_preflight.assert_not_called()
    row = orchestrator.state.get_job("li-own-auth")
    assert row["status"] == "applied"
```

- [ ] **Step 2: Add external ATS preservation RED test**

```python
@pytest.mark.asyncio
async def test_source_reauth_does_not_clear_external_portal_wall(self, orchestrator, tmp_status):
    job = _approved_job("li-workday", "linkedin")
    job["url"] = "https://www.linkedin.com/jobs/view/456"
    job["ats_url"] = "https://acme.wd1.myworkdayjobs.com/job/456"
    self._seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt("li-workday", "workday_session_expired", "portal login")

    preflight = ReauthPreflightResult(
        health={},
        refreshed_sources=frozenset({"linkedin"}),
        notified_sources=frozenset(),
    )
    scraper_cls = MagicMock()

    with patch.dict("src.orchestrator.SOURCE_MAP", {"linkedin": scraper_cls}), \
         patch("src.orchestrator.preflight_session_check_with_reauth", new=AsyncMock(return_value=preflight)), \
         patch("src.orchestrator.preflight_session_check"), \
         patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock):
        await orchestrator.apply_approved(auto_submit=True)

    scraper_cls.assert_not_called()
    row = orchestrator.state.get_job("li-workday")
    assert "session_prepared_at" not in parse_extra_json(row.get("extra_json"))
```

- [ ] **Step 3: Add async-preflight failure fallback RED test**

```python
@pytest.mark.asyncio
async def test_async_preflight_error_falls_back_to_legacy_notification_once(self, orchestrator, tmp_status):
    job = _approved_job("li-blocked", "linkedin")
    job["url"] = "https://www.linkedin.com/jobs/view/789"
    self._seed_job(orchestrator, job)
    orchestrator.state.record_apply_attempt("li-blocked", "linkedin_authwall", "login required")

    with patch("src.orchestrator.preflight_session_check_with_reauth", new=AsyncMock(side_effect=RuntimeError("synthetic preflight failure"))), \
         patch("src.orchestrator.preflight_session_check") as legacy_preflight, \
         patch("src.orchestrator.Orchestrator._pull_approved_from_cloud", new_callable=AsyncMock), \
         patch("src.orchestrator.Orchestrator._push_apply_attempt_to_cloud", new_callable=AsyncMock):
        await orchestrator.apply_approved(auto_submit=True)

    legacy_preflight.assert_called_once_with(["linkedin"])
```

- [ ] **Step 4: Verify RED**

```bash
pytest tests/test_reauth_feature.py -k "background_preflight or external_portal_wall or async_preflight_error" -vv
```

- [ ] **Step 5: Implement orchestrator wiring**

Change the watchdog import to:

```python
from .session_watchdog import preflight_session_check, preflight_session_check_with_reauth
```

After the initial `blocked` list is built, derive:

```python
blocked_sources = {
    str(job.get("source") or "")
    for job, _readiness, _reason in blocked
    if job.get("source")
}
force_reauth_sources: set[str] = set()
for job, _readiness, _reason in blocked:
    source = str(job.get("source") or "")
    extra = parse_extra_json(job.get("extra_json"))
    last_status = str(extra.get("apply_last_status") or "")
    own_statuses = _OWN_SESSION_STATUSES_ANY | _OWN_SESSION_STATUSES.get(source, set())
    if source and last_status in own_statuses:
        force_reauth_sources.add(source)
```

For a non-interactive run with blocked sources, execute:

```python
try:
    preflight_result = await preflight_session_check_with_reauth(
        list(blocked_sources),
        self.config,
        force_reauth=force_reauth_sources,
    )
    for source in preflight_result.refreshed_sources:
        self._unblock_session_jobs_after_reauth(source)
except Exception as exc:
    _log.warning("apply.preflight_reauth_error error=%s", exc)
    preflight_session_check(list(blocked_sources))
```

Then rebuild `blocked` from durable rows:

```python
still_blocked: list[tuple] = []
for old_job, _old_readiness, _old_reason in blocked:
    fresh_job = self.state.get_job(old_job["job_id"]) or old_job
    new_readiness, new_reason = self._classify_apply_readiness(fresh_job)
    if new_readiness in BLOCKED_READINESS:
        still_blocked.append((fresh_job, new_readiness, new_reason))
    else:
        ready.append(fresh_job)
blocked = still_blocked
```

Remove the later unconditional non-interactive call to `preflight_session_check()`; the only legacy call left in this path is the exception fallback above.

- [ ] **Step 6: Verify GREEN and commit**

```bash
pytest tests/test_reauth_feature.py tests/test_reauth_unblock.py tests/test_auth_routing.py \
       tests/test_session_preflight.py tests/test_session_autonomy_preflight.py \
       -q --tb=short --asyncio-mode=auto
git add src/orchestrator.py tests/test_reauth_feature.py
git commit -m "fix(session): reclassify jobs after same-run reauth"
```

---

### Task 5: Checkpoint known-good LinkedIn sessions before downstream work

**Files:**
- Modify: `src/sources/linkedin.py`
- Create: `tests/test_linkedin_session_checkpoint.py`

**Interfaces:**
- Produce `_checkpoint_authenticated_session() -> None` as a small best-effort wrapper around `_save_session()`.
- Call it only after authentication is known good.
- A checkpoint failure is not application success and must not alter apply status.

- [ ] **Step 1: Write failing scrape and saved-job checkpoint tests**

Create `tests/test_linkedin_session_checkpoint.py`:

```python
from unittest.mock import AsyncMock

import pytest

from src.sources.linkedin import LinkedInScraper


@pytest.mark.asyncio
async def test_scrape_checkpoints_authenticated_session_before_search_failure(monkeypatch):
    scraper = LinkedInScraper({"target_roles": ["Director Engineering"]})
    page = AsyncMock()
    page.goto = AsyncMock()
    monkeypatch.setattr(scraper, "_start_browser", AsyncMock(return_value=page))
    monkeypatch.setattr(scraper, "_close_browser", AsyncMock())
    monkeypatch.setattr(scraper, "_delay", AsyncMock())
    monkeypatch.setattr(scraper, "_needs_login", AsyncMock(return_value=False))
    save = AsyncMock()
    monkeypatch.setattr(scraper, "_save_session", save)
    monkeypatch.setattr(
        scraper,
        "_search_jobs",
        AsyncMock(side_effect=RuntimeError("synthetic post-auth failure")),
    )

    assert await scraper.scrape() == []
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_scrape_saved_checkpoints_authenticated_session_before_scroll(monkeypatch):
    scraper = LinkedInScraper({})
    page = AsyncMock()
    page.goto = AsyncMock()
    monkeypatch.setattr(scraper, "_start_browser", AsyncMock(return_value=page))
    monkeypatch.setattr(scraper, "_close_browser", AsyncMock())
    monkeypatch.setattr(scraper, "_delay", AsyncMock())
    monkeypatch.setattr(scraper, "_needs_login", AsyncMock(return_value=False))
    save = AsyncMock()
    monkeypatch.setattr(scraper, "_save_session", save)
    monkeypatch.setattr(
        scraper,
        "_safe_evaluate",
        AsyncMock(side_effect=RuntimeError("synthetic post-auth failure")),
    )

    assert await scraper.scrape_saved() == []
    save.assert_awaited_once()
```

- [ ] **Step 2: Inspect the current `apply()` auth boundary before editing**

Run:

```bash
grep -n "async def apply\|_needs_login\|linkedin_authwall\|linkedin_login_required" src/sources/linkedin.py
```

If `apply()` already has an authentication gate, add a third test with `_needs_login=False` that asserts `_save_session()` occurs before the first Easy Apply/external-ATS action. If it has no authentication gate, do not invent a second login flow in this PR; session self-heal remains orchestrator-owned and the checkpoint scope is `scrape()` plus `scrape_saved()`.

- [ ] **Step 3: Verify RED**

```bash
pytest tests/test_linkedin_session_checkpoint.py -vv
```

- [ ] **Step 4: Implement one helper and call it after auth is proven**

Add to `LinkedInScraper`:

```python
async def _checkpoint_authenticated_session(self) -> None:
    try:
        await self._save_session()
    except Exception as exc:
        console.print(f"[dim]LinkedIn session checkpoint failed: {exc}[/dim]")
```

In `scrape()` and `scrape_saved()`, place:

```python
await self._checkpoint_authenticated_session()
```

after the login branch completes and before search/scroll/detail work begins. Do not add an extra call inside `_auto_login()`, which already persists on success.

- [ ] **Step 5: Verify GREEN and commit**

```bash
pytest tests/test_linkedin_session_checkpoint.py tests/test_linkedin_screening_answers.py \
       -q --tb=short --asyncio-mode=auto
git add src/sources/linkedin.py tests/test_linkedin_session_checkpoint.py
git commit -m "fix(linkedin): checkpoint known-good sessions early"
```

---

### Task 6: Add stable unattended warning identity, CI gates, and final verification

**Files:**
- Modify: `src/orchestrator.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_reauth_feature.py`

**Interfaces:**
- Use Task 2's extended `notify_warning()`.
- Keep existing `receipt-truthfulness` full-suite job unchanged.

- [ ] **Step 1: Add a failing stable-warning test**

In `tests/test_reauth_feature.py`, add a test that reaches the existing “nothing submitted” warning path with no successful applications. Patch `src.orchestrator.notify_warning` and assert:

```python
warning_calls = [call.kwargs for call in notify_warning_mock.call_args_list]
matching = [
    kwargs
    for kwargs in warning_calls
    if kwargs.get("dedupe_key") == "apply_nothing_submitted"
]
assert len(matching) == 1
assert matching[0]["dedupe_seconds"] == 6 * 3600
assert matching[0]["desktop"] is False
```

Use the existing test fixture and mocked scraper boundaries from `TestApplyReauth`; no browser/network is allowed.

- [ ] **Step 2: Implement the stable warning key**

At the current “Apply run: nothing submitted” `notify_warning()` call, add:

```python
dedupe_key="apply_nothing_submitted",
dedupe_seconds=6 * 3600,
desktop=False,
```

Keep the existing title/detail text unchanged.

- [ ] **Step 3: Enroll focused session suites in the Python matrix**

Add these files to the ordinary test command in `.github/workflows/ci.yml`:

```yaml
tests/test_session_watchdog.py \
tests/test_session_preflight.py \
tests/test_session_autonomy_preflight.py \
tests/test_reauth_unit.py \
tests/test_reauth_unblock.py \
tests/test_notifier_hygiene.py \
tests/test_linkedin_session_checkpoint.py \
```

Do not add Playwright browser installation to the 3.11/3.12 matrix. The existing heavyweight job still runs `xvfb-run -a pytest tests/`.

- [ ] **Step 4: Run focused Session Autonomy verification**

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

Expected: zero failures.

- [ ] **Step 5: Re-run submission-safety adjacency**

```bash
pytest tests/test_generic_submit_dispatch_truth.py \
       tests/test_recovery_submit_dispatch_truth.py \
       tests/test_possible_submit_preservation.py \
       tests/test_receipt_freshness_edge_cases.py \
       -q --tb=short --asyncio-mode=auto
```

Expected: zero failures.

- [ ] **Step 6: Run full regression in the CI-shaped environment**

```bash
pip install -r requirements-dev.txt -r dashboard/requirements.txt
python -m playwright install chromium
xvfb-run -a pytest tests/ -q --tb=short --asyncio-mode=auto
```

Expected: zero failures. Existing skips/warnings are acceptable only if unchanged from `main` or explicitly explained in the PR.

- [ ] **Step 7: Run static/security checks**

```bash
ruff check src/ --select=E,W,F --ignore=E501,W291,E402,E741,F841,F541
```

If `gitleaks` is already installed locally, run:

```bash
gitleaks detect --source . --no-banner
```

If it is not installed, do not install extra tooling solely for this step; require the GitHub Actions `secrets` job to pass.

- [ ] **Step 8: Commit final PR-A wiring**

```bash
git add src/orchestrator.py .github/workflows/ci.yml tests/test_reauth_feature.py
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

The PR body must state that it is built from post-#121 `main`; PR #120 was selectively ported rather than merged; blocker-intelligence/adaptive-cap work is excluded; no live employer submissions were used; and list the exact focused/full test results.

- [ ] **Step 10: Request fresh code/security review and freeze scope**

Request fresh Codex/Copilot review on the final head. Fix only confirmed defects in the Session Autonomy contract. Move non-blocking cleanup and unrelated findings into follow-up PRs. Do not merge until required CI is green and no unresolved P0/P1 production-safety finding remains.

---

## Self-Review Checklist

- Every PR A requirement in the approved design has a task and acceptance test above.
- No task imports `blocker_intelligence.py` or adaptive retry behavior from PR #120.
- No task changes PR #121 receipt/ledger semantics.
- Source reauth cannot clear external ATS portal blockers.
- Background preflight has exactly one escalation owner.
- Durable dedupe does not suppress another attempt after Terminal staging fails.
- Jobs are reloaded from SQLite after `clear_session_block()` before reclassification.
- No live employer application is required for PR A verification.
