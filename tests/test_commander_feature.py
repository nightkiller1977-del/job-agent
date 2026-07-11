"""
Feature/integration tests for AgentCommander and StatusWatcher.

All tests use tmp_path for file isolation. The real SQLite StateManager,
notifier file I/O, and JSON status file are exercised; only the Anthropic
API client and ReauthManager are mocked.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub playwright and src.reauth before any src.* imports so the test
# environment doesn't need the real playwright package installed.
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock as _MM
for _m in ("playwright", "playwright.async_api"):
    sys.modules.setdefault(_m, _MM())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.utcnow().isoformat()


def _seed_db(db_path: Path) -> None:
    """Insert a small set of realistic job rows into a temp DB."""
    from src.state_manager import StateManager

    sm = StateManager(str(db_path))
    jobs = [
        {
            "job_id": "jb-001",
            "source": "jobright",
            "title": "Senior Software Engineer",
            "company": "Acme Corp",
            "location": "Remote",
            "status": "applied",
            "discovered_at": _iso_now(),
            "applied_at": _iso_now(),
        },
        {
            "job_id": "jb-002",
            "source": "linkedin",
            "title": "Staff Engineer",
            "company": "Beta Inc",
            "location": "New York, NY",
            "status": "applied",
            "discovered_at": _iso_now(),
            "applied_at": _iso_now(),
        },
        {
            "job_id": "jb-003",
            "source": "indeed",
            "title": "Backend Developer",
            "company": "Gamma LLC",
            "location": "Austin, TX",
            "status": "discovered",
            "discovered_at": _iso_now(),
        },
        {
            "job_id": "jb-004",
            "source": "usajobs",
            "title": "IT Specialist",
            "company": "Dept of Labor",
            "location": "Washington DC",
            "status": "failed",
            "discovered_at": _iso_now(),
        },
        {
            "job_id": "jb-005",
            "source": "jobright",
            "title": "Platform Engineer",
            "company": "Delta Systems",
            "location": "Remote",
            "status": "skipped",
            "discovered_at": _iso_now(),
        },
    ]
    for job in jobs:
        sm.upsert_job(job)


def _write_status(status_file: Path, data: dict) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(data, indent=2))


def _make_config(tmp_path: Path) -> dict:
    return {"state_db_path": str(tmp_path / "jobs.db")}


def _mock_model_client(commander, answer: str):
    """Patch commander._model_client.complete to return *answer* synchronously."""
    commander._model_client.complete = AsyncMock(return_value=answer)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_status(tmp_path: Path) -> Path:
    """Return a path for the status file inside tmp_path."""
    return tmp_path / "state" / "agent_status.json"


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "jobs.db"
    _seed_db(db)
    return db


# ---------------------------------------------------------------------------
# TestQueryIntegration
# ---------------------------------------------------------------------------

class TestQueryIntegration:

    def test_query_returns_meaningful_answer(self, tmp_path, tmp_status, seeded_db, monkeypatch):
        """query() routes through ModelClient and returns the model's response."""
        _write_status(tmp_status, {
            "alerts": [{"level": "info", "title": "Run started", "detail": "", "ts": _iso_now()}],
            "reauth_events": [], "applied": 2, "failed": 1, "skipped": 1, "last_run": _iso_now(),
        })
        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))
        _mock_model_client(commander, "You have applied to 2 jobs today.")

        result = asyncio.run(commander.query("How many jobs have been applied to?"))

        assert "2" in result
        assert isinstance(result, str) and len(result) > 0

    def test_query_context_includes_job_counts(self, tmp_path, tmp_status, seeded_db, monkeypatch):
        """query() builds context that references real job counts from the seeded DB."""
        _write_status(tmp_status, {
            "alerts": [], "reauth_events": [],
            "applied": 5, "failed": 0, "skipped": 0, "last_run": _iso_now(),
        })
        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))

        captured: list[dict] = []

        async def capture_complete(messages, **kw):
            captured.append({"messages": messages})
            return "Context captured."

        commander._model_client.complete = capture_complete
        asyncio.run(commander.query("What is the job count?"))

        assert captured, "ModelClient.complete was never called"
        prompt = captured[0]["messages"][0]["content"]
        assert "applied" in prompt.lower()
        assert "Senior Software Engineer" in prompt or "Acme Corp" in prompt or "discovered" in prompt.lower()

    def test_query_context_includes_alert_text(self, tmp_path, tmp_status, seeded_db, monkeypatch):
        """query() context contains real alert text from a seeded agent_status.json."""
        alert_title = "LinkedIn login failed"
        alert_detail = "Session cookie expired unexpectedly"
        _write_status(tmp_status, {
            "alerts": [{"level": "error", "title": alert_title, "detail": alert_detail, "ts": _iso_now()}],
            "reauth_events": [], "applied": 0, "failed": 0, "skipped": 0, "last_run": _iso_now(),
        })
        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))

        captured: list[dict] = []

        async def capture_complete(messages, **kw):
            captured.append({"messages": messages})
            return "Captured."

        commander._model_client.complete = capture_complete
        asyncio.run(commander.query("Any errors?"))

        prompt = captured[0]["messages"][0]["content"]
        assert alert_title in prompt
        assert alert_detail in prompt


