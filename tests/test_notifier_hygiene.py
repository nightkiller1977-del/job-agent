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


def test_desktop_notifications_are_rate_limited_after_sanitization(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(notifier.time, "time", lambda: 1000)
    notifier._last_notification_times.clear()

    notifier._desktop_notify("Job Agent", "Call 301-518-7135")
    notifier._desktop_notify("Job Agent", "Call +1 301 518 7135")

    assert len(calls) == 1
