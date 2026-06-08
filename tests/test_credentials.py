"""
Tests for the credentials management system.

Covers:
  - GET /api/credentials  — auth gate, decryption, plaintext fallback
  - POST /api/credentials — encryption at rest, upsert, invalid platform
  - _encrypt_password / _decrypt_password round-trip
  - load_credentials_from_dashboard() in the orchestrator
"""
import os
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

    # ── GET /api/credentials ─────────────────────────────────────────────────

    @patch("dashboard.main.get_conn")
    def test_get_credentials_unauthorized_missing_header(self, _):
        resp = self.client.get("/api/credentials")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("Invalid sync secret", resp.json()["detail"])

    @patch("dashboard.main.get_conn")
    def test_get_credentials_unauthorized_wrong_header(self, _):
        resp = self.client.get("/api/credentials", headers={"X-Sync-Secret": "wrongvalue"})
        self.assertEqual(resp.status_code, 403)

    @patch("dashboard.main.get_conn")
    def test_get_credentials_decrypts_passwords(self, mock_get_conn):
        """Encrypted passwords stored in DB should be decrypted in the response."""
        encrypted_pw = _enc("mypassword")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            {"platform": "indeed",   "email": "a@indeed.com",   "password": encrypted_pw},
            {"platform": "linkedin", "email": "a@linkedin.com", "password": encrypted_pw},
        ]

        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY,
                                        "SYNC_SECRET": "testsecret"}):
            resp = self.client.get("/api/credentials", headers={"X-Sync-Secret": "testsecret"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        # Passwords must be returned decrypted
        self.assertEqual(data[0]["password"], "mypassword")
        self.assertEqual(data[1]["password"], "mypassword")

    @patch("dashboard.main.get_conn")
    def test_get_credentials_plaintext_fallback(self, mock_get_conn):
        """Pre-encryption plaintext passwords are returned unchanged."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            {"platform": "jobright", "email": "j@jr.com", "password": "oldplaintext"},
        ]

        with patch.dict("os.environ", {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY,
                                        "SYNC_SECRET": "testsecret"}):
            resp = self.client.get("/api/credentials", headers={"X-Sync-Secret": "testsecret"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["password"], "oldplaintext")

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
    """Tests for orchestrator.load_credentials_from_dashboard()."""

    @patch("httpx.AsyncClient")
    async def test_load_credentials_sets_env_vars(self, mock_client_class):
        from src.orchestrator import Orchestrator

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
            mock_response.json.return_value = [
                {"platform": "indeed",   "email": "cloud@indeed.com",   "password": "cloudpw1"},
                {"platform": "linkedin", "email": "cloud@linkedin.com", "password": "cloudpw2"},
                {"platform": "jobright", "email": "cloud@jobright.com", "password": "cloudpw3"},
            ]
            mock_client.get = AsyncMock(return_value=mock_response)

            for key in ("INDEED_EMAIL", "INDEED_PASSWORD",
                        "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                        "JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD"):
                os.environ.pop(key, None)

            orchestrator = Orchestrator()
            await orchestrator.load_credentials_from_dashboard()

            self.assertEqual(os.environ.get("INDEED_EMAIL"),    "cloud@indeed.com")
            self.assertEqual(os.environ.get("INDEED_PASSWORD"),  "cloudpw1")
            self.assertEqual(os.environ.get("LINKEDIN_EMAIL"),  "cloud@linkedin.com")
            self.assertEqual(os.environ.get("LINKEDIN_PASSWORD"), "cloudpw2")
            self.assertEqual(os.environ.get("JOBRIGHT_EMAIL"),  "cloud@jobright.com")
            self.assertEqual(os.environ.get("JOBRIGHT_PASSWORD"), "cloudpw3")
        finally:
            for k, v in [("DASHBOARD_URL", old_url), ("SYNC_SECRET", old_secret)]:
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    @patch("httpx.AsyncClient")
    async def test_load_credentials_graceful_on_http_error(self, mock_client_class):
        """A failed cloud fetch should not crash the agent — it logs and continues."""
        from src.orchestrator import Orchestrator

        os.environ["DASHBOARD_URL"] = "https://dashboard-test.com"
        os.environ["SYNC_SECRET"]   = "testsecret"

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=ConnectionError("simulated network failure"))

        orchestrator = Orchestrator()
        # Should NOT raise — failure is non-fatal
        try:
            await orchestrator.load_credentials_from_dashboard()
        except Exception as exc:
            self.fail(f"load_credentials_from_dashboard raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()
