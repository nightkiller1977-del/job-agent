import json
import os
import subprocess
import sys
from pathlib import Path

import src.notifier as notifier
import src.session_watchdog as sw


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_warning_dedupe_survives_fresh_python_process(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    notifier._last_notification_times.clear()

    notifier.notify_warning(
        "Apply run: nothing submitted",
        "first process",
        dedupe_key="apply_nothing_submitted",
        dedupe_seconds=21600,
        desktop=False,
    )
    assert len(sent) == 1

    code = "\n".join([
        "import os",
        "from pathlib import Path",
        "import src.notifier as n",
        "n.STATUS_FILE = Path(os.environ['JOBAGENT_STATUS_FILE'])",
        "n._send_telegram = lambda message: print('SENT')",
        "n._desktop_notify = lambda *args, **kwargs: None",
        "n.notify_warning('Apply run: nothing submitted', 'second process', dedupe_key='apply_nothing_submitted', dedupe_seconds=21600, desktop=False)",
    ])
    child = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JOBAGENT_STATUS_FILE": str(status_file),
            "PYTHONPATH": str(REPO_ROOT),
        },
        check=False,
        timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert "SENT" not in child.stdout


def test_deep_link_dedupe_survives_memory_reset(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sw, "_stage_prepare_sessions", lambda source: True)
    monkeypatch.setattr(sw, "_novnc_link", lambda: None)
    notifier._last_notification_times.clear()

    sw._send_deep_link_notification("linkedin", "fix session")
    notifier._last_notification_times.clear()
    sw._send_deep_link_notification("linkedin", "fix session again")

    assert len(sent) == 1


def test_deep_link_staging_failure_does_not_send_or_consume_dedupe(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    sent = []
    monkeypatch.setattr(notifier, "STATUS_FILE", status_file)
    monkeypatch.setattr(notifier, "_send_telegram", lambda message: sent.append(message))
    monkeypatch.setattr(notifier, "_desktop_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sw, "_stage_prepare_sessions", lambda source: False)
    monkeypatch.setattr(sw, "_novnc_link", lambda: None)
    notifier._last_notification_times.clear()

    sw._send_deep_link_notification("linkedin", "fix session")

    data = json.loads(status_file.read_text()) if status_file.exists() else {}
    assert sent == []
    assert "notification_dedupe" not in data
