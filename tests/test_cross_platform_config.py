"""ACES-430: platform-dependent paths must resolve on macOS, Linux and Windows.

Two hardcoded-macOS defects, both silent on other platforms:

1. ``ConfigLoader._merge_ai_commander_settings`` built the settings path as
   ``~/Library/Application Support/...`` unconditionally, so on Linux/Windows
   ``settings-v3.json`` was never found and that whole config layer was skipped
   without a warning — the agent ran on defaults while appearing healthy.
2. ``main._scheduler_unit_statuses`` (formerly an inline launchd-only block)
   reported launchd plists everywhere, so a Linux operator was told the
   schedulers were "NOT INSTALLED" while the systemd timers actually driving
   the agent went unreported.

All filesystem/platform state is faked — no real writes outside tmp_path, no
subprocesses spawned against the host's real init system.
"""
import json
import subprocess

import pytest

import src.main as main_mod
from src.agent_config import ConfigLoader

# ─── settings-v3.json resolution ────────────────────────────────────────────

def _settings_payload() -> dict:
    return {"jobAgent": {"browserRecovery": {"maxSteps": 99}}}


@pytest.mark.parametrize(
    "platform,subpath",
    [
        ("darwin", ("Library", "Application Support", "ai-command-center")),
        ("linux", (".config", "ai-command-center")),
        ("win32", ("AppData", "Roaming", "ai-command-center")),
    ],
)
def test_settings_found_on_every_platform(monkeypatch, tmp_path, platform, subpath):
    """The settings layer must actually load on each platform, not just macOS."""
    home = tmp_path / "home"
    settings_dir = home.joinpath(*subpath)
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings-v3.json").write_text(json.dumps(_settings_payload()))

    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))       # Windows Path.home()
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("AICC_SECRETS_DIR", raising=False)
    import src.secret_store as ss
    monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: home))

    merged = ConfigLoader()._merge_ai_commander_settings({"jobAgent": {}})

    assert merged["jobAgent"]["browser_recovery"]["max_steps"] == 99, (
        f"settings-v3.json was not picked up on {platform}"
    )


def test_settings_lookup_is_independent_of_aicc_secrets_dir_override(monkeypatch, tmp_path):
    """Secrets may live elsewhere; settings stay under platform user-data."""
    home = tmp_path / "home"
    settings_dir = home / ".config" / "ai-command-center"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings-v3.json").write_text(json.dumps(_settings_payload()))
    secret_store = tmp_path / "custom-secrets"
    secret_store.mkdir()
    (secret_store / "settings-v3.json").write_text(json.dumps({"jobAgent": {"browserRecovery": {"maxSteps": 7}}}))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("AICC_SECRETS_DIR", str(secret_store))
    import src.secret_store as ss
    monkeypatch.setattr(ss.Path, "home", staticmethod(lambda: home))

    merged = ConfigLoader()._merge_ai_commander_settings({"jobAgent": {}})
    assert merged["jobAgent"]["browser_recovery"]["max_steps"] == 99


def test_absent_settings_file_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("AICC_SECRETS_DIR", str(tmp_path / "nothing-here"))
    base = {"jobAgent": {"sentinel": True}}
    assert ConfigLoader()._merge_ai_commander_settings(base) == base


def test_malformed_settings_file_does_not_crash(monkeypatch, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "settings-v3.json").write_text("{ this is not json")
    monkeypatch.setenv("AICC_SECRETS_DIR", str(store))

    base = {"jobAgent": {"sentinel": True}}
    assert ConfigLoader()._merge_ai_commander_settings(base) == base


# ─── scheduler unit reporting ───────────────────────────────────────────────

def test_macos_reports_launchd_units(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "darwin")
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.jobagent.discover.plist").write_text("<plist/>")
    monkeypatch.setattr(main_mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        main_mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="com.jobagent.discover\n", stderr=""),
    )

    rows = dict(main_mod._scheduler_unit_statuses())

    assert "ACTIVE" in rows["com.jobagent.discover"]
    assert "NOT INSTALLED" in rows["com.jobagent.apply"]


