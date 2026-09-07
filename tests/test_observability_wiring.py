"""Offline regression tests for canonical-secret and hosted-entry-point wiring."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

store = load("tested_secret_store", ROOT / "src/secret_store.py")
observability = load("tested_dashboard_observability", ROOT / "dashboard/observability.py")

class SecretWiringTests(unittest.TestCase):
    def tearDown(self):
        store.clear_cache()

    def test_default_catalog_reads_shared_pair_through_sops(self):
        plaintext = "LOKI_URL_REMOTE=https://logs.example.grafana.net/loki/api/v1/push\nLOKI_REMOTE_AUTH=Basic dGVzdDp0ZXN0\n"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "secrets.enc.env").write_text("synthetic encrypted-file fixture")
            result = subprocess.CompletedProcess(["sops"], 0, plaintext, "")
            with patch.dict(os.environ, {"AICC_SECRETS_DIR": directory}, clear=True), \
                    patch.object(store.shutil, "which", lambda name: "/synthetic/sops" if name == "sops" else None), \
                    patch.object(store.subprocess, "run", return_value=result) as decrypt:
                store.clear_cache()
                filled = store.fill_missing()
                self.assertIn("LOKI_URL_REMOTE", filled)
                self.assertIn("LOKI_REMOTE_AUTH", filled)
                self.assertEqual(os.environ["LOKI_REMOTE_AUTH"], "Basic dGVzdDp0ZXN0")
                decrypt.assert_called_once()
                self.assertEqual(decrypt.call_args.args[0][:2], ["sops", "-d"])
                self.assertIn("LOKI_USER", store.CANONICAL_KEYS)
                self.assertIn("LOKI_API_KEY", store.CANONICAL_KEYS)

    def test_nonempty_platform_settings_are_preserved(self):
        with patch.dict(os.environ, {"LOKI_URL_REMOTE": "https://platform.example/push", "LOKI_REMOTE_AUTH": "platform-auth"}, clear=True), \
                patch.object(store, "_cli_get", return_value=None), \
                patch.object(store, "_read_store", return_value={"LOKI_REMOTE_AUTH": "store-auth"}):
            store.fill_missing()
            self.assertEqual(os.environ["LOKI_REMOTE_AUTH"], "platform-auth")
            self.assertEqual(os.environ["LOKI_URL_REMOTE"], "https://platform.example/push")

class EntryPointTests(unittest.TestCase):
    def test_render_working_directory_entry_wraps_original_application(self):
        original = object()
        main = types.ModuleType("main"); main.app = original
        with patch.dict(sys.modules, {"main": main, "observability": observability}):
            module = load("observed_under_test", ROOT / "dashboard/observed.py")
        self.assertIs(module.app.app, original)
        self.assertIsInstance(module.app, observability.ObserveASGI)
        self.assertEqual(module.emitter.service, "job-agent-dashboard")
        blueprint = (ROOT / "render.yaml").read_text()
        self.assertIn("rootDir: dashboard", blueprint)
        self.assertIn("startCommand: uvicorn observed:app", blueprint)

    def test_repository_root_entry_also_wraps_original_application(self):
        original = object()
        package = types.ModuleType("dashboard"); package.__path__ = [str(ROOT / "dashboard")]
        main = types.ModuleType("dashboard.main"); main.app = original
        with patch.dict(sys.modules, {"dashboard": package, "dashboard.main": main, "dashboard.observability": observability}):
            module = load("dashboard.observed_under_test", ROOT / "dashboard/observed.py")
        self.assertIs(module.app.app, original)
        self.assertEqual(module.emitter.service, "job-agent-dashboard")

if __name__ == "__main__":
    unittest.main()
