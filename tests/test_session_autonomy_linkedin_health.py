import json
import os

import src.session_watchdog as sw


def _write_linkedin_session(path, cookies):
    path.write_text(json.dumps({"cookies": cookies}))


def test_linkedin_tracking_cookie_expiry_does_not_expire_valid_auth_session(tmp_path, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(sw.time, "time", lambda: now)
    monkeypatch.setattr(sw, "SESSIONS_DIR", tmp_path)
    session = tmp_path / "linkedin_chromium.json"
    _write_linkedin_session(
        session,
        [
            {"name": "li_at", "domain": ".linkedin.com", "expires": now + 30 * 86400},
            {"name": "lidc", "domain": ".linkedin.com", "expires": now - 3600},
            {"name": "UserMatchHistory", "domain": ".linkedin.com", "expires": now - 60},
        ],
    )
    os.utime(session, (now, now))

    [health] = sw.check_session_health(["linkedin"])

    assert health.status == "healthy"
    assert "expired" not in health.detail.lower()


def test_linkedin_recent_session_without_auth_cookie_fails_closed(tmp_path, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(sw.time, "time", lambda: now)
    monkeypatch.setattr(sw, "SESSIONS_DIR", tmp_path)
    session = tmp_path / "linkedin_chromium.json"
    _write_linkedin_session(
        session,
        [
            {"name": "lidc", "domain": ".linkedin.com", "expires": now + 86400},
            {"name": "UserMatchHistory", "domain": ".linkedin.com", "expires": now + 3600},
        ],
    )
    os.utime(session, (now, now))

    [health] = sw.check_session_health(["linkedin"])

    assert health.status == "expired"
    assert "authentication cookie" in health.detail.lower()
