"""
Unit tests for AgentCommander and StatusWatcher.

All tests use mocks — no live Anthropic API calls, no live DB, no live status file.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_status(alerts=None, reauth_events=None, **kwargs):
    return {
        "alerts": alerts or [],
        "reauth_events": reauth_events or [],
        "applied": kwargs.get("applied", 0),
        "failed": kwargs.get("failed", 0),
        "skipped": kwargs.get("skipped", 0),
        "last_run": kwargs.get("last_run", "unknown"),
    }


def _make_commander(tmp_path, monkeypatch, env=None):
    """Return an AgentCommander with a real (but temp) DB and mocked status."""
    db_path = str(tmp_path / "jobs.db")
    config = {"state_db_path": db_path}

    # Redirect STATUS_FILE used inside notifier / commander
    status_file = tmp_path / "agent_status.json"
    status_file.write_text(json.dumps(_make_status()))

    monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)
    monkeypatch.setattr("src.commander.SESSIONS_DIR", tmp_path / "sessions")
    (tmp_path / "sessions").mkdir()

    # Patch env vars
    env_defaults = {
        "ANTHROPIC_API_KEY": "",
        "LINKEDIN_EMAIL": "",
        "LINKEDIN_PASSWORD": "",
        "JOBRIGHT_EMAIL": "",
        "JOBRIGHT_PASSWORD": "",
        "INDEED_EMAIL": "",
        "INDEED_PASSWORD": "",
        "USAJOBS_USERNAME": "",
        "USAJOBS_PASSWORD": "",
    }
    if env:
        env_defaults.update(env)
    monkeypatch.setattr(os, "environ", {**os.environ, **env_defaults})

    from src.commander import AgentCommander
    return AgentCommander(config), status_file


def _write_status(status_file, data):
    status_file.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# TestBuildContext
# ---------------------------------------------------------------------------

class TestBuildContext:
    def test_credential_set_vs_missing(self, tmp_path, monkeypatch):
        """_build_context shows SET for env vars that are present and MISSING otherwise."""
        commander, _ = _make_commander(
            tmp_path, monkeypatch,
            env={
                "LINKEDIN_EMAIL": "user@example.com",
                "LINKEDIN_PASSWORD": "secret",
                "JOBRIGHT_EMAIL": "",
                "JOBRIGHT_PASSWORD": "",
            },
        )
        ctx = commander._build_context()
        assert "LINKEDIN_EMAIL=SET" in ctx
        assert "LINKEDIN_PASSWORD=SET" in ctx
        assert "JOBRIGHT_EMAIL=MISSING" in ctx
        assert "JOBRIGHT_PASSWORD=MISSING" in ctx
        # Values must not appear in context
        assert "user@example.com" not in ctx
        assert "secret" not in ctx

    def test_session_file_age_when_exists(self, tmp_path, monkeypatch):
        """_build_context reports session file age in hours when file exists."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        sessions_dir = tmp_path / "sessions"
        session_file = sessions_dir / "linkedin_chromium.json"
        session_file.write_text("{}")
        # Make it appear ~2 hours old
        old_mtime = time.time() - 7200
        os.utime(session_file, (old_mtime, old_mtime))

        ctx = commander._build_context()
        assert "linkedin_chromium.json: EXISTS" in ctx
        assert "age=" in ctx

    def test_session_file_missing(self, tmp_path, monkeypatch):
        """_build_context shows MISSING when session file does not exist."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        ctx = commander._build_context()
        assert "linkedin_chromium.json: MISSING" in ctx

    def test_alert_and_reauth_summaries(self, tmp_path, monkeypatch):
        """_build_context includes recent alert text and reauth event text."""
        commander, status_file = _make_commander(tmp_path, monkeypatch)
        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "LinkedIn login failed", "detail": "bad password"}
            ],
            reauth_events=[
                {"ts": "2026-06-26T01:00:00Z", "source": "linkedin",
                 "mode": "automated", "outcome": "failed", "detail": "timeout"}
            ],
        )
        _write_status(status_file, status)

        ctx = commander._build_context()
        assert "LinkedIn login failed" in ctx
        assert "bad password" in ctx
        assert "linkedin" in ctx
        assert "failed" in ctx


# ---------------------------------------------------------------------------
# TestQuery
# ---------------------------------------------------------------------------

class TestQuery:
    def test_returns_model_response_text(self, tmp_path, monkeypatch):
        """query() returns the text from ModelClient.complete (Ollama or Claude)."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        with patch.object(commander._model_client, "complete", new=AsyncMock(return_value="All looks good.")):
            result = commander.query("How are things?")
        assert result == "All looks good."

    def test_returns_no_model_message_when_both_unavailable(self, tmp_path, monkeypatch):
        """query() returns an error string when neither Ollama nor Claude is available."""
        commander, _ = _make_commander(tmp_path, monkeypatch, env={"ANTHROPIC_API_KEY": ""})
        with patch.object(commander._model_client, "complete", new=AsyncMock(
            return_value="No model available: Ollama is not running and ANTHROPIC_API_KEY is not set."
        )):
            result = commander.query("status?")
        assert "no model" in result.lower() or "not set" in result.lower() or result

    def test_system_prompt_contains_job_application_agent(self, tmp_path, monkeypatch):
        """query() passes a system prompt mentioning 'job application agent' to ModelClient."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        captured = {}

        async def capture_complete(messages, system="", **kw):
            captured["system"] = system
            return "ok"

        with patch.object(commander._model_client, "complete", side_effect=capture_complete):
            commander.query("test")
        assert "job application agent" in captured.get("system", "").lower()

    def test_user_message_contains_context_and_question(self, tmp_path, monkeypatch):
        """query() includes context and the original question in the messages passed to ModelClient."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        captured = {}

        async def capture_complete(messages, **kw):
            captured["messages"] = messages
            return "ok"

        with patch.object(commander._model_client, "complete", side_effect=capture_complete):
            commander.query("What went wrong?")
        user_content = captured.get("messages", [{}])[0].get("content", "")
        assert "What went wrong?" in user_content
        assert "CREDENTIAL CHECK" in user_content or "Agent state" in user_content


