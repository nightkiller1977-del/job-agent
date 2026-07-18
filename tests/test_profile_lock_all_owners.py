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
