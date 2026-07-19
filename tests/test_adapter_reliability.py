"""Phase 0.1-0.4 reliability substrate — receipt truth, idempotency, policy
containment, and profile lifecycle. All exercised with fakes; no browser needed.
"""
import os
import time

import pytest

from src.sources.adapters.context import AtsApplyContext, AtsApplyResult
from src.sources.adapters.base import AtsAdapter
from src.sources.adapters.registry import AtsAdapterRegistry
from src.sources.adapters.session import ExternalApplySession
from src.sources.adapters.policy import AutoSubmitPolicy, DenyAllPolicy
from src.sources.adapters.idempotency import SubmissionLedger, canonical_key
from src.sources.adapters.profile_lock import ProfileLock, ProfileLockError
from src.sources.adapters.receipt import verify_receipt


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _FakePage:
    def __init__(self, url="https://boards.greenhouse.io/acme/jobs/1", receipt=None):
        self.url = url
        self._receipt = receipt
        self.goto_called = False

    async def goto(self, url, **kw):
        self.goto_called = True
        self.url = url

    async def evaluate(self, script):
        if "thank you for" in script:
            return self._receipt
        return None


class _RecordingAdapter(AtsAdapter):
    name = "recording"

    def __init__(self, result: AtsApplyResult, call_policy: bool = False, raises: bool = False):
        self._result = result
        self._call_policy = call_policy
        self._raises = raises

    async def can_handle(self, ctx):
        return 1.0

    async def apply(self, ctx):
        if self._raises:
            raise RuntimeError("boom during apply")
        if self._call_policy:
            await ctx.policy.confirm_submit(ctx, {})
        return self._result


def _make_session(tmp_path, adapter, monkeypatch, page=None, ledger=None):
    from src.events import RunLog
    reg = AtsAdapterRegistry(fallback=adapter)
    ledger = ledger or SubmissionLedger(tmp_path / "ledger.json")
    sess = ExternalApplySession({}, registry=reg, ledger=ledger,
                                run_log=RunLog(agent="test", runs_dir=tmp_path / "runs"))
    fake_page = page or _FakePage()
    sess._closed = False

    async def _start(load_extensions=False, disable_extensions=False):
        return fake_page

    async def _close(save_session=True):
        sess._closed = True

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", _close)
    prof = tmp_path / "jobright_profile"
    prof.mkdir(exist_ok=True)
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))
    return sess, fake_page, ledger


JOB = {"url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=x&gh_jid=99"}


# --------------------------------------------------------------------------- #
# 0.1 receipt verification
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_receipt_confirmed_via_url():
    ok, sig = await verify_receipt(_FakePage(url="https://acme.com/apply/thank-you"))
    assert ok and sig.startswith("url:")


@pytest.mark.asyncio
async def test_receipt_confirmed_via_text():
    ok, sig = await verify_receipt(_FakePage(url="https://acme.com/apply", receipt="t:thank you for applying"))
    assert ok and sig == "t:thank you for applying"


@pytest.mark.asyncio
async def test_receipt_absent():
    ok, sig = await verify_receipt(_FakePage(url="https://acme.com/apply", receipt=None))
    assert ok is False and sig == ""


@pytest.mark.asyncio
async def test_receipt_url_ignores_job_title_substrings():
    # 'success'/'applied' inside a posting slug must NOT be read as a receipt
    for u in ("https://acme.com/jobs/customer-success-manager",
              "https://acme.com/jobs/applied-scientist",
              "https://acme.com/jobs/submitted-samples-analyst"):
        ok, _ = await verify_receipt(_FakePage(url=u, receipt=None))
        assert ok is False, u
    ok, _ = await verify_receipt(_FakePage(url="https://acme.com/apply/confirmation"))
    assert ok is True


# --------------------------------------------------------------------------- #
# 0.3 policy
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_autosubmit_policy_grants_only_when_allowed_and_auto_submit():
    p = AutoSubmitPolicy(allow=True)
    ctx = AtsApplyContext(page=None, job={}, profile=None, auto_submit=True, attempt_id="a1", extra={})
    assert await p.confirm_submit(ctx, {}) is True
    assert p.authorized(ctx) is True

    ctx2 = AtsApplyContext(page=None, job={}, profile=None, auto_submit=False, attempt_id="a2", extra={})
    assert await p.confirm_submit(ctx2, {}) is False
    assert p.authorized(ctx2) is False


