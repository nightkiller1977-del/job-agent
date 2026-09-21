"""ACES-403 — unit tests for the auth-blocker triage spike
(src/discovery/auth_blocker_triage.py).

No live network, no real state/jobs.db: fakes stand in for both
StateManager and Playwright so these run in CI without a browser or a real
database.
"""
from __future__ import annotations

import json

import pytest

from src.discovery import auth_blocker_triage as triage
from src.blocker_classifier import BlockerClass, _STATUS_TO_CLASS


# --------------------------------------------------------------------------- #
# auth_blocked_jobs — aggregates across every AUTH_REQUIRED status, dedupes
# --------------------------------------------------------------------------- #

class _FakeStateManager:
    """Deliberately read-only: only get_jobs_by_status exists. If triage code
    ever tried to write, calling a nonexistent method would raise — that's
    the regression guard for "this tool never mutates jobs.db"."""

    def __init__(self, jobs_by_status: dict[str, list[dict]]):
        self._jobs_by_status = jobs_by_status
        self.calls = []

    def get_jobs_by_status(self, status: str) -> list[dict]:
        self.calls.append(status)
        return self._jobs_by_status.get(status, [])


def test_auth_blocked_jobs_only_queries_auth_required_statuses():
    state = _FakeStateManager({})
    triage.auth_blocked_jobs(state)

    auth_statuses = {s for s, cls in _STATUS_TO_CLASS.items() if cls == BlockerClass.AUTH_REQUIRED}
    non_auth_statuses = {s for s, cls in _STATUS_TO_CLASS.items() if cls != BlockerClass.AUTH_REQUIRED}

    assert set(state.calls) == auth_statuses
    assert set(state.calls).isdisjoint(non_auth_statuses)


def test_auth_blocked_jobs_aggregates_and_dedupes_by_job_id():
    state = _FakeStateManager({
        "workday_session_expired": [
            {"job_id": "j1", "source": "workday", "status": "workday_session_expired", "url": "https://a"},
        ],
        "needs_session_prep": [
            {"job_id": "j2", "source": "jobright", "status": "needs_session_prep", "url": "https://b"},
            # same job_id surfacing under a second status string must not duplicate
            {"job_id": "j1", "source": "workday", "status": "needs_session_prep", "url": "https://a"},
        ],
    })
    jobs = triage.auth_blocked_jobs(state)
    ids = [j["job_id"] for j in jobs]
    assert sorted(ids) == ["j1", "j2"]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# classify_one — bot_interstitial / expired_session / inconclusive / error
# --------------------------------------------------------------------------- #

class _FakePage:
    def __init__(self, probe_result, title="Some Title"):
        self._probe_result = probe_result
        self._title = title

    async def goto(self, url, timeout=None, wait_until=None):
        return None

    async def title(self):
        return self._title

    async def evaluate(self, script):
        return self._probe_result


class _FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_context(self):
        return _FakeContext(self._page)

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, page):
        self._page = page

    async def launch(self, headless=True):
        return _FakeBrowser(self._page)


class _FakePlaywrightCM:
    def __init__(self, page):
        self._page = page

    async def __aenter__(self):
        obj = type("P", (), {})()
        obj.chromium = _FakeChromium(self._page)
        return obj

    async def __aexit__(self, *a):
        return False


def _patch_playwright(monkeypatch, page):
    monkeypatch.setattr(triage, "async_playwright", lambda: _FakePlaywrightCM(page))


@pytest.mark.asyncio
async def test_classify_one_bot_interstitial_when_challenge_present(monkeypatch):
    page = _FakePage({"password": False, "challenge": True})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "bot_interstitial"


@pytest.mark.asyncio
async def test_classify_one_bot_interstitial_wins_even_with_a_password_field(monkeypatch):
    # A challenge takes priority: the page is actively gating with a bot
    # check regardless of what else is on it.
    page = _FakePage({"password": True, "challenge": True})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "bot_interstitial"


@pytest.mark.asyncio
async def test_classify_one_expired_session_when_only_password_present(monkeypatch):
    page = _FakePage({"password": True, "challenge": False})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "expired_session"