def test_linux_reports_systemd_timers_not_launchd(monkeypatch):
    """The ACES-430 regression: a Linux box must not be told launchd is missing."""
    monkeypatch.setattr("sys.platform", "linux")

    def fake_run(cmd, **kwargs):
        verb = cmd[2]  # systemctl --user <verb> <unit>
        out = "enabled" if verb == "is-enabled" else "active"
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    rows = dict(main_mod._scheduler_unit_statuses())

    assert set(rows) == {"jobagent-discover.timer", "jobagent-apply.timer"}
    assert all("ACTIVE" in v for v in rows.values())
    assert not any("com.jobagent" in k for k in rows), "launchd labels must not appear on Linux"


def test_linux_inactive_but_installed_timer(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")

    def fake_run(cmd, **kwargs):
        verb = cmd[2]
        out = "enabled" if verb == "is-enabled" else "inactive"
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)
    rows = dict(main_mod._scheduler_unit_statuses())
    assert all("NOT ACTIVE" in v for v in rows.values())


def test_missing_systemctl_reports_unknown_not_not_installed(monkeypatch):
    """Absent systemctl must not masquerade as 'scheduler not installed'."""
    monkeypatch.setattr("sys.platform", "linux")

    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(main_mod.subprocess, "run", boom)

    rows = dict(main_mod._scheduler_unit_statuses())
    assert all("UNKNOWN" in v for v in rows.values()), rows
    assert not any("NOT INSTALLED" in v for v in rows.values())


# ─── Copilot review findings on PR #147 ─────────────────────────────────────

def test_windows_is_not_probed_with_systemctl(monkeypatch):
    """No scheduler units ship for Windows; don't imply systemd and report
    'systemctl unavailable', which sends an operator down the wrong path."""
    monkeypatch.setattr("sys.platform", "win32")

    def explode(*a, **k):  # must not be reached
        raise AssertionError("systemctl must not be probed on win32")

    monkeypatch.setattr(main_mod.subprocess, "run", explode)

    rows = dict(main_mod._scheduler_unit_statuses())
    assert all("UNKNOWN" in v and "platform" in v for v in rows.values()), rows


def test_launchctl_failure_does_not_assert_not_loaded(monkeypatch, tmp_path):
    """Plist present but launchctl unreadable => load state UNKNOWN, not NOT LOADED."""
    monkeypatch.setattr("sys.platform", "darwin")
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.jobagent.apply.plist").write_text("<plist/>")
    monkeypatch.setattr(main_mod.Path, "home", staticmethod(lambda: tmp_path))

    def boom(*a, **k):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(main_mod.subprocess, "run", boom)

    rows = dict(main_mod._scheduler_unit_statuses())
    assert "UNKNOWN" in rows["com.jobagent.apply"], rows
    assert "NOT LOADED" not in rows["com.jobagent.apply"]
    # a plist that genuinely is not there stays NOT INSTALLED
    assert "NOT INSTALLED" in rows["com.jobagent.discover"]


def test_masked_timer_is_not_reported_as_not_installed(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")

    def fake_run(cmd, **kwargs):
        out = "masked" if cmd[2] == "is-enabled" else "inactive"
        return subprocess.CompletedProcess(cmd, 1, stdout=out + "\n", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)
    rows = dict(main_mod._scheduler_unit_statuses())
    assert all("MASKED" in v for v in rows.values()), rows


def test_missing_unit_is_reported_not_installed(monkeypatch):
    """Verified against real systemd: is-enabled='not-found', is-active='inactive', rc=4."""
    monkeypatch.setattr("sys.platform", "linux")

    def fake_run(cmd, **kwargs):
        out = "not-found" if cmd[2] == "is-enabled" else "inactive"
        return subprocess.CompletedProcess(cmd, 4, stdout=out + "\n", stderr="")

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)
    rows = dict(main_mod._scheduler_unit_statuses())
    assert all("NOT INSTALLED" in v for v in rows.values()), rows