@pytest.mark.asyncio
async def test_denyall_policy_never_authorizes():
    p = DenyAllPolicy()
    ctx = AtsApplyContext(page=None, job={}, profile=None, auto_submit=True, attempt_id="a1", extra={})
    assert await p.confirm_submit(ctx, {}) is False
    assert p.authorized(ctx) is False


# --------------------------------------------------------------------------- #
# 0.2 idempotency ledger
# --------------------------------------------------------------------------- #
def test_canonical_key_ignores_tracking_params():
    k1 = canonical_key({"url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=99&utm_source=x"})
    k2 = canonical_key({"url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=99&fbclid=abc"})
    assert k1 == k2 and k1.startswith("greenhouse|")


def test_ledger_begin_complete_and_dedupe(tmp_path):
    led = SubmissionLedger(tmp_path / "l.json")
    key = canonical_key(JOB)
    assert led.already_applied(key) is False
    led.begin(key, "att1")
    assert led.in_progress(key) is True
    led.complete(key, "att1", verified=True)
    assert led.already_applied(key) is True
    assert led.in_progress(key) is False


def test_ledger_unverified_is_not_applied(tmp_path):
    led = SubmissionLedger(tmp_path / "l.json")
    key = canonical_key(JOB)
    led.begin(key, "att1")
    led.complete(key, "att1", verified=False)
    assert led.already_applied(key) is False
    assert led.needs_reconciliation(key) is True   # must block a blind resubmit


def test_ledger_stale_in_progress(tmp_path):
    led = SubmissionLedger(tmp_path / "l.json")
    key = canonical_key(JOB)
    led.begin(key, "att1")
    # backdate the marker beyond the stale threshold
    import json
    data = json.loads((tmp_path / "l.json").read_text())
    data[key]["ts"] = time.time() - (7 * 60 * 60)
    (tmp_path / "l.json").write_text(json.dumps(data))
    assert led.in_progress(key) is True
    assert led.is_stale_in_progress(key) is True


