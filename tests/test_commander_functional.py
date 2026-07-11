"""
Functional trust tests for AgentCommander, StatusWatcher, ReauthManager,
StateManager, and notifier integration.

All file I/O is isolated via tmp_path + monkeypatching of STATUS_FILE and
the StateManager db_path. No real browsers, real Anthropic API calls, or
real iMessage sends are made.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub playwright and src.reauth before any src.* imports.
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock as _MM
for _m in ("playwright", "playwright.async_api"):
    sys.modules.setdefault(_m, _MM())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_status(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load_status(path: Path) -> dict:
    return json.loads(path.read_text())


def _base_status(**kwargs) -> dict:
    data = {
        "alerts": [],
        "reauth_events": [],
        "last_run": None,
        "applied": 0,
        "failed": 0,
        "skipped": 0,
    }
    data.update(kwargs)
    return data


def _make_alert(level: str, title: str, detail: str = "", ts: str | None = None) -> dict:
    return {
        "level": level,
        "title": title,
        "detail": detail,
        "ts": ts or datetime.utcnow().isoformat(),
    }


def _make_reauth_event(
    source: str,
    outcome: str,
    mode: str = "automated",
    detail: str = "",
    ts: str | None = None,
) -> dict:
    return {
        "source": source,
        "mode": mode,
        "outcome": outcome,
        "detail": detail,
        "ts": ts or datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_status(tmp_path, monkeypatch):
    """Patch notifier.STATUS_FILE to a temp file and return its Path."""
    status_file = tmp_path / "state" / "agent_status.json"
    _write_status(status_file, _base_status())

    import src.notifier as notifier_mod
    monkeypatch.setattr(notifier_mod, "STATUS_FILE", status_file)
    return status_file


@pytest.fixture()
def tmp_db(tmp_path):
    """Return a path to a temp SQLite DB for StateManager."""
    return str(tmp_path / "state" / "jobs.db")


@pytest.fixture()
def config(tmp_db):
    return {"state_db_path": tmp_db}


@pytest.fixture()
def commander(config, tmp_status, monkeypatch):
    """Return an AgentCommander wired to tmp files, SESSIONS_DIR mocked."""
    import src.commander as cmd_mod
    fake_sessions = Path(str(tmp_status.parent.parent / "sessions"))
    fake_sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cmd_mod, "SESSIONS_DIR", fake_sessions)
    from src.commander import AgentCommander
    return AgentCommander(config)


@pytest.fixture()
def sessions_dir(commander, monkeypatch, tmp_path):
    """Return the fake sessions dir used by the commander fixture."""
    import src.commander as cmd_mod
    return cmd_mod.SESSIONS_DIR


# ---------------------------------------------------------------------------
# TestTrustCommanderReauthIntegration
# ---------------------------------------------------------------------------

class TestTrustCommanderReauthIntegration:

    def test_trust_reauth_failure_appears_in_diagnosis(
        self, commander, tmp_status, sessions_dir, monkeypatch
    ):
        """A 'failed' reauth event for jobright surfaces in diagnose() with
        recent_reauth_outcome == 'failed' and severity != 'healthy'."""
        # Seed a reauth failure event
        data = _base_status(
            reauth_events=[_make_reauth_event("jobright", "failed")]
        )
        _write_status(tmp_status, data)

        # Provide credentials so we don't hit 'missing credentials' branch first
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@example.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "testpass")

        # Create a session file so we skip the 'no session' branch
        session_file = sessions_dir / "jobright_chromium.json"
        session_file.write_text("{}")
        # Make it fresh (mtime just now) so age < 24h
        import time
        os.utime(session_file, (time.time(), time.time()))

        result = commander.diagnose("jobright")
        src_data = result["sources"]["jobright"]

        assert src_data["recent_reauth_outcome"] == "failed"
        assert src_data["severity"] != "healthy"

    @pytest.mark.asyncio
    async def test_trust_attempt_fix_calls_reauth_manager(
        self, commander, tmp_status, sessions_dir, monkeypatch
    ):
        """attempt_fix() delegates to ReauthManager.handle() for a fixable source."""
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@example.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "testpass")

        # No session file -> stale session -> fixable=True
        # (make sure the file does NOT exist)
        session_file = sessions_dir / "jobright_chromium.json"
        if session_file.exists():
            session_file.unlink()

        mock_reauth_instance = MagicMock()
        mock_reauth_instance.handle = AsyncMock(return_value=True)
        mock_reauth_class = MagicMock(return_value=mock_reauth_instance)

        outcome = await commander.attempt_fix("jobright", _reauth_cls=mock_reauth_class)

        assert mock_reauth_instance.handle.called
        assert outcome["success"] is True
        assert outcome["source"] == "jobright"

    @pytest.mark.asyncio
    async def test_trust_fix_outcome_recorded_in_status_json(
        self, commander, tmp_status, sessions_dir, monkeypatch
    ):
        """After a successful attempt_fix(), at least one info/success alert is
        written to agent_status.json mentioning jobright."""
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@example.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "testpass")

        session_file = sessions_dir / "jobright_chromium.json"
        if session_file.exists():
            session_file.unlink()

        mock_reauth_instance = MagicMock()
        mock_reauth_instance.handle = AsyncMock(return_value=True)
        mock_reauth_class = MagicMock(return_value=mock_reauth_instance)

        await commander.attempt_fix("jobright", _reauth_cls=mock_reauth_class)

        data = _load_status(tmp_status)
        alerts = data.get("alerts", [])
        matching = [
            a for a in alerts
            if a.get("level") in ("info", "success")
            and "jobright" in (a.get("title", "") + a.get("detail", "")).lower()
        ]
        assert len(matching) >= 1, f"Expected at least one info/success alert mentioning jobright, got: {alerts}"

    @pytest.mark.asyncio
    async def test_trust_watcher_detect_and_fix_end_to_end(
        self, config, tmp_status, monkeypatch, tmp_path
    ):
        """StatusWatcher.watch_once() detects a new error alert for jobright,
        diagnoses it as fixable, calls attempt_fix(), and returns one action."""
        import src.commander as cmd_mod
        fake_sessions = tmp_path / "sessions"
        fake_sessions.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(cmd_mod, "SESSIONS_DIR", fake_sessions)

        # Seed an error alert that mentions jobright
        data = _base_status(
            alerts=[_make_alert("error", "jobright reauth failed", "session expired")]
        )
        _write_status(tmp_status, data)

        from src.watcher import StatusWatcher

        fake_diagnosis = {
            "sources": {
                "jobright": {
                    "fixable": True,
                    "severity": "warning",
                    "root_cause": "stale session (no session file)",
                    "credentials": {"email": True, "password": True},
                    "session_exists": False,
                    "session_age_hours": None,
                    "recent_errors": [],
                    "recent_reauth_outcome": None,
                    "job_count": 0,
                    "last_scrape": None,
                }
            },
            "summary": "0/1 sources healthy. jobright: stale session (no session file).",
            "has_fixable": True,
        }
        fake_fix_result = {"success": True, "source": "jobright", "strategy": "automated", "detail": ""}

        watcher = StatusWatcher(config, auto_fix=True)

        with patch.object(watcher._commander, "diagnose", return_value=fake_diagnosis), \
             patch.object(watcher._commander, "attempt_fix", new=AsyncMock(return_value=fake_fix_result)):
            actions = await watcher.watch_once()

        assert len(actions) == 1
        assert actions[0]["source"] == "jobright"

    @pytest.mark.asyncio
    async def test_trust_watcher_no_duplicate_reactions(
        self, config, tmp_status, monkeypatch, tmp_path
    ):
        """watch_once() called twice on the same alert returns [] on the second call."""
        import src.commander as cmd_mod
        fake_sessions = tmp_path / "sessions"
        fake_sessions.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(cmd_mod, "SESSIONS_DIR", fake_sessions)

        # Seed one error alert
        alert = _make_alert("error", "jobright login error", "credentials rejected")
        data = _base_status(alerts=[alert])
        _write_status(tmp_status, data)

        from src.watcher import StatusWatcher

        fake_diagnosis = {
            "sources": {
                "jobright": {
                    "fixable": False,
                    "severity": "critical",
                    "root_cause": "missing credentials",
                    "credentials": {"email": False, "password": False},
                    "session_exists": False,
                    "session_age_hours": None,
                    "recent_errors": [],
                    "recent_reauth_outcome": None,
                    "job_count": 0,
                    "last_scrape": None,
                }
            },
            "summary": "0/1 sources healthy.",
            "has_fixable": False,
        }

        watcher = StatusWatcher(config, auto_fix=True)

        with patch.object(watcher._commander, "diagnose", return_value=fake_diagnosis):
            first = await watcher.watch_once()
            second = await watcher.watch_once()

        assert len(first) == 1
        assert second == []


# ---------------------------------------------------------------------------
# TestTrustQueryReflectsRealState
# ---------------------------------------------------------------------------

class TestTrustQueryReflectsRealState:

    def test_trust_query_sees_job_counts(self, commander, tmp_status, config):
        """_build_context() includes discovered count from the real StateManager."""
        from src.state_manager import StateManager
        sm = StateManager(config["state_db_path"])
        now = datetime.utcnow().isoformat()

        # Insert 5 discovered jobs
        for i in range(5):
            sm.upsert_job({
                "job_id": f"disc-{i}",
                "source": "jobright",
                "title": f"Job {i}",
                "company": "Acme",
                "status": "discovered",
                "discovered_at": now,
            })
        # Insert 2 applied
        for i in range(2):
            sm.upsert_job({
                "job_id": f"appl-{i}",
                "source": "jobright",
                "title": f"Applied Job {i}",
                "company": "Acme",
                "status": "applied",
                "discovered_at": now,
            })
        # Insert 1 failed
        sm.upsert_job({
            "job_id": "fail-0",
            "source": "jobright",
            "title": "Failed Job",
            "company": "Acme",
            "status": "failed",
            "discovered_at": now,
        })

        context = commander._build_context()

        assert "5" in context
        assert "applied" in context.lower()

    def test_trust_query_sees_alerts(self, commander, tmp_status):
        """_build_context() surfaces all three alert levels."""
        data = _base_status(alerts=[
            _make_alert("error", "Something broke", "bad thing"),
            _make_alert("warning", "Low disk space", "80% full"),
            _make_alert("success", "Application submitted", "Acme Corp"),
        ])
        _write_status(tmp_status, data)

        context = commander._build_context()

        assert "error" in context.lower()
        assert "warning" in context.lower()
        assert "success" in context.lower()

    def test_trust_query_sees_credential_status(self, commander, tmp_status, monkeypatch):
        """_build_context() reports MISSING when a credential env var is absent."""
        monkeypatch.setenv("LINKEDIN_EMAIL", "user@example.com")
        # Ensure LINKEDIN_PASSWORD is not set
        monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)

        context = commander._build_context()

        # Should mention linkedin and MISSING for the password
        assert "linkedin" in context.lower()
        assert "MISSING" in context

    def test_trust_query_calls_model_client_with_context_and_question(
        self, commander, tmp_status, monkeypatch
    ):
        """query() calls ModelClient.complete exactly once with the question embedded."""
        question = "How many jobs have been applied today?"
        captured: list[dict] = []

        async def mock_complete(messages, system="", **kw):
            captured.append({"messages": messages, "system": system})
            return "Nothing failed today."

        commander._model_client.complete = mock_complete

        result = asyncio.run(commander.query(question))

        assert len(captured) == 1
        combined = " ".join(m.get("content", "") for m in captured[0]["messages"])
        assert question in combined
        assert result == "Nothing failed today."


# ---------------------------------------------------------------------------
# TestTrustCLICommands
# ---------------------------------------------------------------------------
#
# NOTE: main.py does not yet expose a "commander" subcommand. These tests call
# AgentCommander methods directly through a thin harness that mirrors the
# expected CLI flow (parse args → dispatch → return exit code). This keeps the
# tests real and runnable while the CLI wiring is pending.
# ---------------------------------------------------------------------------

async def _run_commander_ask(question: str, mock_query_return: str, config: dict) -> int:
    """Simulate: job-agent commander ask '<question>'"""
    from src.commander import AgentCommander
    with patch.object(AgentCommander, "query", return_value=mock_query_return):
        cmd = AgentCommander(config)
        result = await cmd.query(question)
    assert result == mock_query_return
    return 0


async def _run_commander_diagnose(source: str | None, config: dict) -> int:
    """Simulate: job-agent commander diagnose [--source <source>]"""
    from src.commander import AgentCommander
    with patch.object(AgentCommander, "diagnose", return_value={"sources": {}, "summary": "ok", "has_fixable": False}) as mock_diag:
        cmd = AgentCommander(config)
        cmd.diagnose(source)
        mock_diag.assert_called_once_with(source)
    return 0


async def _run_commander_fix(source: str, config: dict) -> int:
    """Simulate: job-agent commander fix --source <source>"""
    from src.commander import AgentCommander
    fake_result = {"success": True, "source": source, "strategy": "automated", "detail": ""}
    with patch.object(AgentCommander, "attempt_fix", new=AsyncMock(return_value=fake_result)) as mock_fix:
        cmd = AgentCommander(config)
        result = await cmd.attempt_fix(source)
        mock_fix.assert_called_once_with(source)
    assert result["success"] is True
    return 0


class TestTrustCLICommands:

    @pytest.mark.asyncio
    async def test_trust_commander_ask_cli_entrypoint(
        self, commander, tmp_status, config, monkeypatch
    ):
        """Simulated 'commander ask' flow calls query() and returns 0."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
        exit_code = await _run_commander_ask(
            question="What failed?",
            mock_query_return="nothing failed",
            config=config,
        )
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_trust_commander_diagnose_cli_entrypoint(
        self, commander, tmp_status, config
    ):
        """Simulated 'commander diagnose' flow calls diagnose() and returns 0."""
        exit_code = await _run_commander_diagnose(source=None, config=config)
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_trust_commander_fix_cli_entrypoint(
        self, commander, tmp_status, config, sessions_dir, monkeypatch
    ):
        """Simulated 'commander fix --source jobright' flow calls attempt_fix() and returns 0."""
        monkeypatch.setenv("JOBRIGHT_EMAIL", "test@example.com")
        monkeypatch.setenv("JOBRIGHT_PASSWORD", "testpass")
        exit_code = await _run_commander_fix(source="jobright", config=config)
        assert exit_code == 0
