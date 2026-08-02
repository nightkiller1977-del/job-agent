"""
agent_config.py — Centralized configuration loader for job-agent behavior.

Implements the AI Commander principle: no hard-coded values. All behavioral
parameters (max_steps, timeouts, loop detection thresholds) are loaded from
a centralized config source, supporting override via environment or config.json.

The configuration hierarchy (first win):
1. Environment variables (JOBAGENT_*)
2. AI Commander centralized settings (via settings.json bridge)
3. Local config.json in the job-agent project
4. Built-in defaults (sane values, documented)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class LoopDetectionConfig:
    """Configuration for detecting and preventing response loops."""
    enabled: bool = True
    max_repeated_states: int = 3
    max_repeated_actions: int = 2
    state_hash_window: int = 5
    progress_check_interval: int = 2


@dataclass
class ProgressMetricsConfig:
    """Configuration for tracking progress toward form submission."""
    track_dom_changes: bool = True
    track_selector_usage: bool = True
    track_action_sequence: bool = True
    min_progress_threshold: float = 0.3


@dataclass
class BrowserRecoveryConfig:
    """Configuration for LLM-guided browser recovery agent."""
    enabled: bool = True
    max_steps: int = 8
    step_timeout_ms: int = 15_000
    loop_detection: LoopDetectionConfig = None
    progress_metrics: ProgressMetricsConfig = None
    success_indicators: list[str] = None
    failure_indicators: list[str] = None

    def __post_init__(self):
        if self.loop_detection is None:
            self.loop_detection = LoopDetectionConfig()
        if self.progress_metrics is None:
            self.progress_metrics = ProgressMetricsConfig()
        if self.success_indicators is None:
            self.success_indicators = [
                "thank you", "thanks", "thank", "received", "submitted",
                "success", "application sent", "application received",
                "confirmation"
            ]
        if self.failure_indicators is None:
            self.failure_indicators = [
                "error", "failed", "cannot process", "invalid input",
                "required field", "not supported", "unsupported",
                "connection failed"
            ]


@dataclass
class LLMPromptingConfig:
    """Configuration for LLM prompting and guardrails."""
    model_task: str = "reasoning"
    temperature: float = 0.3
    context_mode: str = "detailed"
    progress_visualization: bool = True
    explicit_loop_guards: bool = True


@dataclass
class TelemetryConfig:
    """Configuration for agent telemetry and monitoring."""
    track_loop_events: bool = True
    track_step_efficiency: bool = True
    track_state_transitions: bool = True
    min_dom_change_threshold: int = 100


@dataclass
class JobAgentConfig:
    """Top-level configuration for job-agent behavior."""
    browser_recovery: BrowserRecoveryConfig = None
    llm_prompting: LLMPromptingConfig = None
    telemetry: TelemetryConfig = None

    def __post_init__(self):
        if self.browser_recovery is None:
            self.browser_recovery = BrowserRecoveryConfig()
        if self.llm_prompting is None:
            self.llm_prompting = LLMPromptingConfig()
        if self.telemetry is None:
            self.telemetry = TelemetryConfig()


class ConfigLoader:
    """Loads job-agent configuration from centralized sources."""

    def __init__(self, project_root: Path | str = None):
        self.project_root = Path(project_root or Path(__file__).parent.parent)
        self._config_cache: Optional[JobAgentConfig] = None

    def load(self, force_reload: bool = False) -> JobAgentConfig:
        """
        Load configuration from the hierarchy (env → settings → config.json → defaults).
        Caches the result unless force_reload=True.
        """
        if self._config_cache and not force_reload:
            return self._config_cache

        # Start with defaults
        config_dict = self._load_defaults()

        # Layer on config.json if it exists
        config_dict = self._merge_config_file(config_dict)

        # Layer on AI Commander settings if available
        config_dict = self._merge_ai_commander_settings(config_dict)

        # Layer on environment variable overrides (JOBAGENT_*)
        config_dict = self._merge_env_overrides(config_dict)

        # Convert dict to typed config
        self._config_cache = self._dict_to_config(config_dict)
        return self._config_cache

    def _load_defaults(self) -> dict:
        """Return the default configuration as a dict."""
        return asdict(JobAgentConfig())

    def _merge_config_file(self, base: dict) -> dict:
        """Merge config.json if it exists at project root."""
        config_path = self.project_root / "config.json"
        if not config_path.exists():
            return base

        try:
            with open(config_path) as f:
                file_config = json.load(f)
            if "jobAgent" in file_config:
                return self._deep_merge(base, {"jobAgent": file_config["jobAgent"]})
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load config.json: {e}")
        return base

    def _merge_ai_commander_settings(self, base: dict) -> dict:
        """
        Merge from AI Commander's centralized settings if available.
        Looks for settings at ~/Library/Application Support/ai-command-center/settings-v3.json
        """
        try:
            home = Path.home()
            settings_path = (
                home / "Library" / "Application Support" /
                "ai-command-center" / "settings-v3.json"
            )
            if not settings_path.exists():
                return base

            with open(settings_path) as f:
                settings = json.load(f)
            if "jobAgent" in settings:
                return self._deep_merge(base, {"jobAgent": settings["jobAgent"]})
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to load AI Commander settings: {e}"
            )
        return base

    def _merge_env_overrides(self, base: dict) -> dict:
        """
        Apply environment variable overrides.
        Format: JOBAGENT_<SECTION>_<KEY>=value
        Example: JOBAGENT_BROWSER_RECOVERY_MAX_STEPS=12
        """
        job_agent_config = base.get("jobAgent", {})

        for key, value in os.environ.items():
            if not key.startswith("JOBAGENT_"):
                continue

            # Parse JOBAGENT_SECTION_KEY -> path in config
            parts = key[9:].lower().split("_")  # strip 'JOBAGENT_', lowercase
            if len(parts) < 2:
                continue

            # Navigate the config dict and set the value
            current = job_agent_config
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]

            # Try to coerce the value to the right type
            final_key = parts[-1]
            current[final_key] = self._coerce_env_value(value)

        base["jobAgent"] = job_agent_config
        return base

    def _coerce_env_value(self, value: str) -> bool | int | float | str:
        """Coerce an environment variable string to the appropriate type."""
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        if value.isdigit():
            return int(value)
        try:
            return float(value)
        except ValueError:
            return value

    @staticmethod
    def _deep_merge(base: dict, overlay: dict) -> dict:
        """Recursively merge overlay into base, preserving base values."""
        result = base.copy()
        for key, value in overlay.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigLoader._deep_merge(result[key], value)
            elif key not in result:
                result[key] = value
        return result

    @staticmethod
    def _dict_to_config(d: dict) -> JobAgentConfig:
        """Convert a dict to typed JobAgentConfig."""
        job_agent_dict = d.get("jobAgent", {})

        browser_recovery = BrowserRecoveryConfig(
            **{k: v for k, v in job_agent_dict.get("browser_recovery", {}).items()}
        )
        llm_prompting = LLMPromptingConfig(
            **{k: v for k, v in job_agent_dict.get("llm_prompting", {}).items()}
        )
        telemetry = TelemetryConfig(
            **{k: v for k, v in job_agent_dict.get("telemetry", {}).items()}
        )

        return JobAgentConfig(
            browser_recovery=browser_recovery,
            llm_prompting=llm_prompting,
            telemetry=telemetry,
        )


# Global singleton for convenience
_global_loader: Optional[ConfigLoader] = None


def get_config() -> JobAgentConfig:
    """Get the global job-agent configuration (lazy singleton)."""
    global _global_loader
    if _global_loader is None:
        _global_loader = ConfigLoader()
    return _global_loader.load()


def reload_config() -> JobAgentConfig:
    """Force reload of the global configuration."""
    global _global_loader
    if _global_loader is None:
        _global_loader = ConfigLoader()
    return _global_loader.load(force_reload=True)