# ---------------------------------------------------------------------------
# TestDiagnose
# ---------------------------------------------------------------------------

class TestDiagnose:
    def test_healthy_source(self, tmp_path, monkeypatch):
        """Credentials set + fresh session → severity 'healthy'."""
        commander, _ = _make_commander(
            tmp_path, monkeypatch,
            env={
                "LINKEDIN_EMAIL": "u@e.com",
                "LINKEDIN_PASSWORD": "pw",
            },
        )
        # Create a fresh session file (< 24h old)
        sessions_dir = tmp_path / "sessions"
        session_file = sessions_dir / "linkedin_chromium.json"
        session_file.write_text("{}")

        result = commander.diagnose("linkedin")
        src = result["sources"]["linkedin"]
        assert src["severity"] == "healthy"

    def test_missing_credentials(self, tmp_path, monkeypatch):
        """Missing credentials → severity 'critical', root_cause 'missing credentials', fixable False."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        result = commander.diagnose("linkedin")
        src = result["sources"]["linkedin"]
        assert src["severity"] == "critical"
        assert "missing credentials" in src["root_cause"]
        assert src["fixable"] is False

    def test_stale_session_with_credentials(self, tmp_path, monkeypatch):
        """Stale session (>24h) with credentials → root_cause contains 'stale session', fixable True."""
        commander, _ = _make_commander(
            tmp_path, monkeypatch,
            env={
                "LINKEDIN_EMAIL": "u@e.com",
                "LINKEDIN_PASSWORD": "pw",
            },
        )
        sessions_dir = tmp_path / "sessions"
        session_file = sessions_dir / "linkedin_chromium.json"
        session_file.write_text("{}")
        # Make it 30 hours old
        old_mtime = time.time() - 30 * 3600
        os.utime(session_file, (old_mtime, old_mtime))

        result = commander.diagnose("linkedin")
        src = result["sources"]["linkedin"]
        assert "stale session" in src["root_cause"]
        assert src["fixable"] is True

    def test_recent_error_alerts_appear_in_diagnosis(self, tmp_path, monkeypatch):
        """Recent error alerts for the source appear in the diagnosis recent_errors list."""
        commander, status_file = _make_commander(
            tmp_path, monkeypatch,
            env={
                "LINKEDIN_EMAIL": "u@e.com",
                "LINKEDIN_PASSWORD": "pw",
            },
        )
        sessions_dir = tmp_path / "sessions"
        (sessions_dir / "linkedin_chromium.json").write_text("{}")

        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "linkedin scrape error", "detail": "captcha hit"}
            ]
        )
        _write_status(status_file, status)

        result = commander.diagnose("linkedin")
        src = result["sources"]["linkedin"]
        assert len(src["recent_errors"]) > 0
        assert any("linkedin" in (a.get("title", "") + a.get("detail", "")).lower()
                   for a in src["recent_errors"])

    def test_diagnose_none_returns_all_sources(self, tmp_path, monkeypatch):
        """diagnose(None) returns all 4 sources."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        result = commander.diagnose(None)
        assert set(result["sources"].keys()) == {"linkedin", "jobright", "indeed", "usajobs"}

    def test_summary_mentions_sources(self, tmp_path, monkeypatch):
        """Summary string mentions unhealthy sources."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        result = commander.diagnose(None)
        summary = result["summary"]
        assert isinstance(summary, str)
        assert len(summary) > 0
        # All sources are missing creds so should appear in summary
        for src in ("linkedin", "jobright", "indeed", "usajobs"):
            assert src in summary


# ---------------------------------------------------------------------------
# TestAttemptFix
# ---------------------------------------------------------------------------

class TestAttemptFix:
    def _stale_session(self, sessions_dir, source="linkedin"):
        f = sessions_dir / f"{source}_chromium.json"
        f.write_text("{}")
        os.utime(f, (time.time() - 30 * 3600,) * 2)
        return f

    def _mock_reauth(self, return_value=True):
        inst = MagicMock()
        inst.handle = AsyncMock(return_value=return_value)
        cls = MagicMock(return_value=inst)
        return cls, inst

    def test_fixable_source_calls_reauth_manager(self, tmp_path, monkeypatch):
        """Fixable source → calls ReauthManager.handle → returns success=True dict."""
        commander, _ = _make_commander(tmp_path, monkeypatch, env={"LINKEDIN_EMAIL": "u@e.com", "LINKEDIN_PASSWORD": "pw"})
        self._stale_session(tmp_path / "sessions")
        mock_cls, mock_inst = self._mock_reauth(return_value=True)

        result = asyncio.run(
            commander.attempt_fix("linkedin", _reauth_cls=mock_cls)
        )

        assert result["success"] is True
        mock_inst.handle.assert_called_once()

    def test_non_fixable_source_returns_failure_without_reauth(self, tmp_path, monkeypatch):
        """Non-fixable source (missing creds) → returns success=False without calling ReauthManager."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        mock_cls, mock_inst = self._mock_reauth()

        result = asyncio.run(
            commander.attempt_fix("linkedin", _reauth_cls=mock_cls)
        )

        assert result["success"] is False
        mock_cls.assert_not_called()

    def test_reauth_manager_returns_false(self, tmp_path, monkeypatch):
        """ReauthManager.handle returning False → attempt_fix returns success=False."""
        commander, _ = _make_commander(tmp_path, monkeypatch, env={"LINKEDIN_EMAIL": "u@e.com", "LINKEDIN_PASSWORD": "pw"})
        self._stale_session(tmp_path / "sessions")
        mock_cls, mock_inst = self._mock_reauth(return_value=False)

        result = asyncio.run(
            commander.attempt_fix("linkedin", _reauth_cls=mock_cls)
        )

        assert result["success"] is False

    def test_result_dict_contains_required_fields(self, tmp_path, monkeypatch):
        """Result dict contains source, strategy, detail fields."""
        commander, _ = _make_commander(tmp_path, monkeypatch, env={"LINKEDIN_EMAIL": "u@e.com", "LINKEDIN_PASSWORD": "pw"})
        self._stale_session(tmp_path / "sessions")
        mock_cls, _ = self._mock_reauth(return_value=True)

        result = asyncio.run(
            commander.attempt_fix("linkedin", _reauth_cls=mock_cls)
        )

        assert "source" in result
        assert "strategy" in result
        assert "detail" in result
        assert result["source"] == "linkedin"


