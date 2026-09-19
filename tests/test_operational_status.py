import json
from pathlib import Path

from src.operational_status import collect_operational_status, show_operational_status


def test_status_includes_current_observation_timestamp(tmp_path, capsys, monkeypatch):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python3").write_text("")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "profile.json").write_text("{}")
    monkeypatch.setattr("src.operational_status._git", lambda *args: "main")

    record = collect_operational_status(tmp_path)
    show_operational_status(tmp_path)

    rendered = json.loads(capsys.readouterr().out)
    assert record["observed_at"]
    assert rendered["observed_at"]
    assert rendered["runtime_branch"] == "main"
    assert rendered["venv_python_present"] is True
    assert rendered["config_present"] is True
    assert rendered["profile_present"] is True
    assert "scheduler_last_result" in rendered
