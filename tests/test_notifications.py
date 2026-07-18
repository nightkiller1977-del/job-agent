"""Phase 2b — notification dispatcher: fail-open, deduplicated, 3-class routing."""
import pytest

from src.notifications import Dispatcher, Channel, NoticeClass, notice_for


def _recorder():
    got = []
    return got, (lambda n: got.append((n["cls"], n["title"])))


def _fixed_clock():
    # deterministic time source (no wall clock in tests)
    state = {"t": 1000.0}
    return state, (lambda: state["t"])


def test_notice_for_mapping():
    assert notice_for("attempt_finished", "applied") is NoticeClass.FYI
    assert notice_for("attempt_finished", "review_ready") is NoticeClass.HUMAN_ACTION_REQUIRED
    assert notice_for("attempt_finished", "submission_unverified") is NoticeClass.HUMAN_ACTION_REQUIRED
    assert notice_for("profile_locked", "profile_locked") is NoticeClass.SUBMISSION_BLOCKED
    assert notice_for("attempt_finished", "login_required") is NoticeClass.HUMAN_ACTION_REQUIRED
    assert notice_for("attempt_finished", "captcha") is NoticeClass.SUBMISSION_BLOCKED
    assert notice_for("attempt_finished", "submit_not_found") is NoticeClass.HUMAN_ACTION_REQUIRED
    assert notice_for("attempt_finished", "form_not_reached") is NoticeClass.HUMAN_ACTION_REQUIRED
    assert notice_for("attempt_finished", "external_ats_error") is None  # transient, not notify-worthy


def test_routes_only_to_accepting_channels():
    fyi, fyi_send = _recorder()
    blocked, blk_send = _recorder()
    d = Dispatcher(channels=[
        Channel("fyi", {NoticeClass.FYI}, fyi_send),
        Channel("blocked", {NoticeClass.SUBMISSION_BLOCKED}, blk_send),
    ])
    d.dispatch(NoticeClass.SUBMISSION_BLOCKED, "blocked!", "x")
    assert blocked and not fyi           # only the blocked channel fired


def test_fail_open_isolates_channel_errors():
    good, good_send = _recorder()

    def boom(n):
        raise RuntimeError("channel down")

    d = Dispatcher(channels=[
        Channel("broken", {NoticeClass.FYI}, boom),
        Channel("good", {NoticeClass.FYI}, good_send),
    ])
    res = d.dispatch(NoticeClass.FYI, "hi")   # must NOT raise
    assert res["failed"] == ["broken"]
    assert res["sent"] == ["good"]
    assert good                                # good channel still delivered


def test_dedup_within_ttl_then_expires():
    state, clock = _fixed_clock()
    got, send = _recorder()
    d = Dispatcher(channels=[Channel("c", {NoticeClass.FYI}, send)],
                   dedup_ttl=100.0, now=clock)
    r1 = d.dispatch(NoticeClass.FYI, "same", "msg", key="k1")
    r2 = d.dispatch(NoticeClass.FYI, "same", "msg", key="k1")  # within TTL -> deduped
    assert r1["deduped"] is False and r2["deduped"] is True
    assert len(got) == 1
    state["t"] += 101                                           # past TTL
    r3 = d.dispatch(NoticeClass.FYI, "same", "msg", key="k1")
    assert r3["deduped"] is False and len(got) == 2


def test_dispatch_event_skips_non_notify_worthy():
    got, send = _recorder()
    d = Dispatcher(channels=[Channel("c", set(NoticeClass), send)])
    assert d.dispatch_event("attempt_finished", "external_ats_error", "t") is None
    assert got == []
    d.dispatch_event("attempt_finished", "applied", "done")
    assert got and got[0][0] is NoticeClass.FYI


def test_dedup_cache_evicts_expired_entries():
    state, clock = _fixed_clock()
    got, send = _recorder()
    d = Dispatcher(channels=[Channel("c", {NoticeClass.FYI}, send)], dedup_ttl=100.0, now=clock)
    d.dispatch(NoticeClass.FYI, "a", key="k1")
    d.dispatch(NoticeClass.FYI, "b", key="k2")
    assert len(d._seen) == 2
    state["t"] += 101                       # both expire
    d.dispatch(NoticeClass.FYI, "c", key="k3")
    assert len(d._seen) == 1                # k1/k2 evicted, only k3 remains


@pytest.mark.asyncio
async def test_session_dispatches_on_outcome(tmp_path, monkeypatch):
    """End-to-end 2a->2b: the session routes a terminal outcome to the dispatcher."""
    from src.events import RunLog
    from src.sources.adapters.registry import AtsAdapterRegistry
    from src.sources.adapters.session import ExternalApplySession
    from src.sources.adapters.context import AtsApplyResult
    from src.sources.adapters.idempotency import SubmissionLedger
    # reuse the fakes from the reliability test module
    from test_adapter_reliability import _RecordingAdapter, _FakePage, JOB

    got, send = _recorder()
    disp = Dispatcher(channels=[Channel("all", set(NoticeClass), send)])
    reg = AtsAdapterRegistry(fallback=_RecordingAdapter(AtsApplyResult.ok("done"), call_policy=True))
    sess = ExternalApplySession({}, registry=reg,
                                ledger=SubmissionLedger(tmp_path / "l.json"),
                                run_log=RunLog(agent="t", runs_dir=tmp_path / "runs"),
                                dispatcher=disp)

    async def _start(load_extensions=False):
        return _FakePage()

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", lambda save_session=True: _noop())
    prof = tmp_path / "jobright_profile"
    prof.mkdir()
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    await sess.apply(JOB, auto_submit=True)
    assert (NoticeClass.FYI, "greenhouse: applied") in got


async def _noop():
    return None
