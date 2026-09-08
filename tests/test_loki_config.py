"""ACES-293 contract tests: atomic pair resolution, Basic-auth validation, env policy."""
import os
import unittest
from unittest.mock import patch

from src import loki_config

URL = "https://logs.example.grafana.net/loki/api/v1/push"
AUTH = "Basic dGVzdDp0ZXN0"  # test:test — synthetic
PROD = {"RENDER": "true"}


class BasicAuthValidationTests(unittest.TestCase):
    def test_valid_header(self):
        self.assertTrue(loki_config.validate_basic_auth(AUTH))
        self.assertEqual(loki_config.basic_auth_credentials(AUTH), ("test", "test"))

    def test_rejects_malformed(self):
        for bad in ["", "Basic", "Basic ", "Bearer dGVzdDp0ZXN0", "Basic !!!", "Basic dGVzdA==",  # no colon
                    "Basic dGVzdDo=", "Basic OnRlc3Q=",  # empty password / user
                    "Basic dGVzdDp0ZXN0\r\nX-Injected: 1", "Basic dGVzdDp0ZXN0\n", "Basic \x00", 42, None]:
            self.assertFalse(loki_config.validate_basic_auth(bad), repr(bad))
            if isinstance(bad, str):
                self.assertIsNone(loki_config.basic_auth_credentials(bad), repr(bad))

    def test_rejects_control_chars_in_decoded_credentials(self):
        import base64
        header = "Basic " + base64.b64encode(b"user:pa\nss").decode()
        self.assertFalse(loki_config.validate_basic_auth(header))


class PairResolutionTests(unittest.TestCase):
    def resolve(self, **env):
        with patch.dict(os.environ, {**PROD, **env}, clear=True):
            return loki_config.resolve_loki_config()

    def test_complete_valid_env_pair_enables(self):
        config = self.resolve(LOKI_URL_REMOTE=URL, LOKI_REMOTE_AUTH=AUTH)
        self.assertTrue(config.enabled)
        self.assertEqual((config.url, config.auth, config.source), (URL, AUTH, "env"))

    def test_url_only_partial_pair_disables(self):
        config = self.resolve(LOKI_URL_REMOTE=URL)
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "partial_pair")

    def test_auth_only_partial_pair_disables(self):
        config = self.resolve(LOKI_REMOTE_AUTH=AUTH)
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "partial_pair")

    def test_unconfigured_disables_quietly(self):
        self.assertEqual(self.resolve().reason, "unconfigured")

    def test_invalid_url_disables(self):
        for bad in ["http://plain.example/push", "https://user:pw@x.example/push", "https://x.example/push?q=1", "not a url"]:
            config = self.resolve(LOKI_URL_REMOTE=bad, LOKI_REMOTE_AUTH=AUTH)
            self.assertFalse(config.enabled, bad)
            self.assertEqual(config.reason, "invalid_url", bad)

    def test_invalid_auth_disables(self):
        config = self.resolve(LOKI_URL_REMOTE=URL, LOKI_REMOTE_AUTH="Bearer nope")
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "invalid_auth")

    def test_partial_pair_warns_once_and_never_logs_values(self):
        loki_config.reset_warnings()
        with patch.dict(os.environ, {**PROD, "LOKI_URL_REMOTE": "https://SECRETVALUE.example/push"}, clear=True):
            with self.assertLogs("loki-config", level="WARNING") as captured:
                loki_config.resolve_loki_config()
                loki_config.resolve_loki_config()
        self.assertEqual(len(captured.records), 1)
        self.assertNotIn("SECRETVALUE", "\n".join(captured.output))


class PolicyTests(unittest.TestCase):
    def test_dev_test_off_by_default(self):
        with patch.dict(os.environ, {"LOKI_URL_REMOTE": URL, "LOKI_REMOTE_AUTH": AUTH}, clear=True):
            config = loki_config.resolve_loki_config()
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "non_production_default_off")

    def test_dev_test_on_with_explicit_opt_in(self):
        with patch.dict(os.environ, {"LOKI_URL_REMOTE": URL, "LOKI_REMOTE_AUTH": AUTH, "OBSERVABILITY_REMOTE": "1"}, clear=True):
            self.assertTrue(loki_config.resolve_loki_config().enabled)

    def test_production_auto_on(self):
        with patch.dict(os.environ, {**PROD, "LOKI_URL_REMOTE": URL, "LOKI_REMOTE_AUTH": AUTH}, clear=True):
            self.assertTrue(loki_config.resolve_loki_config().enabled)

    def test_opt_out_wins_in_production(self):
        with patch.dict(os.environ, {**PROD, "LOKI_URL_REMOTE": URL, "LOKI_REMOTE_AUTH": AUTH, "OBSERVABILITY_REMOTE": "0"}, clear=True):
            config = loki_config.resolve_loki_config()
        self.assertFalse(config.enabled)
        self.assertEqual(config.reason, "opted_out")


class TelemetryConsolidationTests(unittest.TestCase):
    """src/telemetry.py routes through the same resolver — no third Loki path."""

    def _telemetry(self):
        try:
            from src import telemetry
        except ImportError as exc:  # openlit optional in some envs
            self.skipTest(f"telemetry deps unavailable: {exc}")
        return telemetry

    def test_remote_pair_selected_when_enabled(self):
        telemetry = self._telemetry()
        with patch.dict(os.environ, {**PROD, "LOKI_URL_REMOTE": URL, "LOKI_REMOTE_AUTH": AUTH}, clear=True):
            self.assertEqual(telemetry.resolve_loki_url(), URL)
            self.assertEqual(telemetry.resolve_loki_auth(), ("test", "test"))

    def test_partial_pair_falls_back_to_local_without_mixing(self):
        telemetry = self._telemetry()
        with patch.dict(os.environ, {**PROD, "LOKI_URL_REMOTE": URL, "LOKI_URL": "http://localhost:3100/loki/api/v1/push"}, clear=True):
            self.assertEqual(telemetry.resolve_loki_url(), "http://localhost:3100/loki/api/v1/push")
            self.assertIsNone(telemetry.resolve_loki_auth())

    def test_legacy_split_keys_are_dead(self):
        telemetry = self._telemetry()
        with patch.dict(os.environ, {**PROD, "LOKI_USER": "legacy", "LOKI_API_KEY": "legacy"}, clear=True):
            self.assertIsNone(telemetry.resolve_loki_auth())


if __name__ == "__main__":
    unittest.main()