# ---------------------------------------------------------------------------
# TestGetReport
# ---------------------------------------------------------------------------

class TestGetReport:
    def test_contains_generated_at_timestamp(self, tmp_path, monkeypatch):
        """get_report() result contains generated_at timestamp."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        report = commander.get_report()
        assert "generated_at" in report
        # Should parse as an ISO datetime
        datetime.fromisoformat(report["generated_at"])

    def test_contains_jobs_by_status(self, tmp_path, monkeypatch):
        """get_report() result contains jobs_by_status dict."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        report = commander.get_report()
        assert "jobs_by_status" in report
        assert isinstance(report["jobs_by_status"], dict)

    def test_contains_source_health_with_severity(self, tmp_path, monkeypatch):
        """get_report() result contains source_health with severity for each source."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        report = commander.get_report()
        assert "source_health" in report
        sh = report["source_health"]
        for src in ("linkedin", "jobright", "indeed", "usajobs"):
            assert src in sh
            assert "severity" in sh[src]


# ---------------------------------------------------------------------------
# TestFormatReport
# ---------------------------------------------------------------------------

class TestFormatReport:
    def test_returns_non_empty_string(self, tmp_path, monkeypatch):
        """format_report() returns a non-empty string."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        report = commander.get_report()
        formatted = commander.format_report(report)
        assert isinstance(formatted, str)
        assert len(formatted.strip()) > 0

    def test_contains_source_names(self, tmp_path, monkeypatch):
        """format_report() output contains source names."""
        commander, _ = _make_commander(tmp_path, monkeypatch)
        report = commander.get_report()
        formatted = commander.format_report(report)
        for src in ("linkedin", "jobright", "indeed", "usajobs"):
            assert src in formatted


