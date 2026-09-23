import json
import subprocess
import sys
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
    _prepare_sessions_source,
    _prepare_sessions_command,
    _stage_prepare_sessions,
    _venv_activate_parts,
    StagingResult,
)

_STAGED = StagingResult(staged=True, supported=True)
_STAGE_FAILED = StagingResult(staged=False, supported=True, detail="launcher failed")
_STAGE_UNSUPPORTED = StagingResult(staged=False, supported=False, detail="no_terminal_available")

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

def _fake_run(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _osascript_from_run(mock_run):
    args = mock_run.call_args[0][0]
    assert Path(args[0]).name == "osascript"
    assert args[1] == "-e"
    return args[2]


def _fake_which(*available: str):
    """shutil.which stub: resolve only the named executables, to a fake abs path.

    Terminal-launcher resolution must be driven by the test, never by whatever
    happens to be installed on the machine running the suite.
    """
    allowed = set(available)

    def which(name, *args, **kwargs):
        return f"/usr/bin/{name}" if name in allowed else None

    return which


def _headless(monkeypatch):
    """Strip the desktop-session markers so POSIX staging is unsupported."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)


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


def test_prepare_sessions_source_maps_supported_aliases_only():
    assert _prepare_sessions_source("linkedin") == "linkedin"
    assert _prepare_sessions_source("usajobs") == "usajobs"
    assert _prepare_sessions_source("indeed") == "indeed"
    assert _prepare_sessions_source("jobright") == "jobright"
    assert _prepare_sessions_source("linkedin-saved") == "linkedin"
    assert _prepare_sessions_source(None) is None


def test_prepare_sessions_source_omits_jobspy_backed_filters():
    assert _prepare_sessions_source("external") is None
    assert _prepare_sessions_source("glassdoor") is None
    assert _prepare_sessions_source("ziprecruiter") is None
    assert _prepare_sessions_source("google") is None
    assert _prepare_sessions_source("jobspy") is None


def test_prepare_sessions_command_omits_unknown_source():
    cmd, mapped_source = _prepare_sessions_command("unknown-provider")

    assert mapped_source is None
    assert cmd.endswith("prepare-sessions")
    assert "--source" not in cmd


def test_stage_prepare_sessions_omits_jobspy_backed_source_filter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("osascript"))
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("glassdoor") == StagingResult(staged=True, supported=True)
    script = _osascript_from_run(mock_run)
    assert "prepare-sessions" in script
    assert "--source" not in script
    assert "glassdoor" not in script
    assert "jobright" not in script


def test_headless_session_notification_still_delivers_with_manual_command(tmp_path, monkeypatch):
    """A host with no terminal must still reach the human — the old code returned
    early here, so 205 live apply attempts produced zero delivered session alerts."""
    monkeypatch.setattr(sys, "platform", "linux")
    _headless(monkeypatch)
    monkeypatch.setattr("src.notifier.STATUS_FILE", tmp_path / "status.json")
    sent = []
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog.subprocess.run") as run, \
         patch("src.notifier._send_telegram", side_effect=lambda m: sent.append(m)), \
         patch("src.notifier._desktop_notify") as desktop, \
         patch("src.notifier._last_notification_times", {}):
        _send_deep_link_notification("linkedin", "session expired")
        _send_deep_link_notification("linkedin", "session expired")

    assert not run.called
    # Delivered exactly once — the 12h dedupe still applies.
    assert len(sent) == 1
    assert desktop.call_count == 1
    assert "python src/main.py prepare-sessions --source linkedin" in sent[0]
    assert "No terminal on this host" in sent[0]

    status = json.loads((tmp_path / "status.json").read_text())
    conditions = status["secondary_conditions"]
    assert len(conditions) == 1
    assert conditions[0]["primary_kind"] == "session_recovery_required"
    assert conditions[0]["kind"] == "terminal_staging_unavailable"
    assert conditions[0]["operation"] == "prepare_sessions_terminal"


def test_stage_prepare_sessions_keeps_supported_source_filter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("osascript"))
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("linkedin").staged is True

    script = _osascript_from_run(mock_run)
    assert "prepare-sessions --source linkedin" in script


def test_stage_prepare_sessions_reports_retryable_failure_on_osascript_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("osascript"))
    with patch(
        "src.session_watchdog.subprocess.run",
        return_value=_fake_run(1, stderr="not authorized"),
    ):
        result = _stage_prepare_sessions("linkedin")

    # A launcher that exists but fails is transient: supported, so the caller
    # retries next pass instead of burning the dedupe window.
    assert result.staged is False
    assert result.supported is True
    assert "not authorized" in result.detail


def test_stage_prepare_sessions_unsupported_when_darwin_lacks_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which())
    with patch("src.session_watchdog.subprocess.run") as mock_run:
        result = _stage_prepare_sessions("linkedin")

    assert result == StagingResult(staged=False, supported=False, detail="no_terminal_available")
    mock_run.assert_not_called()


def test_stage_prepare_sessions_never_calls_osascript_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("xterm", "bash"))
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("linkedin").staged is True

    argv = mock_run.call_args[0][0]
    assert "osascript" not in " ".join(argv)


def test_stage_prepare_sessions_launches_detected_linux_terminal(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("gnome-terminal", "bash"))
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("linkedin").staged is True

    argv = mock_run.call_args[0][0]
    assert argv[:4] == ["/usr/bin/gnome-terminal", "--", "/usr/bin/bash", "-lc"]
    assert "prepare-sessions --source linkedin" in argv[4]


def test_stage_prepare_sessions_prefers_configured_default_terminal(monkeypatch):
    """x-terminal-emulator is the user's own Debian alternative — honour it before
    guessing at a specific emulator."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(
        "src.session_watchdog.shutil.which",
        _fake_which("x-terminal-emulator", "gnome-terminal", "xterm", "bash"),
    )
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("linkedin").staged is True

    argv = mock_run.call_args[0][0]
    assert argv[0] == "/usr/bin/x-terminal-emulator"
    assert argv[1] == "-e"