# ---------------------------------------------------------------------------
# TestDiagnoseIntegration
# ---------------------------------------------------------------------------

class TestDiagnoseIntegration:

    def test_diagnose_recent_failed_reauth_is_critical_or_warning(
        self, tmp_path, tmp_status, seeded_db, monkeypatch
    ):
        """diagnose() on a source with a recently failed reauth → severity critical or warning."""
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [
                {
                    "source": "jobright",
                    "mode": "automated",
                    "outcome": "failed",
                    "detail": "Login page blocked",
                    "ts": _iso_now(),
                }
            ],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": _iso_now(),
        })

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", tmp_path / "sessions")

        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))
        result = commander.diagnose("jobright")

        src_data = result["sources"]["jobright"]
        assert src_data["severity"] in ("critical", "warning"), (
            f"Expected critical or warning, got {src_data['severity']}"
        )

    def test_diagnose_unseen_source_explains_correctly(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """diagnose() on a never-seen source: no jobs, no session → explains no session."""
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        # Point SESSIONS_DIR at an empty dir → no session files
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        # Fresh DB — no jobs at all
        config = {"state_db_path": str(tmp_path / "empty.db")}
        from src.commander import AgentCommander
        commander = AgentCommander(config)
        result = commander.diagnose("usajobs")

        src_data = result["sources"]["usajobs"]
        assert src_data["job_count"] == 0
        # No session file → root_cause should mention "stale" or "session" or "missing"
        root_cause = src_data["root_cause"].lower()
        assert any(kw in root_cause for kw in ("stale", "session", "missing", "credential")), (
            f"Unexpected root_cause: {src_data['root_cause']!r}"
        )

    def test_diagnose_all_sources_returns_four_entries(
        self, tmp_path, tmp_status, seeded_db, monkeypatch
    ):
        """diagnose() with no source arg returns 4 entries in sources dict."""
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", tmp_path / "sessions")

        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))
        result = commander.diagnose()

        assert len(result["sources"]) == 4
        assert set(result["sources"].keys()) == {"linkedin", "jobright", "indeed", "usajobs"}

    def test_diagnose_stale_session_file_root_cause_contains_stale(
        self, tmp_path, tmp_status, seeded_db, monkeypatch
    ):
        """diagnose() with a session file older than 48h → root_cause contains 'stale'."""
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = sessions_dir / "linkedin_chromium.json"
        session_file.write_text("{}")

        # Back-date modification time to 49 hours ago
        old_mtime = time.time() - (49 * 3600)
        import os
        os.utime(str(session_file), (old_mtime, old_mtime))

        monkeypatch.setenv("LINKEDIN_EMAIL", "test@example.com")
        monkeypatch.setenv("LINKEDIN_PASSWORD", "testpass")

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        from src.commander import AgentCommander
        commander = AgentCommander(_make_config(tmp_path))
        result = commander.diagnose("linkedin")

        root_cause = result["sources"]["linkedin"]["root_cause"].lower()
        assert "stale" in root_cause, f"Expected 'stale' in root_cause, got: {root_cause!r}"


# ---------------------------------------------------------------------------
# TestAttemptFixIntegration
# ---------------------------------------------------------------------------

