"""
Tests for the credentials management system.

Covers:
  - GET /api/credentials  — removed (ACES-65): it returned decrypted passwords to
    any SYNC_SECRET holder and the agent no longer fetches credentials over the
    network. Guarded so it cannot quietly come back.
  - POST /api/credentials — encryption at rest, upsert, invalid platform
  - _encrypt_password round-trip
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

# Mock env before importing the app so MONGODB_URI / SYNC_SECRET are set
with patch.dict("os.environ", {
    "MONGODB_URI": "",
    "SYNC_SECRET": "testsecret",
    "CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY,
}):
    from dashboard.main import app, _encrypt_password


# ── Helper: encrypt a value the same way the app would ─────────────────────
def _enc(plain: str) -> str:
    return Fernet(_TEST_KEY.encode()).encrypt(plain.encode()).decode()


class TestEncryptionHelpers(unittest.TestCase):
    """Unit tests for _encrypt_password (decrypt was removed in the MongoDB migration)."""

    def test_encrypt_produces_ciphertext(self):
        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            encrypted = _encrypt_password("supersecret")
            self.assertNotEqual(encrypted, "supersecret")
            decrypted = Fernet(_TEST_KEY.encode()).decrypt(encrypted.encode()).decode()
            self.assertEqual(decrypted, "supersecret")


class TestCredentialsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    # ── GET /api/credentials — removed (ACES-65) ─────────────────────────────

    def test_get_credentials_endpoint_removed(self):
        """GET must be 405 — only POST remains."""
        resp = self.client.get("/api/credentials", headers={"X-Sync-Secret": "testsecret"})
        self.assertEqual(resp.status_code, 405)

    # ── POST /api/credentials ────────────────────────────────────────────────

    @patch("dashboard.main.get_db")
    def test_save_credentials_encrypts_password(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY}):
            resp = self.client.post("/api/credentials", json={
                "platform": "indeed",
                "email": "save@indeed.com",
                "password": "secretpassword",
            })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "platform": "indeed"})

        call_args = mock_db.credentials.update_one.call_args
        update_doc = call_args[0][1]["$set"]
        self.assertEqual(update_doc["email"], "save@indeed.com")
        self.assertNotEqual(update_doc["password"], "secretpassword")
        decrypted = Fernet(_TEST_KEY.encode()).decrypt(update_doc["password"].encode()).decode()
        self.assertEqual(decrypted, "secretpassword")

    def test_index_context_never_contains_plaintext_passwords(self):
        """GET / template context must carry only email + password_set flag."""
        import inspect
        import dashboard.main as dm
        src = inspect.getsource(dm)
        self.assertIn('"password_set": bool(', src)

    @patch("dashboard.main.get_db")
    def test_save_credentials_all_valid_platforms(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

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
            "platform": "company_portal",
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
