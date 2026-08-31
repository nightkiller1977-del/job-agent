"""Regression (Codex/reviewer P1, PR #80): config.json is now gitignored and
optional (see config.example.json). `commander` subcommands used to re-read
config.json directly with `.read_text()`, which raises FileNotFoundError on a
clean clone that has no config.json — crashing `commander diagnose/fix/watch/
report` before they even start. They must reuse the orchestrator's
already-loaded config (which falls back to {} with a warning) instead."""
import types
from unittest.mock import MagicMock, patch

import pytest

import src.main as main_mod


@pytest.mark.asyncio
async def test_commander_report_does_not_crash_without_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "project_root", tmp_path)
    assert not (tmp_path / "config.json").exists()

    mock_orch = MagicMock()
    mock_orch.config = {}  # Orchestrator._load_config's real fallback shape

    mock_commander = MagicMock()
    mock_commander.get_report.return_value = {"summary": "ok"}
    mock_commander.format_report.return_value = "ok"

    with patch("src.orchestrator.Orchestrator", return_value=mock_orch), \
         patch("src.commander.AgentCommander", return_value=mock_commander) as MockCommander:
        result = await main_mod.main_async(types.SimpleNamespace(
            command="commander", subcommand="report"))

    assert result == 0
    MockCommander.assert_called_once_with(mock_orch.config)
