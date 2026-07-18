"""All-owners profile-lock protocol: every BaseScraper._start_browser participates,
a live foreign owner is refused (never pkill'ed), and the lock is reentrant so
ExternalApplySession's outer lock composes with the base acquisition.
"""
import os

import pytest

import src.sources.base as base_mod
from src.sources.adapters.profile_lock import ProfileLock, ProfileLockError


# --------------------------------------------------------------------------- #
# reentrancy
# --------------------------------------------------------------------------- #
def test_reentrant_acquire_same_process(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    outer = ProfileLock(prof).acquire()
    inner = ProfileLock(prof).acquire()        # same PID -> borrow, not deadlock
    assert inner._borrowed is True
    lockfile = prof.with_suffix(".applylock")
    inner.release()
    assert lockfile.exists()                    # inner release keeps the file
    outer.release()
    assert not lockfile.exists()                # outermost release removes it
    # fully released -> a fresh acquire works and is not a borrow
    again = ProfileLock(prof).acquire()
    assert again._borrowed is False
    again.release()


def test_foreign_live_owner_still_refused(tmp_path):
    prof = tmp_path / "p"
    prof.mkdir()
    # simulate a live FOREIGN process (pid 1 is always alive, never ours)
    prof.with_suffix(".applylock").write_text("1")
    with pytest.raises(ProfileLockError):
        ProfileLock(prof, timeout=0).acquire()


# --------------------------------------------------------------------------- #
# base-scraper integration
# --------------------------------------------------------------------------- #
class _Scraper(base_mod.BaseScraper):
    name = "locktest"

    async def scrape(self, *a, **k):  # abstractmethod
        return []

    async def apply(self, *a, **k):  # abstractmethod
        return False


@pytest.fixture()
def scraper(tmp_path, monkeypatch):
    s = _Scraper({"search_settings": {}})
    prof = tmp_path / "locktest_profile"
    prof.mkdir()
    monkeypatch.setattr(type(s), "_profile_dir", property(lambda self: prof))
    monkeypatch.setattr(base_mod, "_PROFILE_LOCK_WAIT_S", 0.0)
    return s, prof


@pytest.mark.asyncio
async def test_start_browser_acquires_and_close_releases(scraper):
    s, prof = scraper
    await s._start_browser()
    lockfile = prof.with_suffix(".applylock")
    assert lockfile.exists()
    assert lockfile.read_text().strip() == str(os.getpid())
    await s._close_browser(save_session=False)
    assert not lockfile.exists()
    assert s._profile_lock is None


@pytest.mark.asyncio
async def test_start_browser_refuses_live_foreign_owner_without_pkill(scraper, monkeypatch):
    """A live foreign owner must be REFUSED — _clear_profile_locks (pkill) must not run."""
    s, prof = scraper
    cleared = []
    monkeypatch.setattr(s, "_clear_profile_locks", lambda: cleared.append(True))
    prof.with_suffix(".applylock").write_text("1")   # live foreign pid
    with pytest.raises(ProfileLockError):
        await s._start_browser()
    assert cleared == []                              # never reached the pkill path
    assert s._profile_lock is None


@pytest.mark.asyncio
async def test_start_browser_reclaims_dead_owner(scraper):
    s, prof = scraper
    prof.with_suffix(".applylock").write_text("999999999")  # dead pid
    await s._start_browser()                                 # reclaims, proceeds
    assert prof.with_suffix(".applylock").read_text().strip() == str(os.getpid())
    await s._close_browser(save_session=False)


@pytest.mark.asyncio
async def test_session_outer_lock_composes_with_base(tmp_path, monkeypatch):
    """ExternalApplySession's outer lock + the real base _start_browser acquisition
    must compose via reentrancy (no deadlock), and everything releases at the end."""
    from src.events import RunLog
    from src.sources.adapters.registry import AtsAdapterRegistry
    from src.sources.adapters.session import ExternalApplySession
    from src.sources.adapters.context import AtsApplyResult
    from src.sources.adapters.idempotency import SubmissionLedger
    from test_adapter_reliability import _RecordingAdapter

    reg = AtsAdapterRegistry(fallback=_RecordingAdapter(AtsApplyResult.blocked("review_ready")))
    sess = ExternalApplySession({}, registry=reg,
                                ledger=SubmissionLedger(tmp_path / "l.json"),
                                run_log=RunLog(agent="t", runs_dir=tmp_path / "runs"))
    prof = tmp_path / "jobright_profile"
    prof.mkdir()
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))
    monkeypatch.setattr(base_mod, "_PROFILE_LOCK_WAIT_S", 0.0)

    class _P:
        url = "https://boards.greenhouse.io/acme/jobs/1"

        async def goto(self, *a, **k):
            pass

        async def evaluate(self, *a, **k):
            return None

    async def _start(load_extensions=False):
        # the REAL base acquisition (inner, reentrant) without launching Chrome
        from src.sources.adapters.profile_lock import ProfileLock as _PL
        sess._profile_lock = _PL(prof).acquire()
        return _P()

    monkeypatch.setattr(sess, "_start_browser", _start)

    async def _close(save_session=True):
        lock = getattr(sess, "_profile_lock", None)
        if lock is not None:
            lock.release()
            sess._profile_lock = None

    monkeypatch.setattr(sess, "_close_browser", _close)

    res = await sess.apply({"url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert res.status == "review_ready"               # no profile_locked deadlock
    assert not prof.with_suffix(".applylock").exists()  # fully released


@pytest.mark.asyncio
async def test_lock_released_when_startup_fails(scraper, monkeypatch):
    """Codex P1: a launch failure after acquisition must release the lock, or the
    still-live process holds the profile forever."""
    s, prof = scraper

    async def _boom(load_extensions, use_chromium_fallback):
        raise RuntimeError("chrome failed to start")

    monkeypatch.setattr(s, "_launch_browser", _boom)
    with pytest.raises(RuntimeError):
        await s._start_browser()
    assert s._profile_lock is None
    assert not prof.with_suffix(".applylock").exists()   # lock released
    # another owner can acquire immediately
    lk = ProfileLock(prof).acquire(); lk.release()


@pytest.mark.asyncio
async def test_chromium_fallback_takes_no_lock(scraper, monkeypatch):
    """Codex P1: the JSON-session bundled-Chromium path never opens the profile dir,
    so it must not take (or wait on) the profile lock."""
    s, prof = scraper
    monkeypatch.setattr(s, "_should_use_chromium_fallback", lambda: True)
    launched = {}

    async def _fake_launch(load_extensions, use_chromium_fallback):
        launched["fallback"] = use_chromium_fallback
        return object()

    monkeypatch.setattr(s, "_launch_browser", _fake_launch)
    # a live FOREIGN owner holds the profile — fallback path must not care
    prof.with_suffix(".applylock").write_text("1")
    await s._start_browser()
    assert launched["fallback"] is True
    assert s._profile_lock is None
    assert prof.with_suffix(".applylock").read_text() == "1"   # untouched


@pytest.mark.asyncio
async def test_partial_launch_closed_before_release(scraper, monkeypatch):
    """Codex r2 P1: a launch that dies AFTER creating browser resources must close
    them before the lock is released (else the next owner's pkill hits live Chrome)."""
    s, prof = scraper
    order = []

    class _Ctx:
        async def close(self):
            order.append("context_closed")

    async def _boom(load_extensions, use_chromium_fallback):
        s._context = _Ctx()          # partial launch: context exists...
        raise RuntimeError("new_page failed")  # ...then a later step dies

    monkeypatch.setattr(s, "_launch_browser", _boom)
    real_release = ProfileLock.release

    def _spy_release(self):
        order.append("lock_released")
        real_release(self)

    monkeypatch.setattr(ProfileLock, "release", _spy_release)
    with pytest.raises(RuntimeError):
        await s._start_browser()
    assert order.index("context_closed") < order.index("lock_released")
    assert s._context is None
    assert not prof.with_suffix(".applylock").exists()


@pytest.mark.asyncio
async def test_close_browser_releases_lock_on_cancellation(scraper, monkeypatch):
    """Codex r2 P2: cancellation during teardown must still release the lock."""
    import asyncio as _asyncio
    s, prof = scraper

    async def _cancelled_export():
        raise _asyncio.CancelledError()

    monkeypatch.setattr(s, "_export_session_json", _cancelled_export)
    s._profile_lock = ProfileLock(prof).acquire()
    with pytest.raises(_asyncio.CancelledError):
        await s._close_browser(save_session=True)
    assert s._profile_lock is None
    assert not prof.with_suffix(".applylock").exists()


def test_independent_same_process_operation_does_not_borrow(tmp_path):
    """Codex r3 P1: a DIFFERENT logical operation in the same process must not
    borrow the lock just because the PID matches — it waits/refuses instead."""
    import contextvars
    prof = tmp_path / "p"
    prof.mkdir()

    # operation A acquires in its own context
    ctx_a = contextvars.copy_context()
    lock_a = ctx_a.run(lambda: ProfileLock(prof).acquire())

    # operation B (independent context, same PID) must be refused, not borrowed
    ctx_b = contextvars.copy_context()
    with pytest.raises(ProfileLockError):
        ctx_b.run(lambda: ProfileLock(prof, timeout=0).acquire())

    # nested acquire INSIDE operation A's context still borrows
    def _nested():
        inner = ProfileLock(prof).acquire()
        assert inner._borrowed is True
        inner.release()

    ctx_a.run(_nested)
    ctx_a.run(lock_a.release)
    assert not prof.with_suffix(".applylock").exists()


@pytest.mark.asyncio
async def test_external_cancellation_completes_teardown_before_release(scraper):
    """Codex r3 P2: cancelling the owning task mid-close must not release the lock
    before the browser resources are closed — the shielded teardown finishes first."""
    import asyncio as _asyncio
    s, prof = scraper
    order = []
    release_gate = _asyncio.Event()

    class _SlowCtx:
        async def close(self):
            await release_gate.wait()      # keep teardown in-flight while we cancel
            order.append("context_closed")

    s._context = _SlowCtx()
    s._profile_lock = ProfileLock(prof).acquire()

    task = _asyncio.ensure_future(s._close_browser(save_session=False))
    await _asyncio.sleep(0.05)             # let teardown reach context.close()
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task
    # lock must still be held: teardown is completing in the background
    assert prof.with_suffix(".applylock").exists()
    release_gate.set()
    # let the shielded inner task finish (includes the 2s SQLite-flush sleep)
    for _ in range(60):
        if not prof.with_suffix(".applylock").exists():
            break
        await _asyncio.sleep(0.1)
    assert order == ["context_closed"]
    assert not prof.with_suffix(".applylock").exists()   # released AFTER close


@pytest.mark.asyncio
async def test_release_from_other_task_revokes_ownership(tmp_path):
    """Codex r4 P1: a release performed in a DIFFERENT task (the shielded teardown)
    must fully revoke ownership — the original chain must not retain a stale borrow
    right against a later independent owner."""
    import asyncio as _asyncio
    import contextvars as _cv
    prof = tmp_path / "p"
    prof.mkdir()

    lock = ProfileLock(prof).acquire()          # outer acquire in THIS chain

    async def _release_elsewhere():
        lock.release()                           # separate task, copied context

    await _asyncio.ensure_future(_release_elsewhere())
    assert not prof.with_suffix(".applylock").exists()

    # an INDEPENDENT operation can now own the profile...
    ctx_b = _cv.copy_context()
    lock_b = ctx_b.run(lambda: ProfileLock(prof).acquire())
    # ...and OUR (stale) chain must NOT borrow it, despite the matching PID
    with pytest.raises(ProfileLockError):
        ProfileLock(prof, timeout=0).acquire()
    ctx_b.run(lock_b.release)


@pytest.mark.asyncio
async def test_acquire_async_does_not_block_event_loop(tmp_path):
    """Codex r4 P2: waiting on a contended lock must keep the event loop running."""
    import asyncio as _asyncio
    prof = tmp_path / "p"
    prof.mkdir()
    prof.with_suffix(".applylock").write_text("1")   # live foreign owner

    ticks = []

    async def _ticker():
        while True:
            ticks.append(1)
            await _asyncio.sleep(0.02)

    t = _asyncio.ensure_future(_ticker())
    with pytest.raises(ProfileLockError):
        await ProfileLock(prof, timeout=0.4, poll=0.05).acquire_async()
    t.cancel()
    # the loop kept servicing other tasks during the ~0.4s contention window
    assert len(ticks) >= 5
