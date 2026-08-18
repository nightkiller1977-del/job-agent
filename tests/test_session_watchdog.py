import json
import subprocess
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from src.session_watchdog import (
    check_session_health,
    _parse_linkedin_expiry,
    _resolve_tailscale_ip,
    _novnc_link,
    _send_deep_link_notification,
)

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

def _fake_run(returncode=0, stdout=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_resolve_tailscale_ip_success_on_first_candidate():
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0, "100.64.1.2\n")):
        assert _resolve_tailscale_ip() == "100.64.1.2"


def test_resolve_tailscale_ip_falls_through_failing_candidates():
    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FileNotFoundError("no such binary")
        return _fake_run(0, "100.64.9.9\n")

    with patch("src.session_watchdog.subprocess.run", side_effect=side_effect):
        assert _resolve_tailscale_ip() == "100.64.9.9"


def test_resolve_tailscale_ip_none_when_nothing_resolves():
    with patch("src.session_watchdog.subprocess.run", side_effect=FileNotFoundError("no tailscale")):
        assert _resolve_tailscale_ip() is None


def test_novnc_link_none_when_tailscale_unresolvable():
    with patch("src.session_watchdog._resolve_tailscale_ip", return_value=None):
        assert _novnc_link() is None


def test_novnc_link_built_from_resolved_ip_and_default_port(monkeypatch):
    monkeypatch.delenv("NOVNC_PORT", raising=False)
    with patch("src.session_watchdog._resolve_tailscale_ip", return_value="100.64.1.2"):
        link = _novnc_link()
        assert link == "http://100.64.1.2:6080/vnc.html?autoconnect=true&resize=scale"


def test_novnc_link_respects_port_override(monkeypatch):
    monkeypatch.setenv("NOVNC_PORT", "7777")
    with patch("src.session_watchdog._resolve_tailscale_ip", return_value="100.64.1.2"):
        link = _novnc_link()
        assert link.startswith("http://100.64.1.2:7777/")


def test_send_deep_link_notification_includes_novnc_link_when_available():
    with patch("src.session_watchdog._novnc_link", return_value="http://100.64.1.2:6080/vnc.html?autoconnect=true"), \
         patch("src.session_watchdog._stage_prepare_sessions") as mock_stage, \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", {}):
        _send_deep_link_notification("linkedin", "LinkedIn session expired.")

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][0]
        assert "jobagent://prepare-sessions?source=linkedin" in sent_text
        assert "http://100.64.1.2:6080/vnc.html?autoconnect=true" in sent_text
        assert "From your phone" in sent_text
        mock_stage.assert_called_once_with("linkedin")


def test_send_deep_link_notification_omits_novnc_link_when_unresolvable():
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions"), \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", {}):
        _send_deep_link_notification("usajobs", "USAJobs session expired.")

        sent_text = mock_send.call_args[0][0]
        assert "jobagent://prepare-sessions?source=usajobs" in sent_text
        assert "From your phone" not in sent_text
        assert "None" not in sent_text


def test_send_deep_link_notification_respects_rate_limit():
    cache = {}
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions") as mock_stage, \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", cache):
        _send_deep_link_notification("linkedin", "first")
        _send_deep_link_notification("linkedin", "second")

        mock_send.assert_called_once()
        mock_stage.assert_called_once()
