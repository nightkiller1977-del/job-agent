"""ACES-403 — unit tests for the auth-blocker triage spike
(src/discovery/auth_blocker_triage.py).

No live network, no real browser, no real state/jobs.db: fakes/tmp paths
stand in throughout.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.discovery import auth_blocker_triage as triage
from src.blocker_classifier import BlockerClass, _STATUS_TO_CLASS


# --------------------------------------------------------------------------- #
# read_jobs_with_apply_status — genuinely read-only DB access
# --------------------------------------------------------------------------- #

def _make_jobs_db(path, rows):
    """rows: list of (job_id, extra_json_dict). Builds a minimal real jobs
    table so read_jobs_with_apply_status can be exercised against an actual
    SQLite file, not just mocks."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE jobs (
            job_id TEXT, source TEXT, url TEXT, status TEXT, extra_json TEXT
        )
    """)
    for job_id, extra in rows:
        conn.execute(
            "INSERT INTO jobs (job_id, source, url, status, extra_json) VALUES (?,?,?,?,?)",
            (job_id, "workday", f"https://example.com/{job_id}", "approved", json.dumps(extra)),
        )
    conn.commit()
    conn.close()


def test_read_jobs_with_apply_status_returns_empty_list_when_db_missing(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    assert triage.read_jobs_with_apply_status(str(missing)) == []
    assert not missing.exists()  # never created it


def test_read_jobs_with_apply_status_never_creates_or_modifies_the_file(tmp_path):
    db_path = tmp_path / "jobs.db"
    _make_jobs_db(db_path, [("j1", {"apply_last_status": "workday_session_expired"})])
    before = db_path.stat().st_mtime_ns
    before_siblings = sorted(p.name for p in tmp_path.iterdir())

    triage.read_jobs_with_apply_status(str(db_path))

    after = db_path.stat().st_mtime_ns
    after_siblings = sorted(p.name for p in tmp_path.iterdir())
    assert before == after  # not modified
    assert before_siblings == after_siblings  # no -wal/-shm sidecar files created


def test_read_jobs_with_apply_status_extracts_status_and_ats_url(tmp_path):
    db_path = tmp_path / "jobs.db"
    _make_jobs_db(db_path, [
        ("j1", {"apply_last_status": "workday_session_expired", "ats_url": "https://portal.example.com/j1"}),
        ("j2", {"apply_last_status": "applied"}),  # not AUTH_REQUIRED, but has apply_last_status
        ("j3", {}),  # no apply_last_status at all — must be excluded
    ])

    jobs = triage.read_jobs_with_apply_status(str(db_path))
    ids = {j["job_id"] for j in jobs}
    assert ids == {"j1", "j2"}

    j1 = next(j for j in jobs if j["job_id"] == "j1")
    assert j1["apply_last_status"] == "workday_session_expired"
    assert j1["ats_url"] == "https://portal.example.com/j1"


# --------------------------------------------------------------------------- #
# auth_blocked_jobs — pure filter via the canonical classify()
# --------------------------------------------------------------------------- #

def test_auth_blocked_jobs_keeps_only_auth_required_statuses():
    jobs = [
        {"job_id": "j1", "apply_last_status": "workday_session_expired"},
        {"job_id": "j2", "apply_last_status": "needs_session_prep"},
        {"job_id": "j3", "apply_last_status": "applied"},
        {"job_id": "j4", "apply_last_status": "bad_ats_url"},
    ]
    kept = {j["job_id"] for j in triage.auth_blocked_jobs(jobs)}
    assert kept == {"j1", "j2"}


def test_auth_blocked_jobs_covers_every_status_blocker_classifier_maps_to_auth_required():
    auth_statuses = [s for s, cls in _STATUS_TO_CLASS.items() if cls == BlockerClass.AUTH_REQUIRED]
    jobs = [{"job_id": s, "apply_last_status": s} for s in auth_statuses]
    kept = {j["job_id"] for j in triage.auth_blocked_jobs(jobs)}
    assert kept == set(auth_statuses)


# --------------------------------------------------------------------------- #
# blocker_url — resolves the recorded external ATS portal over the source URL
# --------------------------------------------------------------------------- #

def test_blocker_url_prefers_recorded_ats_url_over_discovery_source():
    job = {"url": "https://linkedin.com/jobs/view/123",
           "ats_url": "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/456"}
    assert triage.blocker_url(job) == job["ats_url"]


def test_blocker_url_falls_back_to_source_url_when_no_ats_url_recorded():
    job = {"url": "https://boards.greenhouse.io/acme/jobs/1", "ats_url": ""}
    assert triage.blocker_url(job) == job["url"]


# --------------------------------------------------------------------------- #
# classify_one — bot_interstitial / login_wall / no_wall_detected / error
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
async def test_classify_one_login_wall_when_only_password_present(monkeypatch):
    page = _FakePage({"password": True, "challenge": False})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "login_wall"


@pytest.mark.asyncio
async def test_classify_one_no_wall_detected_when_neither_present(monkeypatch):
    page = _FakePage({"password": False, "challenge": False})
    _patch_playwright(monkeypatch, page)

    out = await triage.classify_one("https://example.com/job/1")
    assert out["classification"] == "no_wall_detected"


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
# run_triage — end to end, jobs injected directly (no db/StateManager needed)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_run_triage_produces_counts_and_writes_json_report(monkeypatch, tmp_path):
    jobs = [
        {"job_id": "j1", "source": "workday", "apply_last_status": "workday_session_expired",
         "url": "https://a.example.com/job/1", "ats_url": ""},
        {"job_id": "j2", "source": "jobright", "apply_last_status": "needs_session_prep",
         "url": "https://b.example.com/job/2", "ats_url": ""},
    ]
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    async def _fake_classify_one(url, timeout_ms=25000):
        if "a.example.com" in url:
            return {"classification": "login_wall", "password_present": True,
                    "challenge_present": False, "title": "A", "error": None}
        return {"classification": "bot_interstitial", "password_present": False,
                "challenge_present": True, "title": "B", "error": None}

    monkeypatch.setattr(triage, "classify_one", _fake_classify_one)

    report = await triage.run_triage(jobs=jobs)

    assert report["total_auth_blocked"] == 2
    assert report["counts"]["login_wall"] == 1
    assert report["counts"]["bot_interstitial"] == 1

    out_file = tmp_path / "aces-403-auth-blocker-triage-results.json"
    assert out_file.exists()
    on_disk = json.loads(out_file.read_text())
    assert on_disk["total_auth_blocked"] == 2


@pytest.mark.asyncio
async def test_run_triage_handles_zero_auth_blocked_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    report = await triage.run_triage(jobs=[])

    assert report["total_auth_blocked"] == 0
    assert all(v == 0 for v in report["counts"].values())


@pytest.mark.asyncio
async def test_run_triage_uses_the_ats_url_not_the_discovery_source_url(monkeypatch, tmp_path):
    """A LinkedIn-discovered Workday blocker must classify the Workday
    portal, not the LinkedIn listing (Copilot + Codex review, PR #140)."""
    jobs = [
        {"job_id": "j1", "source": "linkedin", "apply_last_status": "workday_session_expired",
         "url": "https://linkedin.com/jobs/view/123",
         "ats_url": "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/456"},
    ]
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    seen_urls = []

    async def _fake_classify_one(url, timeout_ms=25000):
        seen_urls.append(url)
        return {"classification": "login_wall", "password_present": True,
                "challenge_present": False, "title": "", "error": None}

    monkeypatch.setattr(triage, "classify_one", _fake_classify_one)

    await triage.run_triage(jobs=jobs)

    assert seen_urls == ["https://acme.wd1.myworkdayjobs.com/en-US/careers/job/456"]


@pytest.mark.asyncio
async def test_run_triage_reads_from_db_path_when_no_jobs_injected(monkeypatch, tmp_path):
    """The default (CLI) path reads via read_jobs_with_apply_status, not a
    StateManager — confirms the two layers are actually wired together."""
    db_path = tmp_path / "jobs.db"
    _make_jobs_db(db_path, [("j1", {"apply_last_status": "workday_session_expired"})])
    monkeypatch.setattr(triage, "RESULTS_DIR", tmp_path)

    async def _fake_classify_one(url, timeout_ms=25000):
        return {"classification": "no_wall_detected", "password_present": False,
                "challenge_present": False, "title": "", "error": None}

    monkeypatch.setattr(triage, "classify_one", _fake_classify_one)

    report = await triage.run_triage(db_path=str(db_path))
    assert report["total_auth_blocked"] == 1