@pytest.mark.asyncio
async def test_classify_one_inconclusive_when_neither_present(monkeypatch):
    page = _FakePage({"password": False, "challenge": False})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "inconclusive"


@pytest.mark.asyncio
async def test_classify_one_error_never_raises_on_navigation_failure(monkeypatch):
    class _BoomPage:
        async def goto(self, url, timeout=None, wait_until=None):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    _patch_playwright(monkeypatch, _BoomPage())

    out = await triage.classify_one("https://does-not-resolve.invalid/job/1")
    assert out["classification"] == "error"
    assert "ERR_NAME_NOT_RESOLVED" in out["error"]


@pytest.mark.asyncio
async def test_classify_one_closes_the_browser_it_launched(monkeypatch):
    # Unlike the Fortress-CDP leg in ACES-402, this IS a browser we launched
    # ourselves (throwaway, in-memory) — it must be closed.
    page = _FakePage({"password": False, "challenge": False})

    captured = {}

    class _CapturingChromium(_FakeChromium):
        async def launch(self, headless=True):
            b = _FakeBrowser(self._page)
            captured["browser"] = b
            return b

    class _CapturingCM(_FakePlaywrightCM):
        async def __aenter__(self):
            obj = type("P", (), {})()
            obj.chromium = _CapturingChromium(self._page)
            return obj

    monkeypatch.setattr(triage, "async_playwright", lambda: _CapturingCM(page))

    await triage.classify_one("https://example.com/job/1")
    assert captured["browser"].closed is True


# --------------------------------------------------------------------------- #
# run_triage — end to end with fakes, never touches the real filesystem/db
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_run_triage_produces_counts_and_writes_json_report(monkeypatch, tmp_path):
    state = _FakeStateManager({
        "workday_session_expired": [
            {"job_id": "j1", "source": "workday", "status": "workday_session_expired",
             "url": "https://a.example.com/job/1"},
        ],
        "needs_session_prep": [
            {"job_id": "j2", "source": "jobright", "status": "needs_session_prep",
             "url": "https://b.example.com/job/2"},
        ],
    })
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    async def _fake_classify_one(url, timeout_ms=25000):
        if "a.example.com" in url:
            return {"classification": "expired_session", "password_present": True,
                    "challenge_present": False, "title": "A", "error": None}
        return {"classification": "bot_interstitial", "password_present": False,
                "challenge_present": True, "title": "B", "error": None}

    monkeypatch.setattr(triage, "classify_one", _fake_classify_one)

    report = await triage.run_triage(state=state)

    assert report["total_auth_blocked"] == 2
    assert report["counts"]["expired_session"] == 1
    assert report["counts"]["bot_interstitial"] == 1

    out_file = tmp_path / "aces-403-auth-blocker-triage-results.json"
    assert out_file.exists()
    on_disk = json.loads(out_file.read_text())
    assert on_disk["total_auth_blocked"] == 2


@pytest.mark.asyncio
async def test_run_triage_handles_zero_auth_blocked_jobs(monkeypatch, tmp_path):
    state = _FakeStateManager({})
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    report = await triage.run_triage(state=state)

    assert report["total_auth_blocked"] == 0
    assert all(v == 0 for v in report["counts"].values())


@pytest.mark.asyncio
async def test_run_triage_never_calls_a_state_mutating_method(monkeypatch, tmp_path):
    """_FakeStateManager only implements get_jobs_by_status — any attempt to
    write (mark_applied, update_status, etc.) would raise AttributeError.
    This is the regression guard for "read-only triage, never mutates jobs.db"."""
    state = _FakeStateManager({
        "session_expired": [
            {"job_id": "j1", "source": "linkedin", "status": "session_expired",
             "url": "https://a.example.com/job/1"},
        ],
    })
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(triage, "classify_one",
                        lambda url, timeout_ms=25000: _immediate({
                            "classification": "inconclusive", "password_present": False,
                            "challenge_present": False, "title": "", "error": None,
                        }))

    report = await triage.run_triage(state=state)
    assert report["total_auth_blocked"] == 1  # ran to completion without AttributeError


async def _immediate(value):
    return value
