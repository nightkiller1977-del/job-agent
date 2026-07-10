import json
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

from src.session_watchdog import check_session_health, _parse_linkedin_expiry

def test_parse_linkedin_expiry_expired():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_file = Path(tmpdir) / "linkedin.json"
        now = time.time()
        # Cookie expired 5 hours ago
        session_data = {
            "cookies": [
                {"name": "li_at", "domain": ".linkedin.com", "expires": now - 5 * 3600}
            ]
        }
        session_file.write_text(json.dumps(session_data))
        
        expiry_hours = _parse_linkedin_expiry(session_file)
        assert expiry_hours is not None
        assert expiry_hours < 0
        assert pytest.approx(expiry_hours, rel=1e-2) == -5

def test_parse_linkedin_expiry_healthy():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_file = Path(tmpdir) / "linkedin.json"
        now = time.time()
        # Cookie expires in 10 hours
        session_data = {
            "cookies": [
                {"name": "li_at", "domain": ".linkedin.com", "expires": now + 10 * 3600}
            ]
        }
        session_file.write_text(json.dumps(session_data))
        
        expiry_hours = _parse_linkedin_expiry(session_file)
        assert expiry_hours is not None
        assert expiry_hours > 0
        assert pytest.approx(expiry_hours, rel=1e-2) == 10

def test_check_session_health_expired_cookie():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        session_file = session_dir / "linkedin_chromium.json"
        now = time.time()
        # Cookie expired 2 hours ago
        session_data = {
            "cookies": [
                {"name": "li_at", "domain": ".linkedin.com", "expires": now - 2 * 3600}
            ]
        }
        session_file.write_text(json.dumps(session_data))
        
        with patch("src.session_watchdog.SESSIONS_DIR", session_dir):
            results = check_session_health(["linkedin"])
            assert len(results) == 1
            health = results[0]
            assert health.status == "expired"
            assert "expired" in health.detail

def test_check_session_health_stale_cookie():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        session_file = session_dir / "linkedin_chromium.json"
        now = time.time()
        # Cookie expires in 2 hours (stale threshold)
        session_data = {
            "cookies": [
                {"name": "li_at", "domain": ".linkedin.com", "expires": now + 2 * 3600}
            ]
        }
        session_file.write_text(json.dumps(session_data))
        
        with patch("src.session_watchdog.SESSIONS_DIR", session_dir):
            results = check_session_health(["linkedin"])
            assert len(results) == 1
            health = results[0]
            assert health.status == "stale"
            assert "expire in" in health.detail