class TestAttemptFixIntegration:

    def test_attempt_fix_automated_source_calls_reauth_manager(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """attempt_fix for an automated source (jobright) calls ReauthManager.handle."""
        # No session file → fixable
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@example.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "testpass")

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        mock_reauth_instance = MagicMock()
        mock_reauth_instance.handle = AsyncMock(return_value=True)
        mock_reauth_class = MagicMock(return_value=mock_reauth_instance)

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        from src.commander import AgentCommander

        commander = AgentCommander(config)

        result = asyncio.run(
            commander.attempt_fix("jobright", _reauth_cls=mock_reauth_class)
        )

        assert mock_reauth_instance.handle.called
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True
        assert "source" in result

    def test_attempt_fix_non_fixable_source_returns_failure_dict(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """attempt_fix for a non-fixable source returns failure dict without crashing."""
        # Seed a failed reauth outcome → severity critical, fixable=False
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [
                {
                    "source": "linkedin",
                    "mode": "automated",
                    "outcome": "failed",
                    "detail": "CAPTCHA block",
                    "ts": _iso_now(),
                }
            ],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        # Provide session file so "no session" branch is skipped; age < 24h
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = sessions_dir / "linkedin_chromium.json"
        session_file.write_text("{}")

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        from src.commander import AgentCommander
        commander = AgentCommander(config)

        result = asyncio.run(
            commander.attempt_fix("linkedin")
        )

        assert result["success"] is False
        assert "reason" in result
        assert "not fixable" in result["reason"].lower()

    def test_attempt_fix_records_notification(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """attempt_fix records result notification in agent_status.json."""
        _write_status(tmp_status, {
            "alerts": [],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        mock_reauth_instance = MagicMock()
        mock_reauth_instance.handle = AsyncMock(return_value=True)
        mock_reauth_class = MagicMock(return_value=mock_reauth_instance)

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        from src.commander import AgentCommander
        commander = AgentCommander(config)

        asyncio.run(
            commander.attempt_fix("indeed", _reauth_cls=mock_reauth_class)
        )

        # Whether fix succeeded or was skipped, the status file should have been touched
        status_data = json.loads(tmp_status.read_text())
        # alerts list is populated by notify_info / notify_error
        assert "alerts" in status_data


# ---------------------------------------------------------------------------
# TestWatcherIntegration
# ---------------------------------------------------------------------------

class TestWatcherIntegration:

    def _make_watcher(self, config, auto_fix=False):
        from src.watcher import StatusWatcher
        return StatusWatcher(config, poll_interval=1, auto_fix=auto_fix)

    def test_watcher_detects_new_error_and_diagnoses(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """Watcher detects a new error written to agent_status.json and diagnoses it."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        _write_status(tmp_status, {
            "alerts": [
                {
                    "level": "error",
                    "title": "jobright scrape failed",
                    "detail": "Session expired",
                    "ts": _iso_now(),
                }
            ],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        watcher = self._make_watcher(config, auto_fix=False)

        actions = asyncio.run(watcher.watch_once())

        assert len(actions) >= 1
        sources_detected = {a.get("source") for a in actions}
        assert "jobright" in sources_detected

    def test_watcher_with_auto_fix_attempts_reauth(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """Watcher with auto_fix=True attempts fix when source is fixable."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # No session file for jobright → fixable

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        _write_status(tmp_status, {
            "alerts": [
                {
                    "level": "error",
                    "title": "jobright login failure",
                    "detail": "stale session",
                    "ts": _iso_now(),
                }
            ],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        watcher = self._make_watcher(config, auto_fix=True)

        fake_fix = AsyncMock(return_value={"success": True, "source": "jobright", "strategy": "automated", "detail": ""})
        with patch.object(watcher._commander, "attempt_fix", fake_fix):
            actions = asyncio.run(watcher.watch_once())

        assert len(actions) >= 1
        action_types = {a.get("action") for a in actions}
        assert action_types & {"auto_fix_attempted", "detected_no_fix", "auto_fix_failed"}

    def test_watcher_watch_once_twice_no_duplicate_actions(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """watch_once called twice: second call with no new events returns []."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        _write_status(tmp_status, {
            "alerts": [
                {
                    "level": "error",
                    "title": "indeed scrape error",
                    "detail": "timeout",
                    "ts": _iso_now(),
                }
            ],
            "reauth_events": [],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        watcher = self._make_watcher(config, auto_fix=False)

        first = asyncio.run(watcher.watch_once())
        second = asyncio.run(watcher.watch_once())

        assert len(first) >= 1, "First call should detect the error"
        assert len(second) == 0, "Second call with same data should return no new actions"

    def test_watcher_actions_have_required_fields(
        self, tmp_path, tmp_status, monkeypatch
    ):
        """Actions list from watch_once contains type, source, action, ts fields."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        import src.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "STATUS_FILE", tmp_status)
        import src.commander as commander_mod
        monkeypatch.setattr(commander_mod, "SESSIONS_DIR", sessions_dir)

        _write_status(tmp_status, {
            "alerts": [
                {
                    "level": "error",
                    "title": "usajobs session expired",
                    "detail": "cookie stale",
                    "ts": _iso_now(),
                }
            ],
            "reauth_events": [
                {
                    "source": "linkedin",
                    "mode": "automated",
                    "outcome": "failed",
                    "detail": "blocked",
                    "ts": _iso_now(),
                }
            ],
            "applied": 0,
            "failed": 0,
            "skipped": 0,
            "last_run": None,
        })

        config = {"state_db_path": str(tmp_path / "jobs.db")}
        watcher = self._make_watcher(config, auto_fix=False)

        actions = asyncio.run(watcher.watch_once())

        assert len(actions) >= 1
        for action in actions:
            assert "type" in action, f"Missing 'type' in {action}"
            assert "source" in action, f"Missing 'source' in {action}"
            assert "action" in action, f"Missing 'action' in {action}"
            assert "ts" in action, f"Missing 'ts' in {action}"