def test_stage_prepare_sessions_unsupported_on_headless_posix(monkeypatch):
    """The production failure mode: scheduler run, terminal installed, no desktop."""
    monkeypatch.setattr(sys, "platform", "linux")
    _headless(monkeypatch)
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("gnome-terminal", "bash"))
    with patch("src.session_watchdog.subprocess.run") as mock_run:
        result = _stage_prepare_sessions("linkedin")

    assert result.supported is False
    mock_run.assert_not_called()


def test_stage_prepare_sessions_unsupported_when_no_terminal_emulator(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("bash"))
    with patch("src.session_watchdog.subprocess.run") as mock_run:
        assert _stage_prepare_sessions("linkedin").supported is False
    mock_run.assert_not_called()


def test_stage_prepare_sessions_unsupported_without_bash(monkeypatch):
    """`source .venv/bin/activate` is a bash builtin — opening a window that
    cannot activate the venv is worse than reporting the gap."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which("xterm"))
    with patch("src.session_watchdog.subprocess.run") as mock_run:
        assert _stage_prepare_sessions("linkedin").supported is False
    mock_run.assert_not_called()


def test_stage_prepare_sessions_uses_windows_console(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which())
    with patch("src.session_watchdog.subprocess.run", return_value=_fake_run(0)) as mock_run:
        assert _stage_prepare_sessions("linkedin").staged is True

    argv = mock_run.call_args[0][0]
    assert argv[:6] == [r"C:\Windows\System32\cmd.exe", "/c", "start", "", "cmd", "/k"]
    assert r".venv\Scripts\activate.bat" in argv[6]
    assert "prepare-sessions --source linkedin" in argv[6]


def test_stage_prepare_sessions_unsupported_on_windows_without_comspec(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("COMSPEC", raising=False)
    monkeypatch.setattr("src.session_watchdog.shutil.which", _fake_which())
    with patch("src.session_watchdog.subprocess.run") as mock_run:
        assert _stage_prepare_sessions("linkedin").supported is False
    mock_run.assert_not_called()


def test_venv_activate_parts_are_platform_specific(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _venv_activate_parts() == ["source", ".venv/bin/activate"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _venv_activate_parts() == ["source", ".venv/bin/activate"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert _venv_activate_parts() == [r".venv\Scripts\activate.bat"]


def test_send_deep_link_notification_includes_novnc_link_when_available(tmp_path):
    with patch("src.session_watchdog._novnc_link", return_value="http://100.64.1.2:6080/vnc.html?autoconnect=true"), \
         patch("src.session_watchdog._stage_prepare_sessions", return_value=_STAGED) as mock_stage, \
         patch("src.notifier.STATUS_FILE", tmp_path / "status.json"), \
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


def test_send_deep_link_notification_omits_jobspy_backed_deep_link_source(tmp_path):
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions", return_value=_STAGED), \
         patch("src.notifier.STATUS_FILE", tmp_path / "status.json"), \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", {}):
        _send_deep_link_notification("glassdoor", "Glassdoor session expired.")

        sent_text = mock_send.call_args[0][0]
        assert sent_text.endswith("jobagent://prepare-sessions")
        assert "source=" not in sent_text


def test_send_deep_link_notification_omits_novnc_link_when_unresolvable(tmp_path):
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions", return_value=_STAGED), \
         patch("src.notifier.STATUS_FILE", tmp_path / "status.json"), \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", {}):
        _send_deep_link_notification("usajobs", "USAJobs session expired.")

        sent_text = mock_send.call_args[0][0]
        assert "jobagent://prepare-sessions?source=usajobs" in sent_text
        assert "From your phone" not in sent_text
        assert "None" not in sent_text


def test_send_deep_link_notification_respects_rate_limit(tmp_path):
    cache = {}
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions", return_value=_STAGED) as mock_stage, \
         patch("src.notifier.STATUS_FILE", tmp_path / "status.json"), \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", cache):
        _send_deep_link_notification("linkedin", "first")
        _send_deep_link_notification("linkedin", "second")

        mock_send.assert_called_once()
        mock_stage.assert_called_once()


def test_send_deep_link_notification_retries_staging_without_sending_when_stage_fails(tmp_path):
    cache = {}
    status_file = tmp_path / "status.json"
    with patch("src.session_watchdog._novnc_link", return_value=None), \
         patch("src.session_watchdog._stage_prepare_sessions", return_value=_STAGE_FAILED) as mock_stage, \
         patch("src.notifier.STATUS_FILE", status_file), \
         patch("src.notifier._send_telegram") as mock_send, \
         patch("src.notifier._desktop_notify"), \
         patch("src.notifier._last_notification_times", cache):
        _send_deep_link_notification("linkedin", "first")
        _send_deep_link_notification("linkedin", "second")

        assert mock_send.call_count == 0
        assert mock_stage.call_count == 2
        data = json.loads(status_file.read_text()) if status_file.exists() else {}
        assert "notification_dedupe" not in data
