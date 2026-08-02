"""
Unit tests for ConfigLoader: precedence, camelCase normalization,
nested dataclass construction, and env-var overrides.
"""
import json
import os
import pytest
from pathlib import Path
from src.agent_config import (
    ConfigLoader,
    JobAgentConfig,
    BrowserRecoveryConfig,
    LoopDetectionConfig,
    ProgressMetricsConfig,
    _normalize_keys,
    _camel_to_snake,
)


class TestCamelToSnake:
    def test_single_word(self):
        assert _camel_to_snake("enabled") == "enabled"

    def test_camel_case(self):
        assert _camel_to_snake("maxSteps") == "max_steps"
        assert _camel_to_snake("loopDetection") == "loop_detection"
        assert _camel_to_snake("browserRecovery") == "browser_recovery"
        assert _camel_to_snake("minProgressThreshold") == "min_progress_threshold"

    def test_already_snake(self):
        assert _camel_to_snake("max_steps") == "max_steps"


class TestNormalizeKeys:
    def test_flat_dict(self):
        result = _normalize_keys({"maxSteps": 8, "enabled": True})
        assert result == {"max_steps": 8, "enabled": True}

    def test_nested_dict(self):
        result = _normalize_keys({"browserRecovery": {"maxSteps": 8}})
        assert result == {"browser_recovery": {"max_steps": 8}}

    def test_list_values_unchanged(self):
        result = _normalize_keys({"successIndicators": ["thank you"]})
        assert result == {"success_indicators": ["thank you"]}


class TestDeepMerge:
    def test_overlay_wins_over_base(self):
        """Higher-priority sources must override lower-priority ones."""
        result = ConfigLoader._deep_merge(
            {"max_steps": 6, "enabled": True},
            {"max_steps": 12},
        )
        assert result["max_steps"] == 12
        assert result["enabled"] is True

    def test_nested_overlay_wins(self):
        result = ConfigLoader._deep_merge(
            {"browser_recovery": {"max_steps": 6, "enabled": True}},
            {"browser_recovery": {"max_steps": 12}},
        )
        assert result["browser_recovery"]["max_steps"] == 12
        assert result["browser_recovery"]["enabled"] is True

    def test_new_keys_added(self):
        result = ConfigLoader._deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}


class TestParseEnvKey:
    def test_browser_recovery_top_level(self):
        path, key = ConfigLoader._parse_env_key("browser_recovery_max_steps")
        assert path == ["browser_recovery"]
        assert key == "max_steps"

    def test_loop_detection_nested(self):
        path, key = ConfigLoader._parse_env_key("browser_recovery_loop_detection_enabled")
        assert path == ["browser_recovery", "loop_detection"]
        assert key == "enabled"

    def test_progress_metrics_nested(self):
        path, key = ConfigLoader._parse_env_key("browser_recovery_progress_metrics_min_progress_threshold")
        assert path == ["browser_recovery", "progress_metrics"]
        assert key == "min_progress_threshold"

    def test_unrecognized_returns_none(self):
        assert ConfigLoader._parse_env_key("something_random") is None

    def test_section_only_no_key_returns_none(self):
        assert ConfigLoader._parse_env_key("browser_recovery") is None


class TestDictToConfig:
    def test_defaults_when_empty(self):
        cfg = ConfigLoader._dict_to_config({})
        assert isinstance(cfg, JobAgentConfig)
        assert cfg.browser_recovery.max_steps == 8
        assert isinstance(cfg.browser_recovery.loop_detection, LoopDetectionConfig)

    def test_nested_dict_converted_to_dataclass(self):
        """Passing loop_detection as a dict must not cause AttributeError."""
        cfg = ConfigLoader._dict_to_config({
            "jobAgent": {
                "browser_recovery": {
                    "max_steps": 10,
                    "loop_detection": {"enabled": False, "max_repeated_states": 5},
                }
            }
        })
        assert isinstance(cfg.browser_recovery.loop_detection, LoopDetectionConfig)
        assert cfg.browser_recovery.loop_detection.enabled is False
        assert cfg.browser_recovery.loop_detection.max_repeated_states == 5

    def test_unknown_keys_ignored(self):
        """Extra keys from a future config version must not raise TypeError."""
        cfg = ConfigLoader._dict_to_config({
            "jobAgent": {
                "browser_recovery": {"max_steps": 9, "future_option": "x"}
            }
        })
        assert cfg.browser_recovery.max_steps == 9


class TestConfigLoaderPrecedence:
    def test_config_json_overrides_defaults(self, tmp_path):
        config_json = tmp_path / "config.json"
        config_json.write_text(json.dumps({"jobAgent": {"browserRecovery": {"maxSteps": 15}}}))
        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load()
        assert cfg.browser_recovery.max_steps == 15

    def test_camelcase_settings_loaded(self, tmp_path):
        """Settings using camelCase keys (as settings-v3.json does) must be applied."""
        config_json = tmp_path / "config.json"
        config_json.write_text(json.dumps({
            "jobAgent": {
                "browserRecovery": {
                    "maxSteps": 10,
                    "loopDetection": {"maxRepeatedStates": 4},
                }
            }
        }))
        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load()
        assert cfg.browser_recovery.max_steps == 10
        assert cfg.browser_recovery.loop_detection.max_repeated_states == 4

    def test_env_override_beats_config_file(self, tmp_path, monkeypatch):
        config_json = tmp_path / "config.json"
        config_json.write_text(json.dumps({"jobAgent": {"browserRecovery": {"maxSteps": 10}}}))
        monkeypatch.setenv("JOBAGENT_BROWSER_RECOVERY_MAX_STEPS", "20")
        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load()
        assert cfg.browser_recovery.max_steps == 20

    def test_env_nested_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JOBAGENT_BROWSER_RECOVERY_LOOP_DETECTION_ENABLED", "false")
        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load()
        assert cfg.browser_recovery.loop_detection.enabled is False

    def test_missing_config_file_uses_defaults(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        cfg = loader.load()
        assert cfg.browser_recovery.max_steps == 8  # default

    def test_force_reload_clears_cache(self, tmp_path):
        config_json = tmp_path / "config.json"
        config_json.write_text(json.dumps({"jobAgent": {"browserRecovery": {"maxSteps": 7}}}))
        loader = ConfigLoader(project_root=tmp_path)
        cfg1 = loader.load()
        config_json.write_text(json.dumps({"jobAgent": {"browserRecovery": {"maxSteps": 11}}}))
        cfg2 = loader.load(force_reload=True)
        assert cfg1.browser_recovery.max_steps == 7
        assert cfg2.browser_recovery.max_steps == 11
