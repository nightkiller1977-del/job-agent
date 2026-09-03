import json

from src import notifier


def test_notifications_redact_phone_and_home_path(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    desktop = []

    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier.subprocess, "run", lambda *args, **kwargs: desktop.append(args))
    notifier._last_notification_times.clear()

    notifier.notify_warning(
        "usajobs session expired",
        f"iMessage sent to 301-518-7135. Missing config at {notifier.Path.home()}/Dev/Projects/job-agent/config.json",
    )

    data = json.loads(status_file.read_text())
    detail = data["alerts"][-1]["detail"]
    assert "301-518-7135" not in detail
    assert str(notifier.Path.home()) not in detail
    assert "[phone]" in detail
    assert "~/Dev/Projects/job-agent/config.json" in detail
    assert sent
    assert "301-518-7135" not in sent[0]


def test_reauth_events_redact_detail_without_corrupting_path_prefixes(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "STATUS_FILE", tmp_path / "status.json")

    notifier.record_reauth_event(
        "usajobs",
        "human",
        "waiting",
        f"call +1 (301) 518-7135 and inspect {notifier.Path.home()}/Dev/Projects/job-agent, not {notifier.Path.home()}fs/config",
    )

    data = json.loads((tmp_path / "status.json").read_text())
    detail = data["reauth_events"][-1]["detail"]
    assert "+1 (301) 518-7135" not in detail
    assert f"{notifier.Path.home()}/Dev/Projects/job-agent" not in detail
    assert "~/Dev/Projects/job-agent" in detail
    assert f"{notifier.Path.home()}fs/config" in detail
    assert "~fs/config" not in detail


def test_home_redaction_accepts_non_path_delimiters_without_matching_prefixes():
    home = str(notifier.Path.home())

    sanitized = notifier._sanitize_notification_text(
        f"missing config at {home}. Retry from {home}) or {home}, but leave {home}fs alone"
    )

    assert f"{home}." not in sanitized
    assert f"{home})" not in sanitized
    assert f"{home}," not in sanitized
    assert "~. Retry" in sanitized
    assert "~)" in sanitized
    assert "~," in sanitized
    assert f"{home}fs" in sanitized
    assert f"{home}-backup" in notifier._sanitize_notification_text(f"{home}-backup/file")
    assert f"{home}.config" in notifier._sanitize_notification_text(f"{home}.config")
    assert f"{home},backup" in notifier._sanitize_notification_text(f"{home},backup/file")
    assert home not in notifier._sanitize_notification_text(f"{home} backup/file")
    assert home not in notifier._sanitize_notification_text(f"{home} backup.files/file")
    assert home not in notifier._sanitize_notification_text(f"{home} backup=2026/file")
    assert home not in notifier._sanitize_notification_text(f"{home} backup files/file")
    assert f"{home} backup files" in notifier._sanitize_notification_text(f"PosixPath('{home} backup files/file')")
    assert f"{home} family photo archive" in notifier._sanitize_notification_text(f"PosixPath('{home} family photo archive/file')")
    assert f"{home} backup" in notifier._sanitize_notification_text(f"Path('{home} backup')")
    assert f"{home}, backup/file" in notifier._sanitize_notification_text(f"Path('{home}, backup/file')")
    escaped_path = f"PosixPath('{home} backup\\'s \"archive\"/file')"
    assert f"{home} backup\\'s \"archive\"/file" in notifier._sanitize_notification_text(escaped_path)
    assert home not in notifier._sanitize_notification_text(f"Home {home} is distinct from /tmp/config")
    assert home not in notifier._sanitize_notification_text(f"Inspect {home} then check /tmp/config")
    assert home not in notifier._sanitize_notification_text(f"Inspect {home} then set TMP=/tmp/config")
    assert home not in notifier._sanitize_notification_text(f"Home {home} failed while opening config/settings.json")
    assert home not in notifier._sanitize_notification_text(f"Working directory: {home} config/settings.json")
    assert home not in notifier._sanitize_notification_text(f"Working directory: {home} cache/index.json")
    assert home not in notifier._sanitize_notification_text(f'detail="{home} failed opening cache/index.json"')
    assert home not in notifier._sanitize_notification_text(f"Working directory: {home}\nconfig/settings.json could not be opened")
    assert "PosixPath('~')" == notifier._sanitize_notification_text(f"PosixPath('{home}')")
    assert '"~"' == notifier._sanitize_notification_text(f'"{home}"')
    assert f"{home}'backup" in notifier._sanitize_notification_text(f"{home}'backup/file")
    assert "~fs" not in sanitized


def test_warning_can_record_without_desktop_popup(tmp_path, monkeypatch):
    desktop = []
    monkeypatch.setattr(notifier, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: None)
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: desktop.append(args))
    notifier._last_notification_times.clear()

    notifier.notify_warning("usajobs session expired", "Auth refresh instructions were sent.", desktop=False)

    data = json.loads((tmp_path / "status.json").read_text())
    assert data["alerts"][-1]["title"] == "usajobs session expired"
    assert desktop == []


def test_desktop_notifications_are_rate_limited_after_sanitization(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(notifier.time, "time", lambda: 1000)
    notifier._last_notification_times.clear()

    notifier._desktop_notify("Job Agent", "Call 301-518-7135")
    notifier._desktop_notify("Job Agent", "Call +1 301 518 7135")

    assert len(calls) == 1