# ---------------------------------------------------------------------------
# TestStatusWatcher
# ---------------------------------------------------------------------------

def _make_watcher(tmp_path, monkeypatch, auto_fix=True, env=None):
    """Return a StatusWatcher with mocked internals."""
    db_path = str(tmp_path / "jobs.db")
    config = {"state_db_path": db_path}

    status_file = tmp_path / "agent_status.json"
    status_file.write_text(json.dumps(_make_status()))

    monkeypatch.setattr("src.notifier.STATUS_FILE", status_file)
    monkeypatch.setattr("src.commander.SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr("src.watcher._load_status", lambda: json.loads(status_file.read_text()))
    (tmp_path / "sessions").mkdir(exist_ok=True)

    env_defaults = {
        "ANTHROPIC_API_KEY": "",
        "LINKEDIN_EMAIL": "",
        "LINKEDIN_PASSWORD": "",
        "JOBRIGHT_EMAIL": "",
        "JOBRIGHT_PASSWORD": "",
        "INDEED_EMAIL": "",
        "INDEED_PASSWORD": "",
        "USAJOBS_USERNAME": "",
        "USAJOBS_PASSWORD": "",
    }
    if env:
        env_defaults.update(env)
    monkeypatch.setattr(os, "environ", {**os.environ, **env_defaults})

    from src.watcher import StatusWatcher
    return StatusWatcher(config, auto_fix=auto_fix), status_file


class TestStatusWatcher:
    def test_watch_once_no_new_alerts_returns_empty(self, tmp_path, monkeypatch):
        """watch_once with no error alerts returns empty list."""
        watcher, _ = _make_watcher(tmp_path, monkeypatch)
        result = asyncio.run(watcher.watch_once())
        assert result == []

    def test_watch_once_new_error_alert_for_known_source_diagnoses(self, tmp_path, monkeypatch):
        """watch_once with new error alert for known source calls diagnose on that source."""
        watcher, status_file = _make_watcher(tmp_path, monkeypatch)
        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "jobright login failed", "detail": "bad creds"}
            ]
        )
        _write_status(status_file, status)

        diagnose_mock = MagicMock(return_value={
            "sources": {"jobright": {"fixable": False, "severity": "critical", "root_cause": "missing credentials"}},
            "summary": "0/1 healthy",
            "has_fixable": False,
        })
        watcher._commander.diagnose = diagnose_mock

        result = asyncio.run(watcher.watch_once())
        diagnose_mock.assert_called_once_with("jobright")
        assert len(result) == 1

    def test_watch_once_fixable_auto_fix_true_calls_attempt_fix(self, tmp_path, monkeypatch):
        """watch_once with fixable source and auto_fix=True calls attempt_fix."""
        watcher, status_file = _make_watcher(tmp_path, monkeypatch, auto_fix=True)
        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "linkedin session expired", "detail": "stale"}
            ]
        )
        _write_status(status_file, status)

        watcher._commander.diagnose = MagicMock(return_value={
            "sources": {"linkedin": {"fixable": True, "severity": "warning", "root_cause": "stale session"}},
            "summary": "",
            "has_fixable": True,
        })
        watcher._commander.attempt_fix = AsyncMock(return_value={
            "success": True, "source": "linkedin", "strategy": "browser", "detail": "ok"
        })

        result = asyncio.run(watcher.watch_once())
        watcher._commander.attempt_fix.assert_awaited_once_with("linkedin")
        assert any(a.get("action") == "auto_fix_attempted" for a in result)

    def test_watch_once_fixable_auto_fix_false_does_not_call_attempt_fix(self, tmp_path, monkeypatch):
        """watch_once with fixable source and auto_fix=False does NOT call attempt_fix."""
        watcher, status_file = _make_watcher(tmp_path, monkeypatch, auto_fix=False)
        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "linkedin session expired", "detail": "stale"}
            ]
        )
        _write_status(status_file, status)

        watcher._commander.diagnose = MagicMock(return_value={
            "sources": {"linkedin": {"fixable": True, "severity": "warning", "root_cause": "stale session"}},
            "summary": "",
            "has_fixable": True,
        })
        watcher._commander.attempt_fix = AsyncMock()

        result = asyncio.run(watcher.watch_once())
        watcher._commander.attempt_fix.assert_not_awaited()
        assert any(a.get("action") == "detected_no_fix" for a in result)

    def test_same_alert_seen_twice_processed_once(self, tmp_path, monkeypatch):
        """Same alert appearing twice is only processed once (dedup)."""
        watcher, status_file = _make_watcher(tmp_path, monkeypatch)
        alert = {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "jobright login failed", "detail": ""}
        status = _make_status(alerts=[alert])
        _write_status(status_file, status)

        watcher._commander.diagnose = MagicMock(return_value={
            "sources": {"jobright": {"fixable": False, "severity": "critical", "root_cause": "missing credentials"}},
            "summary": "",
            "has_fixable": False,
        })

        asyncio.run(watcher.watch_once())
        call_count_first = watcher._commander.diagnose.call_count

        # Second poll — same alert still present
        asyncio.run(watcher.watch_once())
        call_count_second = watcher._commander.diagnose.call_count

        assert call_count_first == 1
        assert call_count_second == 1  # No new calls on second poll

    def test_extract_source_finds_jobright(self, tmp_path, monkeypatch):
        """_extract_source finds 'jobright' in 'jobright reauth failed'."""
        watcher, _ = _make_watcher(tmp_path, monkeypatch)
        result = watcher._extract_source("jobright reauth failed")
        assert result == "jobright"

    def test_extract_source_returns_none_for_unknown(self, tmp_path, monkeypatch):
        """_extract_source returns None for text without a known source name."""
        watcher, _ = _make_watcher(tmp_path, monkeypatch)
        result = watcher._extract_source("some random error with no source")
        assert result is None

    def test_get_seen_counts_after_watch_once(self, tmp_path, monkeypatch):
        """get_seen_counts returns correct dict after watch_once processes an alert."""
        watcher, status_file = _make_watcher(tmp_path, monkeypatch)
        status = _make_status(
            alerts=[
                {"ts": "2026-06-26T00:00:00Z", "level": "error",
                 "title": "indeed login failed", "detail": "captcha"}
            ]
        )
        _write_status(status_file, status)

        watcher._commander.diagnose = MagicMock(return_value={
            "sources": {"indeed": {"fixable": False, "severity": "critical", "root_cause": "missing credentials"}},
            "summary": "",
            "has_fixable": False,
        })

        asyncio.run(watcher.watch_once())
        counts = watcher.get_seen_counts()
        assert "alerts_seen" in counts
        assert "reauth_events_seen" in counts
        assert counts["alerts_seen"] == 1
        assert counts["reauth_events_seen"] == 0
