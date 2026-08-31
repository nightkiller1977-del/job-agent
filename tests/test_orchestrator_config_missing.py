"""Regression (reviewer feedback on PR #80): config.json is gitignored/personal
now (copy from config.example.json), and it drives target roles, reject roles,
and compensation thresholds — the criteria discover/apply actually act on. A
missing config.json must be loud (notify_error, which reaches the dashboard
alerts + Telegram/OS notification), not just a console line in an unattended
cron log nobody is watching — otherwise a scheduled run can silently start
scoring/auto-submitting against defaults instead of the real targeting
criteria."""
from unittest.mock import patch

from src.orchestrator import Orchestrator


def test_missing_config_json_fires_notify_error():
    # Construct against the real config.json in this repo checkout so __init__
    # itself doesn't hit the missing-config path — only the explicit call below
    # exercises it, in isolation.
    orchestrator = Orchestrator(config_path="config.json")

    with patch("src.orchestrator.notify_error") as mock_notify:
        result = orchestrator._load_config("definitely-does-not-exist-config.json")

    assert result == {}
    mock_notify.assert_called_once()
    title, detail = mock_notify.call_args[0]
    assert "config.json" in title.lower()
    assert "config.example.json" in detail