# --------------------------------------------------------------------------- #
# 0.4 profile lock
# --------------------------------------------------------------------------- #
def test_profile_lock_is_exclusive_across_processes(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    # a LIVE foreign process (pid 1: always alive, never ours) holds the profile
    prof.with_suffix(".applylock").write_text("1")
    with pytest.raises(ProfileLockError):
        ProfileLock(prof, timeout=0).acquire()
    # released (foreign lock gone) -> acquire succeeds
    prof.with_suffix(".applylock").unlink()
    b = ProfileLock(prof).acquire()
    b.release()


def test_profile_lock_reclaims_dead_pid(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    lockfile = prof.with_suffix(".applylock")
    lockfile.write_text("999999999")  # a PID that is not alive
    lk = ProfileLock(prof).acquire()   # should reclaim the stale lock
    assert lockfile.read_text().strip() == str(os.getpid())
    lk.release()


# --------------------------------------------------------------------------- #
# session integration (0.2 + 0.3 + 0.4)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_downgrades_submit_that_bypassed_policy(tmp_path, monkeypatch):
    """An adapter that reports submitted WITHOUT passing the gate is downgraded."""
    bypass = _RecordingAdapter(AtsApplyResult(submitted=True, status="applied", verified=True),
                               call_policy=False)
    sess, page, led = _make_session(tmp_path, bypass, monkeypatch)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.submitted is False
    assert res.status == "submission_unverified"
    assert sess._closed is True                       # 0.4 browser closed in finally
    assert led.already_applied(canonical_key(JOB)) is False


@pytest.mark.asyncio
async def test_session_honors_authorized_verified_submit(tmp_path, monkeypatch):
    good = _RecordingAdapter(AtsApplyResult.ok("done"), call_policy=True)
    sess, page, led = _make_session(tmp_path, good, monkeypatch)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.submitted is True and res.verified is True and res.status == "applied"
    assert res.attempt_id
    assert led.already_applied(canonical_key(JOB)) is True   # 0.2 recorded verified


@pytest.mark.asyncio
async def test_session_prevents_duplicate_without_launching(tmp_path, monkeypatch):
    led = SubmissionLedger(tmp_path / "l.json")
    led.complete(canonical_key(JOB), "prev", verified=True)
    adapter = _RecordingAdapter(AtsApplyResult.ok())
    sess, page, _ = _make_session(tmp_path, adapter, monkeypatch, ledger=led)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "duplicate_application_prevented"
    assert page.goto_called is False                  # never even launched
    assert sess._closed is False


@pytest.mark.asyncio
async def test_session_blocks_unresolved_in_progress(tmp_path, monkeypatch):
    led = SubmissionLedger(tmp_path / "l.json")
    led.begin(canonical_key(JOB), "prev")
    sess, page, _ = _make_session(tmp_path, _RecordingAdapter(AtsApplyResult.ok()), monkeypatch, ledger=led)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "submit_in_progress"
    assert page.goto_called is False


@pytest.mark.asyncio
async def test_session_blocks_unverified_until_reconciled(tmp_path, monkeypatch):
    led = SubmissionLedger(tmp_path / "l.json")
    led.complete(canonical_key(JOB), "prev", verified=False)   # a prior unconfirmed submit
    sess, page, _ = _make_session(tmp_path, _RecordingAdapter(AtsApplyResult.ok()), monkeypatch, ledger=led)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "submit_unverified_unresolved"
    assert page.goto_called is False                            # never resubmits blindly


@pytest.mark.asyncio
async def test_session_clears_marker_on_non_submit_outcome(tmp_path, monkeypatch):
    # a pre-submit blocker (login wall) must NOT leave an unverified marker that would
    # permanently block the job — only an actual click can mark unverified.
    led = SubmissionLedger(tmp_path / "l.json")
    adapter = _RecordingAdapter(AtsApplyResult.blocked("login_required", "wall"))
    sess, page, _ = _make_session(tmp_path, adapter, monkeypatch, ledger=led)
    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "login_required"
    key = canonical_key(JOB)
    assert led.needs_reconciliation(key) is False
    assert led.in_progress(key) is False


@pytest.mark.asyncio
async def test_session_refuses_when_profile_locked(tmp_path, monkeypatch):
    adapter = _RecordingAdapter(AtsApplyResult.ok())
    sess, page, _ = _make_session(tmp_path, adapter, monkeypatch)
    # simulate a live FOREIGN process holding the profile (pid 1: alive, not ours —
    # our own pid would be a reentrant borrow, which is allowed by design)
    lockfile = (tmp_path / "jobright_profile").with_suffix(".applylock")
    lockfile.write_text("1")
    res = await sess.apply(JOB, auto_submit=True)
    assert res.status == "profile_locked"
    assert page.goto_called is False


@pytest.mark.asyncio
async def test_session_emits_attempt_lifecycle_events(tmp_path, monkeypatch):
    """Phase 2a: the session emits a structured per-run event stream."""
    from src.events import read_run
    good = _RecordingAdapter(AtsApplyResult.ok("done"), call_policy=True)
    sess, page, led = _make_session(tmp_path, good, monkeypatch)
    await sess.apply(JOB, auto_submit=True)
    events = read_run(sess.run_log.run_id, runs_dir=tmp_path / "runs")
    names = [e["event"] for e in events]
    assert "attempt_started" in names
    assert "form_reached" in names
    assert "attempt_finished" in names
    finished = next(e for e in events if e["event"] == "attempt_finished")
    assert finished["phase"] == "receipt_verified" and finished["outcome"] == "applied"
    assert finished["vendor"] == "greenhouse"
    # never leak the raw URL / PII — only the host is recorded
    assert all("resume" not in e and "url" not in e for e in events)


@pytest.mark.asyncio
async def test_session_closes_browser_and_releases_lock_on_error(tmp_path, monkeypatch):
    from src.events import read_run
    boom = _RecordingAdapter(AtsApplyResult.ok(), raises=True)
    sess, page, _ = _make_session(tmp_path, boom, monkeypatch)
    with pytest.raises(RuntimeError):
        await sess.apply(JOB, auto_submit=True)
    assert sess._closed is True                       # 0.4 finally ran
    lockfile = (tmp_path / "jobright_profile").with_suffix(".applylock")
    assert lockfile.exists() is False                 # lock released
    # a crash must still close the attempt in the audit stream (no dangling start)
    events = read_run(sess.run_log.run_id, runs_dir=tmp_path / "runs")
    finished = [e for e in events if e["event"] == "attempt_finished"]
    assert finished and finished[-1]["outcome"] == "error"


def test_host_uses_hostname_not_netloc():
    from src.sources.adapters.session import _host
    assert _host("https://token@boards.greenhouse.io/acme/jobs/1") == "boards.greenhouse.io"
    assert _host("https://user:pass@careers.example.com:443/x") == "careers.example.com"
