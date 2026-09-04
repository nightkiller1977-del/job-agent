"""
Tests for the credentials management system.

Covers:
  - GET /api/credentials  — removed (ACES-65): it returned decrypted passwords to
    any SYNC_SECRET holder and the agent no longer fetches credentials over the
    network. Guarded so it cannot quietly come back.
  - POST /api/credentials — encryption at rest, upsert, invalid platform
  - _encrypt_password / _decrypt_password round-trip
  - orchestrator → dashboard push sync (_push_apply_attempt_to_cloud)
"""
import os
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# ── Generate a real test key so encryption helpers work in tests ────────────
_TEST_KEY = Fernet.generate_key().decode()

# Mock env before importing the app so DATABASE_URL / SYNC_SECRET are set
with patch.dict("os.environ", {
    "DATABASE_URL": "postgresql://localhost/dummy",
    "SYNC_SECRET": "testsecret",
    "CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY,
}):
    from dashboard.main import app, _encrypt_password, _decrypt_password


# ── Helper: encrypt a value the same way the app would ─────────────────────
def _enc(plain: str) -> str:
    return Fernet(_TEST_KEY.encode()).encrypt(plain.encode()).decode()


class TestEncryptionHelpers(unittest.TestCase):
    """Unit tests for the standalone encrypt/decrypt helpers."""

    def test_round_trip_with_key(self):
        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            encrypted = _encrypt_password("supersecret")
            self.assertNotEqual(encrypted, "supersecret")
            decrypted = _decrypt_password(encrypted)
            self.assertEqual(decrypted, "supersecret")

    def test_no_key_returns_plaintext(self):
        """Without a key the helpers are pass-through (warning logged)."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove key from env
            env = {k: v for k, v in os.environ.items() if k != "CREDENTIAL_ENCRYPTION_KEY"}
            with patch.dict("os.environ", env, clear=True):
                self.assertEqual(_encrypt_password("plain"), "plain")
                self.assertEqual(_decrypt_password("plain"), "plain")

    def test_decrypt_plaintext_fallback(self):
        """Decrypting a plaintext value (pre-migration) should return it unchanged."""
        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            # "oldplaintext" was stored before encryption was enabled
            result = _decrypt_password("oldplaintext")
            self.assertEqual(result, "oldplaintext")

    def test_empty_password_is_safe(self):
        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            self.assertEqual(_decrypt_password(""), "")


class TestCredentialsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    # ── GET /api/credentials — removed (ACES-65) ─────────────────────────────

    @patch("dashboard.main.get_conn")
    def test_get_credentials_endpoint_removed(self, mock_get_conn):
        """The route that returned DECRYPTED passwords to any SYNC_SECRET holder is
        gone, and the agent no longer pulls credentials over the network at run
        start (src/secret_store.py is the only resolver). Only POST — the dashboard
        UI's save — remains on this path, so GET must be 405, never 200 or 403.
        Even a correct secret must not get data back."""
        resp = self.client.get("/api/credentials", headers={"X-Sync-Secret": "testsecret"})
        self.assertEqual(resp.status_code, 405)
        mock_get_conn.assert_not_called()

    # ── POST /api/credentials ────────────────────────────────────────────────

    @patch("dashboard.main.get_conn")
    def test_save_credentials_encrypts_password(self, mock_get_conn):
        """The stored password arg should be the encrypted form, not plaintext."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            resp = self.client.post("/api/credentials", json={
                "platform": "indeed",
                "email": "save@indeed.com",
                "password": "secretpassword",
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "platform": "indeed"})

        args = mock_cursor.execute.call_args[0]
        self.assertIn("INSERT INTO credentials", args[0])
        platform_arg, email_arg, stored_pw_arg = args[1]
        self.assertEqual(platform_arg, "indeed")
        self.assertEqual(email_arg, "save@indeed.com")
        # Stored value must NOT be plaintext
        self.assertNotEqual(stored_pw_arg, "secretpassword")
        # And must be decryptable back to the original
        decrypted = Fernet(_TEST_KEY.encode()).decrypt(stored_pw_arg.encode()).decode()
        self.assertEqual(decrypted, "secretpassword")

    @patch("dashboard.main.get_conn")
    def test_save_credentials_all_valid_platforms(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        for platform in ("indeed", "linkedin", "jobright"):
            with self.subTest(platform=platform):
                resp = self.client.post("/api/credentials", json={
                    "platform": platform,
                    "email": f"user@{platform}.com",
                    "password": "pw",
                })
                self.assertEqual(resp.status_code, 200)

    def test_save_credentials_invalid_platform(self):
        resp = self.client.post("/api/credentials", json={
            "platform": "company_portal",   # was removed — should now be rejected
            "email": "x@x.com",
            "password": "pw",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("platform must be one of", resp.json()["detail"])

    def test_save_credentials_unknown_platform(self):
        resp = self.client.post("/api/credentials", json={
            "platform": "random_site",
            "email": "x@x.com",
            "password": "pw",
        })
        self.assertEqual(resp.status_code, 400)


class TestAgentCredentialsSync(unittest.IsolatedAsyncioTestCase):
    """Tests for the orchestrator ↔ dashboard sync boundary."""

    def test_orchestrator_has_no_network_credential_fetch(self):
        """ACES-65: credential resolution is src/secret_store.py only (.env → central
        SOPS store). The legacy per-run HTTP pull from the dashboard — a network
        dependency at cred-load time that always ended in 'Kept local .env' — is
        gone and must not be reintroduced under the same name."""
        from src.orchestrator import Orchestrator
        self.assertFalse(hasattr(Orchestrator, "load_credentials_from_dashboard"))

    @patch("httpx.AsyncClient")
    async def test_push_apply_attempt_syncs_extra_json_to_dashboard(self, mock_client_class):
        from src.orchestrator import Orchestrator
        from src.state_manager import StateManager

        old_url    = os.environ.get("DASHBOARD_URL")
        old_secret = os.environ.get("SYNC_SECRET")

        os.environ["DASHBOARD_URL"] = "https://dashboard-test.com"
        os.environ["SYNC_SECRET"]   = "testsecret"

        try:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=None)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post = AsyncMock(return_value=mock_response)

            with tempfile.TemporaryDirectory() as tmpdir:
                orchestrator = Orchestrator()
                orchestrator.state = StateManager(os.path.join(tmpdir, "jobs.db"))
                orchestrator.state.upsert_job({
                    "job_id": "job-123",
                    "source": "linkedin",
                    "title": "Director Engineering",
                    "company": "ExampleCo",
                    "url": "https://www.linkedin.com/jobs/view/123/",
                    "status": "approved",
                    "score": 95,
                })
                orchestrator.state.record_apply_attempt(
                    "job-123",
                    "linkedin_stuck_on_required_field",
                    "Required question needs an answer.",
                )

                await orchestrator._push_apply_attempt_to_cloud("job-123")

            mock_client.post.assert_awaited_once()
            url = mock_client.post.await_args.kwargs["url"] if "url" in mock_client.post.await_args.kwargs else mock_client.post.await_args.args[0]
            payload = mock_client.post.await_args.kwargs["json"]
            headers = mock_client.post.await_args.kwargs["headers"]

            self.assertEqual(url, "https://dashboard-test.com/api/sync")
            self.assertEqual(headers, {"X-Sync-Secret": "testsecret"})
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["job_id"], "job-123")
            self.assertEqual(payload[0]["status"], "approved")
            extra = json.loads(payload[0]["extra_json"])
            self.assertEqual(extra["apply_last_status"], "linkedin_stuck_on_required_field")
            self.assertEqual(extra["apply_last_detail"], "Required question needs an answer.")
            self.assertEqual(extra["apply_attempt_count"], 1)
        finally:
            for k, v in [("DASHBOARD_URL", old_url), ("SYNC_SECRET", old_secret)]:
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
